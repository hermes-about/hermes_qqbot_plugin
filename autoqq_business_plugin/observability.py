import hashlib
import logging
import re

_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")
_SECRET = re.compile(
    r'(?i)("?(?:password|token|secret|lease_token|pairing_code)"?\s*[:=]\s*")([^"\s]+)'
)


def mask_identifier(value: str) -> str:
    if not value:
        return "unknown"
    digest = hashlib.sha256(value.encode()).hexdigest()[:10]
    return f"id:{digest}"


def redact_text(value: str) -> str:
    return _SECRET.sub(r"\1[REDACTED]", _BEARER.sub(r"\1[REDACTED]", value))


def audit(logger: logging.Logger, action: str, platform: str, openid: str, result: str) -> None:
    logger.info(
        "autoqq action=%s platform=%s actor=%s result=%s",
        action,
        platform,
        mask_identifier(openid),
        result,
    )
