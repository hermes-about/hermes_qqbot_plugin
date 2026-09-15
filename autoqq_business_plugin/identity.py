from typing import Any

from .models import MessageIdentity


class IdentityError(ValueError):
    pass


def _string(value: Any) -> str:
    actual = getattr(value, "value", value)
    return str(actual or "").strip()


def extract_identity(event: Any) -> MessageIdentity:
    source = getattr(event, "source", None)
    if source is None:
        raise IdentityError("message source is missing")
    platform = _string(getattr(source, "platform", "")).lower()
    openid = _string(getattr(source, "user_id", ""))
    chat_id = _string(getattr(source, "chat_id", ""))
    text = str(getattr(event, "text", "") or "").strip()
    chat_type = _string(getattr(source, "chat_type", "")) or "unknown"
    if not platform or not openid or not chat_id:
        raise IdentityError("trusted platform, user_id, and chat_id are required")
    if len(openid) > 128 or len(chat_id) > 256:
        raise IdentityError("message identity exceeds supported length")
    return MessageIdentity(platform, openid, chat_id, chat_type, text)
