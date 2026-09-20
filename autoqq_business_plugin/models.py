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
    aliases: tuple[str, ...] = ()


def resolve_match_key(options: tuple[MatchKeyOption, ...], token: str) -> str | None:
    """Resolve a user-typed option to its canonical key.

    Options are declared by a controlled catalogue (EventServer events or the
    query service). Users may type the canonical key or the catalogue label;
    labels are unique within one catalogue entry, so the mapping is
    deterministic. The canonical key is what gets sent to the service.
    """
    for option in options:
        if option.key == token:
            return option.key
    lowered = token.lower()
    for option in options:
        values = [option.key, option.label, *option.aliases]
        if any(value and value.lower() == lowered for value in values):
            return option.key
    return None


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

    def match_key_for(self, token: str) -> str | None:
        """Resolve a user-typed watch token to a catalogue key."""
        return resolve_match_key(self.match_key_options, token)


@dataclass(frozen=True)
class QueryTarget:
    """One subject a query command can answer, e.g. `地球`."""

    query_key: str
    aliases: tuple[str, ...] = ()
    reply: str = "text"
    """Channel-neutral reply mode: `text` or `image_url` when the answer carries an image."""


@dataclass(frozen=True)
class QueryNamespace:
    """One query command declared by the controlled catalogue, e.g. `/wf`."""

    command: str
    display_name: str
    description: str = ""
    targets: tuple[QueryTarget, ...] = ()

    def resolve(self, token: str) -> QueryTarget | None:
        wanted = token.strip().lower()
        if not wanted:
            return None
        for target in self.targets:
            if target.query_key.lower() == wanted:
                return target
            if any(alias.lower() == wanted for alias in target.aliases):
                return target
        return None


@dataclass(frozen=True)
class QueryParam:
    """One declared query parameter, owned by the query service catalogue."""

    name: str
    label: str
    type: str = "text"
    options: tuple[MatchKeyOption, ...] = ()
    required: bool = False
    multiple: bool = False
    max_items: int = 0
    max_length: int = 64
    default: str = ""

    def option_label(self, key: str) -> str | None:
        for option in self.options:
            if option.key == key:
                return option.label
        return None

    def option_key_for(self, token: str) -> str | None:
        return resolve_match_key(self.options, token)


@dataclass(frozen=True)
class QueryAction:
    """One declared action inside a query, e.g. `词条` for the riven target."""

    key: str
    label: str = ""
    aliases: tuple[str, ...] = ()
    params: tuple[str, ...] = ()
    default: bool = False

    def matches(self, token: str) -> bool:
        wanted = token.strip().lower()
        if not wanted:
            return False
        values = (self.key, self.label, *self.aliases)
        return any(value and value.lower() == wanted for value in values)


@dataclass(frozen=True)
class QueryInfo:
    """Catalogue entry reported by the read-only query service."""

    query_key: str
    display_name: str
    description: str = ""
    route: str = ""
    params: tuple[QueryParam, ...] = ()
    actions: tuple[QueryAction, ...] = ()

    def param(self, name: str) -> QueryParam | None:
        for item in self.params:
            if item.name == name:
                return item
        return None

    def default_action(self) -> QueryAction | None:
        for item in self.actions:
            if item.default:
                return item
        return self.actions[0] if self.actions else None

    def action_for(self, token: str) -> QueryAction | None:
        for item in self.actions:
            if item.matches(token):
                return item
        return None

    def action_params(self, action: QueryAction | None) -> tuple[QueryParam, ...]:
        """Return the ordered parameters an action accepts."""
        if action is None:
            return self.params
        resolved = [self.param(name) for name in action.params]
        return tuple(item for item in resolved if item is not None)


@dataclass(frozen=True)
class QueryResult:
    """Channel-neutral answer returned by the query service."""

    query_key: str
    title: str
    text: str
    image_url: str | None = None


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
