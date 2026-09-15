import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from autoqq_business_plugin.hermes_sender import GatewaySender
from autoqq_business_plugin.models import DeliveryItem, MessageIdentity


class Adapter:
    def __init__(self) -> None:
        self.calls = []

    async def send(self, chat_id, text):
        self.calls.append((chat_id, text))
        return SimpleNamespace(success=True, message_id="message-1", raw_response={})


def test_gateway_sender_handles_reply_and_worker_thread_delivery() -> None:
    async def scenario() -> None:
        adapter = Adapter()
        sender = GatewaySender(1)
        sender.bind_adapter("qqbot", adapter)
        identity = MessageIdentity("qqbot", "user", "chat", "dm", "/help")
        sender.reply(identity, "reply")
        await asyncio.sleep(0)
        item = DeliveryItem(
            "delivery",
            "l" * 32,
            "event",
            "demo.event.changed",
            "qqbot",
            "target",
            {"text": "notice"},
            1,
            datetime.now(UTC),
        )
        outcome = await asyncio.to_thread(sender.send_delivery, item)
        assert outcome.success is True
        assert outcome.message_id == "message-1"
        assert adapter.calls == [("chat", "reply"), ("target", "notice")]

    asyncio.run(scenario())
