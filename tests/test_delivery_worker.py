from datetime import UTC, datetime

from autoqq_business_plugin.delivery_worker import DeliveryWorker
from autoqq_business_plugin.eventserver_client import EventServerUnavailable
from autoqq_business_plugin.models import DeliveryItem, SendOutcome


def delivery(identifier: str = "delivery-1", *, text="hello", platform="qqbot", data=None):
    return DeliveryItem(
        delivery_id=identifier,
        lease_token="l" * 32,
        event_id="event-1",
        event_key="demo.event.changed",
        platform=platform,
        openid="target-openid",
        message={"text": text, "data": data or {}},
        attempt=1,
        created_at=datetime.now(UTC),
    )


class FakeDeliveryClient:
    def __init__(self, items):
        self.items = items
        self.claims = []
        self.acks = []
        self.failures = []

    def claim_deliveries(self, *args):
        self.claims.append(args)
        return list(self.items)

    def ack_delivery(self, *args):
        self.acks.append(args)

    def fail_delivery(self, *args):
        self.failures.append(args)


class FakeSender:
    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or [SendOutcome(True, message_id="message-1")])
        self.sent = []

    def send_delivery(self, item):
        self.sent.append(item)
        return self.outcomes.pop(0)


def worker(client, sender, **kwargs):
    return DeliveryWorker(
        client,
        sender,
        worker_id="worker-1",
        poll_interval_seconds=0.01,
        max_backoff_seconds=0.02,
        jitter=lambda: 0.5,
        **kwargs,
    )


def test_success_is_acked_with_current_lease() -> None:
    client = FakeDeliveryClient([delivery()])
    sender = FakeSender()
    assert worker(client, sender).run_once() == 1
    assert [item.delivery_id for item in sender.sent] == ["delivery-1"]
    assert client.acks == [("delivery-1", "l" * 32, "message-1")]
    assert client.failures == []


def test_send_failure_is_reported_and_next_item_continues() -> None:
    client = FakeDeliveryClient([delivery("one"), delivery("two")])
    sender = FakeSender(
        [
            SendOutcome(False, error_code="QQ_RATE_LIMIT", retryable=True),
            SendOutcome(True, message_id="message-2"),
        ]
    )
    worker(client, sender).run_once()
    assert client.failures == [("one", "l" * 32, "QQ_RATE_LIMIT", True)]
    assert client.acks == [("two", "l" * 32, "message-2")]


def test_invalid_target_or_message_is_dead_lettered_without_send() -> None:
    items = [delivery("wrong-platform", platform="telegram"), delivery("too-long", text="x" * 11)]
    client = FakeDeliveryClient(items)
    sender = FakeSender()
    worker(client, sender, max_message_chars=10).run_once()
    assert sender.sent == []
    assert client.failures == [
        ("wrong-platform", "l" * 32, "INVALID_TARGET", False),
        ("too-long", "l" * 32, "INVALID_MESSAGE", False),
    ]


def test_writeback_failure_does_not_crash_batch() -> None:
    class FailingWritebackClient(FakeDeliveryClient):
        def ack_delivery(self, *args):
            raise EventServerUnavailable("down")

    client = FailingWritebackClient([delivery()])
    assert worker(client, FakeSender()).run_once() == 1


def test_worker_sends_rendered_subscription_match_text() -> None:
    item = delivery(
        text="【帐篷 A】\n• 搜索并救援\n• 捕获 Grineer 特工",
        data={
            "subscription_match": {"items": [{"key": "RescueBountyResc", "label": "搜索并救援"}]}
        },
    )
    client = FakeDeliveryClient([item])
    sender = FakeSender()

    worker(client, sender).run_once()

    assert sender.sent[0].message["text"] == (
        "【帐篷 A】\n• **搜索并救援** 🔴\n• 捕获 Grineer 特工"
    )


def test_worker_falls_back_to_original_when_highlighting_would_exceed_limit() -> None:
    text = "• 搜索并救援"
    item = delivery(
        text=text,
        data={
            "subscription_match": {"items": [{"key": "RescueBountyResc", "label": "搜索并救援"}]}
        },
    )
    client = FakeDeliveryClient([item])
    sender = FakeSender()

    worker(client, sender, max_message_chars=len(text)).run_once()

    assert sender.sent[0].message["text"] == text
