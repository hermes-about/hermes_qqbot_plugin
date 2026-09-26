import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from autoqq_business_plugin.hermes_sender import GatewaySender
from autoqq_business_plugin.models import DeliveryItem, MessageIdentity


class Adapter:
    def __init__(self) -> None:
        self.calls = []

    async def send(self, chat_id, text):
        self.calls.append(("text", chat_id, text))
        return SimpleNamespace(success=True, message_id="message-1", raw_response={})

    async def send_image(self, chat_id, image_url, caption=None):
        self.calls.append(("image", chat_id, image_url, caption))
        return SimpleNamespace(success=True, message_id="message-1", raw_response={})


def test_gateway_sender_handles_reply_and_worker_thread_delivery() -> None:
    async def scenario() -> None:
        adapter = Adapter()
        sender = GatewaySender(1)
        sender.bind_adapter("qqbot", adapter)
        identity = MessageIdentity("qqbot", "user", "chat", "dm", "/help")
        sender.reply(identity, "reply")
        group_identity = MessageIdentity("qqbot", "member-openid", "group", "group", "/bind x")
        sender.reply(group_identity, "已订阅。", mention_sender=True)
        sender.reply_image(identity, "https://example.invalid/image.png")
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
        group_item = DeliveryItem(
            "group-delivery",
            "l" * 32,
            "event",
            "demo.event.changed",
            "qqbot",
            "member-openid",
            {"text": "group notice"},
            1,
            datetime.now(UTC),
            chat_type="group",
            chat_id="group-openid",
        )
        group_outcome = await asyncio.to_thread(sender.send_delivery, group_item)
        assert outcome.success is True
        assert outcome.message_id == "message-1"
        assert group_outcome.success is True
        assert adapter.calls == [
            ("text", "chat", "reply"),
            (
                "text",
                "group",
                '<qqbot-at-user id="member-openid" /> 已订阅。',
            ),
            ("image", "chat", "https://example.invalid/image.png", None),
            ("text", "target", "notice"),
            (
                "text",
                "group-openid",
                '<qqbot-at-user id="member-openid" /> group notice',
            ),
        ]

    asyncio.run(scenario())


def test_gateway_sender_falls_back_to_text_for_an_older_adapter() -> None:
    class TextAdapter:
        def __init__(self) -> None:
            self.calls = []

        async def send(self, chat_id, text):
            self.calls.append((chat_id, text))
            return SimpleNamespace(success=True, message_id="message-1", raw_response={})

    async def scenario() -> None:
        adapter = TextAdapter()
        sender = GatewaySender(1)
        sender.bind_adapter("qqbot", adapter)
        identity = MessageIdentity("qqbot", "user", "chat", "dm", "/wf 地球")
        sender.reply_image(identity, "https://example.invalid/image.png")
        await asyncio.sleep(0)
        assert adapter.calls == [("chat", "https://example.invalid/image.png")]

    asyncio.run(scenario())
