import pytest
from conftest import FakeClient, make_processor, message

from autoqq_business_plugin.eventserver_client import (
    EventServerResponseError,
    EventServerUnavailable,
)
from autoqq_business_plugin.models import PermissionSnapshot


@pytest.mark.parametrize(
    ("chat", "command", "chat_action", "bind_reply"),
    [
        (False, False, "skip", "没有执行"),
        (False, True, "skip", "已订阅"),
        (True, False, "allow", "没有执行"),
        (True, True, "allow", "已订阅"),
    ],
)
def test_chat_command_four_way_matrix(
    policy, chat: bool, command: bool, chat_action: str, bind_reply: str
) -> None:
    snapshot = PermissionSnapshot("qqbot", "user-openid", "active", "user", chat, command)
    processor = make_processor(FakeClient(snapshot), policy)
    assert processor.process(message("hello")).action == chat_action
    decision = processor.process(message("/bind demo.event.changed"))
    assert decision.action == "skip"
    assert bind_reply in (decision.reply or "")


def test_public_command_allows_unknown_but_blocked_wins(policy) -> None:
    unknown = PermissionSnapshot("qqbot", "user-openid", "unknown", "user", False, False)
    allowed = make_processor(FakeClient(unknown), policy).process(message("/events"))
    assert allowed.action == "skip"
    assert "demo.event.changed" in (allowed.reply or "")

    blocked = PermissionSnapshot("qqbot", "user-openid", "blocked", "admin", True, True)
    denied = make_processor(FakeClient(blocked), policy).process(message("/help"))
    assert denied.action == "skip"
    assert denied.reason == "command-denied"


@pytest.mark.parametrize(
    "snapshot",
    [
        PermissionSnapshot("qqbot", "user-openid", "active", "admin", True, False),
        PermissionSnapshot("qqbot", "user-openid", "active", "user", True, True),
        PermissionSnapshot("qqbot", "user-openid", "blocked", "admin", True, True),
    ],
)
def test_admin_requires_active_admin_and_command(policy, snapshot) -> None:
    decision = make_processor(FakeClient(snapshot), policy).process(
        message("/grant target-openid command")
    )
    assert decision.reason == "command-denied"


def test_admin_command_uses_real_actor_and_explicit_dimension(policy) -> None:
    admin = PermissionSnapshot("qqbot", "user-openid", "active", "admin", True, True)
    client = FakeClient(admin)
    processor = make_processor(client, policy)
    invalid = processor.process(message("/grant target-openid"))
    assert "用法" in (invalid.reply or "")
    valid = processor.process(message("/grant target-openid command"))
    assert valid.reason == "command-handled"
    assert ("grant", "qqbot", "target-openid", ("command",), "user-openid") in client.calls


def test_admin_can_grant_and_clear_scoped_bind_permission(policy) -> None:
    admin = PermissionSnapshot("qqbot", "user-openid", "active", "admin", True, True, ("*",))
    client = FakeClient(admin)
    processor = make_processor(client, policy)

    granted = processor.process(message("/grant target-openid bind warframe.* warframe.cetus.*"))
    assert granted.reason == "command-handled"
    assert "bind=warframe.*,warframe.cetus.*" in (granted.reply or "")
    assert (
        "grant-bind",
        "qqbot",
        "target-openid",
        ("warframe.*", "warframe.cetus.*"),
        "user-openid",
    ) in client.calls

    cleared = processor.process(message("/revoke target-openid bind all"))
    assert cleared.reason == "command-handled"
    assert "bind=none" in (cleared.reply or "")
    assert (
        "revoke-bind",
        "qqbot",
        "target-openid",
        (),
        "user-openid",
        True,
    ) in client.calls


@pytest.mark.parametrize(
    "text",
    [
        "/grant target-openid bind warframe*",
        "/grant target-openid bind Warframe.*",
        "/revoke target-openid bind all warframe.*",
        "/grant ABCDEFGH bind warframe.*",
    ],
)
def test_bind_scope_admin_commands_reject_ambiguous_input(policy, text: str) -> None:
    admin = PermissionSnapshot("qqbot", "user-openid", "active", "admin", True, True)
    decision = make_processor(FakeClient(admin), policy).process(message(text))
    assert decision.reason == "command-handled"
    assert "bind" in (decision.reply or "")


def test_bind_scope_denial_is_reported_without_falling_into_llm(policy) -> None:
    class DeniedClient(FakeClient):
        def subscribe(self, *args, **kwargs):
            raise EventServerResponseError(403, "FORBIDDEN")

    active = PermissionSnapshot("qqbot", "user-openid", "active", "user", False, True)
    decision = make_processor(DeniedClient(active), policy).process(
        message("/bind demo.event.changed")
    )
    assert decision.action == "skip"
    assert decision.reason == "command-handled"
    assert "bind 权限" in (decision.reply or "")


def test_pairing_code_grant_and_mention_fail_safe(policy) -> None:
    admin = PermissionSnapshot("qqbot", "user-openid", "active", "admin", True, True)
    client = FakeClient(admin)
    processor = make_processor(client, policy)
    assert processor.process(message("/grant ABCDEFGH command")).reason == "command-handled"
    assert any(call[0] == "approve" for call in client.calls)
    mention = processor.process(message("/grant @nickname command"))
    assert "pairing code" in (mention.reply or "")


def test_permission_mutation_invalidates_cached_target(policy) -> None:
    admin = PermissionSnapshot("qqbot", "user-openid", "active", "admin", True, True)
    target = PermissionSnapshot("qqbot", "target-openid", "active", "user", False, False)
    client = FakeClient(admin)
    client.permissions[target.openid] = target
    processor = make_processor(client, policy)
    first = processor.process(message("/permissions target-openid"))
    assert "command=false" in (first.reply or "")
    processor.process(message("/grant target-openid command"))
    second = processor.process(message("/permissions target-openid"))
    assert "command=true" in (second.reply or "")
    target_permission_calls = [
        call for call in client.calls if call == ("permission", "qqbot", "target-openid")
    ]
    assert len(target_permission_calls) == 2


def test_unknown_and_malformed_commands_never_enter_llm(policy) -> None:
    active = PermissionSnapshot("qqbot", "user-openid", "active", "user", True, True)
    client = FakeClient(active)
    processor = make_processor(client, policy)
    for text in ("/missing", "/BAD!"):
        decision = processor.process(message(text))
        assert decision.action == "skip"
        assert decision.reason == "unknown-command"


def test_eventserver_failure_is_fail_closed(policy) -> None:
    active = PermissionSnapshot("qqbot", "user-openid", "active", "user", True, True)
    client = FakeClient(active)
    client.raise_permission = EventServerUnavailable("down")
    decision = make_processor(client, policy).process(message("hello"))
    assert decision.action == "skip"
    assert decision.reason == "eventserver-unavailable"


def test_unknown_chat_receives_pairing_code_without_llm(policy) -> None:
    unknown = PermissionSnapshot("qqbot", "user-openid", "unknown", "user", False, False)
    decision = make_processor(FakeClient(unknown), policy).process(message("hello"))
    assert decision.action == "skip"
    assert "ABCDEFGH" in (decision.reply or "")


def test_other_platform_is_untouched(policy) -> None:
    snapshot = PermissionSnapshot("qqbot", "user-openid", "blocked", "user", False, False)
    decision = make_processor(FakeClient(snapshot), policy).process(
        message("hello", platform="telegram")
    )
    assert decision.action == "allow"
