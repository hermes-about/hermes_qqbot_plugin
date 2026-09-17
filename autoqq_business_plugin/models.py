from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class AccessPolicy(StrEnum):
    PUBLIC = "public"
    AUTHORIZED = "authorized"
    ADMIN = "admin"


@dataclass(frozen=True)
class MessageIdentity:
    platform: str
    openid: str
    chat_id: str
    chat_type: str
    text: str


@dataclass(frozen=True)
class PermissionSnapshot:
    platform: str
    openid: str
    account_status: str
    role: str
    chat: bool
    command: bool

    @property
    def blocked(self) -> bool:
        return self.account_status == "blocked"

    def allows(self, policy: AccessPolicy) -> bool:
        if self.account_status == "blocked":
            return False
        if policy is AccessPolicy.PUBLIC:
            return True
        if self.account_status != "active" or not self.command:
            return False
        return policy is AccessPolicy.AUTHORIZED or self.role == "admin"


@dataclass(frozen=True)
class ParsedCommand:
    name: str
    args: tuple[str, ...]


@dataclass(frozen=True)
class DispatchDecision:
    action: str
    reason: str
    reply: str | None = None


@dataclass(frozen=True)
class MatchKeyOption:
    key: str
    label: str


@dataclass(frozen=True)
class EventInfo:
    event_key: str
    display_name: str
    description: str
    deprecated: bool = False
    match_key_field: str | None = None
    match_keys_required: bool = False
    match_key_options: tuple[MatchKeyOption, ...] = ()

    def option_label(self, key: str) -> str | None:
        for option in self.match_key_options:
            if option.key == key:
                return option.label
        return None


@dataclass(frozen=True)
class SubscriptionInfo:
    event_key: str
    locale: str
    changed: bool | None = None
    match_keys: tuple[str, ...] = ()
    updated: bool = False


@dataclass(frozen=True)
class DeliveryItem:
    delivery_id: str
    lease_token: str
    event_id: str
    event_key: str
    platform: str
    openid: str
    message: dict[str, Any]
    attempt: int
    created_at: datetime


@dataclass(frozen=True)
class SendOutcome:
    success: bool
    message_id: str | None = None
    error_code: str = "HERMES_SEND_ERROR"
    retryable: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)
