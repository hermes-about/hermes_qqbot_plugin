import asyncio
import concurrent.futures
import logging
import threading
from typing import Any

from .models import DeliveryItem, MessageIdentity, SendOutcome

logger = logging.getLogger("autoqq.sender")


def _platform_name(value: Any) -> str:
    return str(getattr(value, "value", value) or "").lower()


class GatewaySender:
    """Narrow adapter around the documented ``gateway.adapters[platform].send`` surface."""

    def __init__(self, timeout_seconds: float) -> None:
        self._timeout = timeout_seconds
        self._lock = threading.RLock()
        self._gateway: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._platform_keys: dict[str, Any] = {}
        self._adapters: dict[str, Any] = {}

    def bind(self, gateway: Any, platform_key: Any) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise RuntimeError("Hermes gateway hook is not running on an event loop") from exc
        with self._lock:
            self._gateway = gateway
            self._loop = loop
            self._platform_keys[_platform_name(platform_key)] = platform_key

    def bind_adapter(self, platform: str, adapter: Any) -> None:
        if not callable(getattr(adapter, "send", None)):
            raise RuntimeError("Hermes platform adapter has no send method")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise RuntimeError("Hermes platform factory is not running on an event loop") from exc
        with self._lock:
            self._loop = loop
            self._adapters[platform] = adapter

    @property
    def is_bound(self) -> bool:
        with self._lock:
            return self._loop is not None and (self._gateway is not None or bool(self._adapters))

    def reply(self, identity: MessageIdentity, text: str) -> None:
        loop, adapter = self._resolve(identity.platform)

        async def send_reply() -> Any:
            return await adapter.send(identity.chat_id, text)

        def log_failure(future: asyncio.Future[Any]) -> None:
            try:
                result = future.result()
                if not self._normalize(result).success:
                    logger.warning("autoqq command reply was not accepted by the adapter")
            except Exception:
                logger.exception("autoqq command reply failed")

        if loop is asyncio.get_running_loop():
            task = loop.create_task(send_reply())
            task.add_done_callback(log_failure)
        else:
            future = asyncio.run_coroutine_threadsafe(send_reply(), loop)
            future.add_done_callback(
                lambda completed: (
                    logger.warning("autoqq command reply failed") if completed.exception() else None
                )
            )

    def reply_image(
        self, identity: MessageIdentity, image_url: str, *, caption: str | None = None
    ) -> None:
        """Send a native image when supported, with a text fallback for older adapters."""
        loop, adapter = self._resolve(identity.platform)

        async def send_reply() -> Any:
            send_image = getattr(adapter, "send_image", None)
            if callable(send_image):
                return await send_image(identity.chat_id, image_url, caption=caption)
            fallback = f"{caption}\n{image_url}" if caption else image_url
            return await adapter.send(identity.chat_id, fallback)

        def log_failure(future: asyncio.Future[Any]) -> None:
            try:
                result = future.result()
                if not self._normalize(result).success:
                    logger.warning("autoqq command image reply was not accepted by the adapter")
            except Exception:
                logger.exception("autoqq command image reply failed")

        if loop is asyncio.get_running_loop():
            task = loop.create_task(send_reply())
            task.add_done_callback(log_failure)
        else:
            future = asyncio.run_coroutine_threadsafe(send_reply(), loop)
            future.add_done_callback(
                lambda completed: (
                    logger.warning("autoqq command image reply failed")
                    if completed.exception()
                    else None
                )
            )

    def send_delivery(self, item: DeliveryItem) -> SendOutcome:
        try:
            loop, adapter = self._resolve(item.platform)
        except Exception:
            logger.exception("autoqq delivery sender is not bound")
            return SendOutcome(False, error_code="HERMES_NOT_READY", retryable=True)

        async def send_message() -> Any:
            return await adapter.send(item.openid, str(item.message["text"]))

        future = asyncio.run_coroutine_threadsafe(send_message(), loop)
        try:
            return self._normalize(future.result(timeout=self._timeout))
        except concurrent.futures.TimeoutError:
            return SendOutcome(False, error_code="HERMES_SEND_TIMEOUT", retryable=True)
        except Exception:
            logger.exception("autoqq proactive delivery failed")
            return SendOutcome(False, error_code="HERMES_SEND_ERROR", retryable=True)

    def _resolve(self, platform: str) -> tuple[asyncio.AbstractEventLoop, Any]:
        with self._lock:
            gateway = self._gateway
            loop = self._loop
            key = self._platform_keys.get(platform)
            adapter = self._adapters.get(platform)
        if loop is None or loop.is_closed():
            raise RuntimeError("Hermes gateway sender is not bound")
        if adapter is None and gateway is not None:
            adapters = getattr(gateway, "adapters", None)
            if isinstance(adapters, dict):
                adapter = adapters.get(key) if key is not None else None
                if adapter is None:
                    adapter = next(
                        (
                            value
                            for candidate, value in adapters.items()
                            if _platform_name(candidate) == platform
                        ),
                        None,
                    )
        if adapter is None or not callable(getattr(adapter, "send", None)):
            raise RuntimeError("Hermes platform adapter is unavailable")
        return loop, adapter

    @staticmethod
    def _normalize(result: Any) -> SendOutcome:
        if isinstance(result, dict):
            success = bool(result.get("success"))
            message_id = result.get("message_id") or result.get("id")
            return SendOutcome(
                success,
                str(message_id) if message_id else None,
                error_code="HERMES_SEND_REJECTED",
                retryable=True,
            )
        success = bool(getattr(result, "success", False))
        message_id = getattr(result, "message_id", None)
        raw = getattr(result, "raw_response", None)
        if not message_id and isinstance(raw, dict):
            message_id = raw.get("message_id") or raw.get("id")
        return SendOutcome(
            success,
            str(message_id) if message_id else None,
            error_code="HERMES_SEND_REJECTED",
            retryable=True,
        )
