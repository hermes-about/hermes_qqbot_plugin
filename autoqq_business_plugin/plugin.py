import atexit
import logging
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

from .command_policy import CommandPolicyRegistry
from .commands import CommandService
from .config import Settings
from .delivery_worker import DeliveryWorker
from .eventserver_client import EventServerClient
from .hermes_sender import GatewaySender
from .identity import extract_identity
from .permission_cache import PermissionCache
from .processor import MessageProcessor
from .query_catalog import QueryCatalog, QueryCatalogError
from .query_client import QueryServiceClient
from .rate_limit import SlidingWindowRateLimiter

logger = logging.getLogger("autoqq.plugin")
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_runtime_lock = threading.Lock()
_runtime: "PluginRuntime | None" = None


class PluginRuntime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = EventServerClient(
            settings.event_server_url,
            settings.internal_api_token,
            connect_timeout=settings.connect_timeout_seconds,
            read_timeout=settings.read_timeout_seconds,
            safe_read_retries=settings.safe_read_retries,
            max_response_bytes=settings.max_response_bytes,
        )
        packaged_policy = Path(__file__).resolve().parent / "config" / "commands.yaml"
        source_policy = _PLUGIN_ROOT / "config" / "commands.yaml"
        policy_path = packaged_policy if packaged_policy.is_file() else source_policy
        policies = CommandPolicyRegistry.from_file(policy_path)
        packaged_queries = Path(__file__).resolve().parent / "config" / "queries.yaml"
        source_queries = _PLUGIN_ROOT / "config" / "queries.yaml"
        queries_path = packaged_queries if packaged_queries.is_file() else source_queries
        try:
            query_catalog = QueryCatalog.from_file(queries_path)
        except QueryCatalogError:
            logger.exception("autoqq query catalogue is invalid; query commands stay unavailable")
            query_catalog = QueryCatalog.empty()
        query_service = None
        if settings.query_service_url:
            query_service = QueryServiceClient(
                settings.query_service_url,
                settings.query_service_token,
                connect_timeout=settings.query_connect_timeout_seconds,
                read_timeout=settings.query_read_timeout_seconds,
                max_response_bytes=settings.query_max_response_bytes,
                catalog_ttl_seconds=settings.query_catalog_ttl_seconds,
            )
        cache = PermissionCache(settings.permission_cache_ttl_seconds)
        limiter = SlidingWindowRateLimiter(
            settings.command_rate_limit_count, settings.command_rate_limit_window_seconds
        )
        self.query_client = query_service
        commands = CommandService(
            self.client,
            cache,
            policies,
            query_catalog=query_catalog,
            query_service=query_service,
            query_reply_max_chars=settings.query_reply_max_chars,
        )
        self.processor = MessageProcessor(self.client, policies, cache, limiter, commands)
        self.sender = GatewaySender(settings.delivery_send_timeout_seconds)
        self.worker = DeliveryWorker(
            self.client,
            self.sender,
            worker_id=settings.delivery_worker_id,
            batch_size=settings.delivery_claim_batch_size,
            lease_seconds=settings.delivery_lease_seconds,
            poll_interval_seconds=settings.delivery_poll_interval_seconds,
            max_backoff_seconds=settings.delivery_empty_poll_backoff_max_seconds,
            max_message_chars=settings.delivery_max_message_chars,
        )
        self._closed = False

    def hook(self, event: Any, gateway: Any, **_kwargs: Any) -> dict[str, str]:
        """Hermes `pre_gateway_dispatch`; all exceptions are contained to fail closed."""
        try:
            source = getattr(event, "source", None)
            platform_key = getattr(source, "platform", "")
            platform_name = str(getattr(platform_key, "value", platform_key) or "").lower()
            if platform_name == "qqbot":
                self.sender.bind(gateway, platform_key)
                if self.settings.delivery_poll_enabled:
                    self.worker.start()
            decision = self.processor.process(event)
            if decision.reply:
                # Identity has already been validated inside the processor; extract again only at
                # this narrow adapter boundary so the core decision remains immutable.
                self.sender.reply(extract_identity(event), decision.reply)
            if decision.action == "allow":
                return {"action": "allow"}
            return {"action": "skip", "reason": decision.reason}
        except Exception:
            logger.exception("autoqq hook failed closed")
            return {"action": "skip", "reason": "autoqq-internal-error"}

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.worker.stop()
        if self.query_client is not None:
            self.query_client.close()
        self.client.close()

    def bind_qqbot_adapter(self, _native: Any, adapter: Any) -> None:
        """Hermes platform-connect factory; starts polling before the first user message."""
        self.sender.bind_adapter("qqbot", adapter)
        if self.settings.delivery_poll_enabled:
            self.worker.start()


def _build_runtime(ctx: Any) -> PluginRuntime:
    settings = Settings.from_env()
    try:
        poll_enabled = ctx.get_config(
            "delivery_poll_enabled", default=settings.delivery_poll_enabled
        )
    except Exception:
        poll_enabled = settings.delivery_poll_enabled
    if isinstance(poll_enabled, bool):
        settings = replace(settings, delivery_poll_enabled=poll_enabled)
    return PluginRuntime(settings)


def register(ctx: Any) -> None:
    """Register the native Hermes plugin without importing Hermes internals."""
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            _runtime = _build_runtime(ctx)
            atexit.register(_runtime.close)
        runtime = _runtime
    ctx.register_hook("pre_gateway_dispatch", runtime.hook)
    register_platform_handler = getattr(ctx, "register_platform_handler", None)
    if callable(register_platform_handler):
        register_platform_handler("qqbot", runtime.bind_qqbot_adapter)
