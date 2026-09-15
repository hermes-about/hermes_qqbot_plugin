from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from autoqq_business_plugin.command_policy import CommandPolicyRegistry
from autoqq_business_plugin.commands import CommandService
from autoqq_business_plugin.models import (
    EventInfo,
    PermissionSnapshot,
    SubscriptionInfo,
)
from autoqq_business_plugin.permission_cache import PermissionCache
from autoqq_business_plugin.processor import MessageProcessor
from autoqq_business_plugin.rate_limit import SlidingWindowRateLimiter


def message(text: str, openid: str = "user-openid", platform: str = "qqbot"):
    source = SimpleNamespace(
        platform=platform,
        user_id=openid,
        chat_id=openid if platform == "qqbot" else "chat-id",
        chat_type="dm",
    )
    return SimpleNamespace(source=source, text=text, message_id="message-1")


@dataclass
class FakeClient:
    snapshot: PermissionSnapshot

    def __post_init__(self) -> None:
        self.permissions: dict[str, PermissionSnapshot] = {self.snapshot.openid: self.snapshot}
        self.calls: list[tuple] = []
        self.raise_permission: Exception | None = None

    def get_permission(self, platform: str, openid: str) -> PermissionSnapshot:
        self.calls.append(("permission", platform, openid))
        if self.raise_permission:
            raise self.raise_permission
        return self.permissions.get(
            openid, PermissionSnapshot(platform, openid, "unknown", "user", False, False)
        )

    def list_events(self):
        self.calls.append(("events",))
        return [EventInfo("demo.event.changed", "示例事件", "description")]

    def list_subscriptions(self, platform: str, openid: str):
        self.calls.append(("bindings", platform, openid))
        return [SubscriptionInfo("demo.event.changed", "zh-CN")]

    def subscribe(self, platform: str, openid: str, event_key: str):
        self.calls.append(("bind", platform, openid, event_key))
        return SubscriptionInfo(event_key, "zh-CN", True)

    def unsubscribe(self, platform: str, openid: str, event_key: str):
        self.calls.append(("unbind", platform, openid, event_key))
        return SubscriptionInfo(event_key, "", False)

    def create_pairing_code(self, platform: str, openid: str):
        self.calls.append(("pair", platform, openid))
        return "ABCDEFGH", object()

    def grant(self, platform: str, target: str, permissions: list[str], operator: str):
        self.calls.append(("grant", platform, target, tuple(permissions), operator))
        result = PermissionSnapshot(
            platform, target, "active", "user", "chat" in permissions, "command" in permissions
        )
        self.permissions[target] = result
        return result

    def approve_pairing(self, platform: str, code: str, permissions: list[str], operator: str):
        self.calls.append(("approve", platform, code, tuple(permissions), operator))
        return PermissionSnapshot(platform, "paired-user", "active", "user", False, True)

    def revoke(self, platform: str, target: str, permissions: list[str], operator: str):
        self.calls.append(("revoke", platform, target, tuple(permissions), operator))
        return PermissionSnapshot(platform, target, "active", "user", False, False)

    def change_role(self, platform: str, target: str, role: str, operator: str):
        self.calls.append(("role", platform, target, role, operator))
        return PermissionSnapshot(platform, target, "active", role, False, False)


@pytest.fixture
def policy() -> CommandPolicyRegistry:
    path = Path(__file__).parents[1] / "config" / "commands.yaml"
    return CommandPolicyRegistry.from_file(path)


def make_processor(client: FakeClient, policy: CommandPolicyRegistry) -> MessageProcessor:
    cache = PermissionCache(30)
    limiter = SlidingWindowRateLimiter(100, 60)
    commands = CommandService(client, cache, policy)
    return MessageProcessor(client, policy, cache, limiter, commands)
