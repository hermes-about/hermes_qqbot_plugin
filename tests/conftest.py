from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from autoqq_business_plugin.command_policy import CommandPolicyRegistry
from autoqq_business_plugin.commands import CommandService
from autoqq_business_plugin.models import (
    BindingContext,
    EventInfo,
    MatchKeyOption,
    PermissionSnapshot,
    SubscriptionInfo,
)
from autoqq_business_plugin.permission_cache import PermissionCache
from autoqq_business_plugin.processor import MessageProcessor
from autoqq_business_plugin.rate_limit import SlidingWindowRateLimiter


def message(
    text: str,
    openid: str = "user-openid",
    platform: str = "qqbot",
    *,
    chat_type: str = "dm",
    chat_id: str | None = None,
):
    source = SimpleNamespace(
        platform=platform,
        user_id=openid,
        chat_id=chat_id or (openid if platform == "qqbot" else "chat-id"),
        chat_type=chat_type,
    )
    return SimpleNamespace(source=source, text=text, message_id="message-1")


@dataclass
class FakeClient:
    snapshot: PermissionSnapshot

    def __post_init__(self) -> None:
        self.permissions: dict[str, PermissionSnapshot] = {self.snapshot.openid: self.snapshot}
        self.calls: list[tuple] = []
        self.raise_permission: Exception | None = None
        self.updated = False
        self.subscriptions: list[SubscriptionInfo] = [
            SubscriptionInfo("demo.event.changed", "zh-CN")
        ]

    def get_permission(self, platform: str, openid: str) -> PermissionSnapshot:
        self.calls.append(("permission", platform, openid))
        if self.raise_permission:
            raise self.raise_permission
        return self.permissions.get(
            openid, PermissionSnapshot(platform, openid, "unknown", "user", False, False)
        )

    def list_events(self):
        self.calls.append(("events",))
        return [
            EventInfo("demo.event.changed", "示例事件", "description"),
            EventInfo(
                "warframe.cetus.bounty_current",
                "Cetus 当前轮次赏金",
                "当前轮次出现的任务",
                match_key_field="match_keys",
                match_keys_required=True,
                match_key_options=(
                    MatchKeyOption("RescueBountyResc", "搜索并救援"),
                    MatchKeyOption("ReclamationBountyCap", "捕获 Grineer 特工"),
                ),
            ),
        ]

    def list_subscriptions(self, platform: str, openid: str):
        self.calls.append(("bindings", platform, openid))
        return list(self.subscriptions)

    def subscribe(
        self,
        platform: str,
        openid: str,
        event_key: str,
        match_keys=None,
        binding_context: BindingContext | None = None,
    ):
        binding = (binding_context.chat_type, binding_context.chat_id) if binding_context else None
        self.calls.append(("bind", platform, openid, event_key, tuple(match_keys or ()), binding))
        return SubscriptionInfo(
            event_key,
            "zh-CN",
            True,
            tuple(match_keys or ()),
            self.updated,
            binding_context,
        )

    def unsubscribe(self, platform: str, openid: str, event_key: str):
        self.calls.append(("unbind", platform, openid, event_key))
        return SubscriptionInfo(event_key, "", False)

    def create_pairing_code(self, platform: str, openid: str):
        self.calls.append(("pair", platform, openid))
        return "ABCDEFGH", object()

    def grant(self, platform: str, target: str, permissions: list[str], operator: str):
        self.calls.append(("grant", platform, target, tuple(permissions), operator))
        current = self.permissions.get(
            target, PermissionSnapshot(platform, target, "active", "user", False, False)
        )
        result = PermissionSnapshot(
            platform,
            target,
            current.account_status,
            current.role,
            current.chat or "chat" in permissions,
            current.command or "command" in permissions,
            current.bind,
        )
        self.permissions[target] = result
        return result

    def grant_bind(self, platform: str, target: str, scopes: list[str], operator: str):
        self.calls.append(("grant-bind", platform, target, tuple(scopes), operator))
        current = self.permissions.get(
            target, PermissionSnapshot(platform, target, "active", "user", False, False)
        )
        result = PermissionSnapshot(
            platform,
            target,
            current.account_status,
            current.role,
            current.chat,
            current.command,
            tuple(sorted(set(current.bind).union(scopes))),
        )
        self.permissions[target] = result
        return result

    def approve_pairing(self, platform: str, code: str, permissions: list[str], operator: str):
        self.calls.append(("approve", platform, code, tuple(permissions), operator))
        return PermissionSnapshot(platform, "paired-user", "active", "user", False, True)

    def revoke(self, platform: str, target: str, permissions: list[str], operator: str):
        self.calls.append(("revoke", platform, target, tuple(permissions), operator))
        current = self.permissions.get(
            target, PermissionSnapshot(platform, target, "active", "user", False, False)
        )
        result = PermissionSnapshot(
            platform,
            target,
            current.account_status,
            current.role,
            current.chat and "chat" not in permissions,
            current.command and "command" not in permissions,
            current.bind,
        )
        self.permissions[target] = result
        return result

    def revoke_bind(
        self,
        platform: str,
        target: str,
        scopes: list[str],
        operator: str,
        *,
        clear_all: bool = False,
    ):
        self.calls.append(("revoke-bind", platform, target, tuple(scopes), operator, clear_all))
        current = self.permissions.get(
            target, PermissionSnapshot(platform, target, "active", "user", False, False)
        )
        remaining = (
            () if clear_all else tuple(scope for scope in current.bind if scope not in scopes)
        )
        result = PermissionSnapshot(
            platform,
            target,
            current.account_status,
            current.role,
            current.chat,
            current.command,
            remaining,
        )
        self.permissions[target] = result
        return result

    def change_role(self, platform: str, target: str, role: str, operator: str):
        self.calls.append(("role", platform, target, role, operator))
        current = self.permissions.get(
            target, PermissionSnapshot(platform, target, "active", "user", False, False)
        )
        result = PermissionSnapshot(
            platform,
            target,
            current.account_status,
            role,
            current.chat,
            current.command,
            current.bind,
        )
        self.permissions[target] = result
        return result


@pytest.fixture
def policy() -> CommandPolicyRegistry:
    path = Path(__file__).parents[1] / "config" / "commands.yaml"
    return CommandPolicyRegistry.from_file(path)


def make_processor(
    client: FakeClient,
    policy: CommandPolicyRegistry,
    *,
    query_catalog=None,
    query_service=None,
    query_reply_max_chars: int = 1200,
) -> MessageProcessor:
    cache = PermissionCache(30)
    limiter = SlidingWindowRateLimiter(100, 60)
    commands = CommandService(
        client,
        cache,
        policy,
        query_catalog=query_catalog,
        query_service=query_service,
        query_reply_max_chars=query_reply_max_chars,
    )
    return MessageProcessor(client, policy, cache, limiter, commands)
