import os
from dataclasses import dataclass
from urllib.parse import urlsplit


class ConfigurationError(ValueError):
    pass


def _integer(env: dict[str, str], name: str, default: int, minimum: int, maximum: int) -> int:
    raw = env.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


def _number(env: dict[str, str], name: str, default: float, minimum: float) -> float:
    raw = env.get(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if value < minimum:
        raise ConfigurationError(f"{name} must be at least {minimum}")
    return value


def _boolean(env: dict[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be a boolean")


@dataclass(frozen=True)
class Settings:
    event_server_url: str
    internal_api_token: str
    connect_timeout_seconds: float = 3.0
    read_timeout_seconds: float = 5.0
    safe_read_retries: int = 1
    max_response_bytes: int = 262_144
    permission_cache_ttl_seconds: float = 30.0
    command_rate_limit_count: int = 10
    command_rate_limit_window_seconds: float = 60.0
    delivery_poll_enabled: bool = True
    delivery_poll_interval_seconds: float = 5.0
    delivery_claim_batch_size: int = 10
    delivery_lease_seconds: int = 60
    delivery_worker_id: str = ""
    delivery_empty_poll_backoff_max_seconds: float = 30.0
    delivery_send_timeout_seconds: float = 20.0
    delivery_max_message_chars: int = 2000
    default_timezone: str = "Asia/Shanghai"
    query_service_url: str = ""
    query_service_token: str = ""
    query_connect_timeout_seconds: float = 2.0
    query_read_timeout_seconds: float = 4.0
    query_max_response_bytes: int = 65_536
    query_catalog_ttl_seconds: float = 300.0
    query_reply_max_chars: int = 1200

    @classmethod
    def from_env(cls, source: dict[str, str] | None = None) -> "Settings":
        env = dict(os.environ if source is None else source)
        base_url = env.get("EVENT_SERVER_URL", "").strip().rstrip("/")
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username:
            raise ConfigurationError(
                "EVENT_SERVER_URL must be an absolute HTTP(S) URL without userinfo"
            )
        token = env.get("INTERNAL_API_TOKEN", "").strip()
        if len(token) < 32:
            raise ConfigurationError("INTERNAL_API_TOKEN must contain at least 32 characters")
        worker_id = env.get("DELIVERY_WORKER_ID", "").strip()
        poll_enabled = _boolean(env, "DELIVERY_POLL_ENABLED", True)
        if poll_enabled and not worker_id:
            raise ConfigurationError(
                "DELIVERY_WORKER_ID is required when delivery polling is enabled"
            )
        query_url = env.get("QUERY_SERVICE_URL", "").strip().rstrip("/")
        query_token = env.get("QUERY_SERVICE_TOKEN", "").strip()
        if query_url:
            parsed_query = urlsplit(query_url)
            if (
                parsed_query.scheme not in {"http", "https"}
                or not parsed_query.netloc
                or parsed_query.username
            ):
                raise ConfigurationError(
                    "QUERY_SERVICE_URL must be an absolute HTTP(S) URL without userinfo"
                )
            if len(query_token) < 32:
                raise ConfigurationError("QUERY_SERVICE_TOKEN must contain at least 32 characters")
        elif query_token:
            raise ConfigurationError("QUERY_SERVICE_TOKEN requires QUERY_SERVICE_URL")
        return cls(
            event_server_url=base_url,
            internal_api_token=token,
            connect_timeout_seconds=_number(env, "EVENTSERVER_CONNECT_TIMEOUT_SECONDS", 3, 0.1),
            read_timeout_seconds=_number(env, "EVENTSERVER_READ_TIMEOUT_SECONDS", 5, 0.1),
            safe_read_retries=_integer(env, "EVENTSERVER_SAFE_READ_RETRIES", 1, 0, 3),
            max_response_bytes=_integer(
                env, "EVENTSERVER_MAX_RESPONSE_BYTES", 262_144, 1024, 4_194_304
            ),
            permission_cache_ttl_seconds=_number(env, "PERMISSION_CACHE_TTL_SECONDS", 30, 0),
            command_rate_limit_count=_integer(env, "COMMAND_RATE_LIMIT_COUNT", 10, 1, 1000),
            command_rate_limit_window_seconds=_number(
                env, "COMMAND_RATE_LIMIT_WINDOW_SECONDS", 60, 1
            ),
            delivery_poll_enabled=poll_enabled,
            delivery_poll_interval_seconds=_number(env, "DELIVERY_POLL_INTERVAL_SECONDS", 5, 0.1),
            delivery_claim_batch_size=_integer(env, "DELIVERY_CLAIM_BATCH_SIZE", 10, 1, 100),
            delivery_lease_seconds=_integer(env, "DELIVERY_LEASE_SECONDS", 60, 10, 600),
            delivery_worker_id=worker_id,
            delivery_empty_poll_backoff_max_seconds=_number(
                env, "DELIVERY_EMPTY_POLL_BACKOFF_MAX_SECONDS", 30, 1
            ),
            delivery_send_timeout_seconds=_number(env, "DELIVERY_SEND_TIMEOUT_SECONDS", 20, 0.1),
            delivery_max_message_chars=_integer(env, "DELIVERY_MAX_MESSAGE_CHARS", 2000, 1, 10000),
            default_timezone=(
                env.get("DEFAULT_TIMEZONE", "Asia/Shanghai").strip() or "Asia/Shanghai"
            ),
            query_service_url=query_url,
            query_service_token=query_token,
            query_connect_timeout_seconds=_number(env, "QUERY_CONNECT_TIMEOUT_SECONDS", 2, 0.1),
            query_read_timeout_seconds=_number(env, "QUERY_READ_TIMEOUT_SECONDS", 4, 0.1),
            query_max_response_bytes=_integer(
                env, "QUERY_MAX_RESPONSE_BYTES", 65_536, 1024, 4_194_304
            ),
            query_catalog_ttl_seconds=_number(env, "QUERY_CATALOG_TTL_SECONDS", 300, 0),
            query_reply_max_chars=_integer(env, "QUERY_REPLY_MAX_CHARS", 1200, 200, 4000),
        )
