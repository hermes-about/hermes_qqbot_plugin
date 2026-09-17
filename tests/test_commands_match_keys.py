from conftest import FakeClient, make_processor, message

from autoqq_business_plugin.models import PermissionSnapshot, SubscriptionInfo

ACTIVE = PermissionSnapshot("qqbot", "user-openid", "active", "user", False, True)
BOUNTY = "warframe.cetus.bounty_current"


def test_events_marks_which_events_have_watchable_options(policy) -> None:
    decision = make_processor(FakeClient(ACTIVE), policy).process(message("/events"))
    assert decision.reason == "command-handled"
    reply = decision.reply or ""
    assert f"- {BOUNTY}：Cetus 当前轮次赏金（可关注 2 项，必选）" in reply
    assert "- demo.event.changed：示例事件\n" in reply


def test_events_with_event_key_lists_options(policy) -> None:
    decision = make_processor(FakeClient(ACTIVE), policy).process(message(f"/events {BOUNTY}"))
    reply = decision.reply or ""
    assert "RescueBountyResc：搜索并救援" in reply
    assert "ReclamationBountyCap：捕获 Grineer 特工" in reply
    assert f"用法：/bind {BOUNTY} <关注项[,关注项]>" in reply


def test_events_with_unknown_key_is_reported_without_calling_eventserver(policy) -> None:
    client = FakeClient(ACTIVE)
    decision = make_processor(client, policy).process(message("/events no.such.event"))
    assert "没有找到事件 no.such.event" in (decision.reply or "")


def test_bind_with_watch_keys_reports_labels_and_passes_them_through(policy) -> None:
    client = FakeClient(ACTIVE)
    decision = make_processor(client, policy).process(
        message(f"/bind {BOUNTY} RescueBountyResc,ReclamationBountyCap")
    )
    assert decision.reason == "command-handled"
    assert (
        "bind",
        "qqbot",
        "user-openid",
        BOUNTY,
        ("RescueBountyResc", "ReclamationBountyCap"),
    ) in client.calls
    assert "已订阅" in (decision.reply or "")
    assert "搜索并救援" in (decision.reply or "")


def test_bind_accepts_separate_arguments_and_deduplicates(policy) -> None:
    client = FakeClient(ACTIVE)
    make_processor(client, policy).process(
        message(f"/bind {BOUNTY} RescueBountyResc RescueBountyResc")
    )
    assert (
        "bind",
        "qqbot",
        "user-openid",
        BOUNTY,
        ("RescueBountyResc",),
    ) in client.calls


def test_bind_rejects_an_unknown_watch_key_locally(policy) -> None:
    client = FakeClient(ACTIVE)
    decision = make_processor(client, policy).process(message(f"/bind {BOUNTY} NotInCatalog"))
    assert "不在该事件的可选范围内" in (decision.reply or "")
    assert not [call for call in client.calls if call[0] == "bind"]


def test_bind_requires_watch_keys_when_the_catalog_demands_them(policy) -> None:
    client = FakeClient(ACTIVE)
    decision = make_processor(client, policy).process(message(f"/bind {BOUNTY}"))
    assert "必须指定至少一个关注项" in (decision.reply or "")
    assert not [call for call in client.calls if call[0] == "bind"]


def test_bind_without_options_keeps_the_simple_reply(policy) -> None:
    decision = make_processor(FakeClient(ACTIVE), policy).process(
        message("/bind demo.event.changed")
    )
    assert "已订阅 demo.event.changed" in (decision.reply or "")


def test_bind_reports_an_updated_watch_list(policy) -> None:
    client = FakeClient(ACTIVE)
    client.updated = True
    decision = make_processor(client, policy).process(message(f"/bind {BOUNTY} RescueBountyResc"))
    assert "已更新" in (decision.reply or "")
    assert "搜索并救援" in (decision.reply or "")


def test_bindings_shows_watched_keys(policy) -> None:
    client = FakeClient(ACTIVE)
    client.subscriptions = [
        SubscriptionInfo(BOUNTY, "zh-CN", match_keys=("RescueBountyResc",)),
        SubscriptionInfo("demo.event.changed", "zh-CN"),
    ]
    decision = make_processor(client, policy).process(message("/bindings"))
    reply = decision.reply or ""
    assert f"- {BOUNTY}：关注 RescueBountyResc" in reply
    assert "- demo.event.changed：关注全部" in reply


def test_help_mentions_watch_keys(policy) -> None:
    reply = make_processor(FakeClient(ACTIVE), policy).process(message("/help")).reply or ""
    assert "/bind <event_key> [关注项...]" in reply
    assert "/events <event_key>" in reply
