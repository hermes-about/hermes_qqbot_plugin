import asyncio
from types import SimpleNamespace

from conftest import message

from autoqq_business_plugin.config import Settings
from autoqq_business_plugin.models import DispatchDecision
from autoqq_business_plugin.plugin import PluginRuntime


class StubSender:
    def __init__(self, *, fail=False) -> None:
        self.fail = fail
        self.bound = []
        self.replies = []
        self.image_replies = []

    def bind(self, gateway, platform) -> None:
        if self.fail:
            raise RuntimeError("bind failed")
        self.bound.append((gateway, platform))

    def bind_adapter(self, platform, adapter) -> None:
        self.bound.append((platform, adapter))

    def reply(self, identity, text) -> None:
        self.replies.append((identity, text))

    def reply_image(self, identity, image_url, *, caption=None) -> None:
        self.image_replies.append((identity, image_url, caption))


class StubWorker:
    def __init__(self) -> None:
        self.started = 0

    def start(self) -> bool:
        self.started += 1
        return self.started == 1


def runtime(decision: DispatchDecision, *, sender=None) -> PluginRuntime:
    value = PluginRuntime.__new__(PluginRuntime)
    value.settings = Settings("http://eventserver", "x" * 32, delivery_worker_id="worker")
    value.sender = sender or StubSender()
    value.worker = StubWorker()
    value.processor = SimpleNamespace(process=lambda _event: decision)
    return value


def test_hook_maps_core_allow_and_skip_and_schedules_reply() -> None:
    async def scenario() -> None:
        gateway = SimpleNamespace(adapters={})
        allowed = runtime(DispatchDecision("allow", "ok"))
        assert allowed.hook(message("hello"), gateway) == {"action": "allow"}

        skipped = runtime(DispatchDecision("skip", "handled", "reply text"))
        assert skipped.hook(message("/help"), gateway) == {
            "action": "skip",
            "reason": "handled",
        }
        assert skipped.sender.replies[0][1] == "reply text"

    asyncio.run(scenario())


def test_hook_contains_adapter_errors_and_fails_closed() -> None:
    async def scenario() -> None:
        value = runtime(
            DispatchDecision("allow", "would-have-allowed"), sender=StubSender(fail=True)
        )
        assert value.hook(message("hello"), SimpleNamespace(adapters={})) == {
            "action": "skip",
            "reason": "autoqq-internal-error",
        }

    asyncio.run(scenario())


def test_hook_sends_a_structured_image_reply() -> None:
    async def scenario() -> None:
        value = runtime(
            DispatchDecision(
                "skip", "command-handled", image_url="https://example.invalid/image.png"
            )
        )
        assert value.hook(message("/wf 地球"), SimpleNamespace(adapters={})) == {
            "action": "skip",
            "reason": "command-handled",
        }
        assert value.sender.image_replies[0][1:] == (
            "https://example.invalid/image.png",
            None,
        )

    asyncio.run(scenario())


def test_platform_connect_binds_sender_and_starts_worker() -> None:
    async def scenario() -> None:
        value = runtime(DispatchDecision("allow", "ok"))
        adapter = object()
        value.bind_qqbot_adapter(None, adapter)
        assert value.sender.bound == [("qqbot", adapter)]
        assert value.worker.started == 1

    asyncio.run(scenario())


def test_non_qq_platform_does_not_start_delivery_worker() -> None:
    async def scenario() -> None:
        value = runtime(DispatchDecision("allow", "out-of-scope"))
        event = message("hello", platform="telegram")
        assert value.hook(event, SimpleNamespace(adapters={})) == {"action": "allow"}
        assert value.sender.bound == []
        assert value.worker.started == 0

    asyncio.run(scenario())


def test_runtime_wires_the_query_service_and_catalogue() -> None:
    configured = Settings(
        "http://eventserver",
        "x" * 32,
        delivery_worker_id="worker",
        query_service_url="http://wfdata-publisher:8081",
        query_service_token="q" * 32,
    )
    value = PluginRuntime(configured)
    try:
        assert value.query_client is not None
        assert value.processor._commands._queries.for_command("/wf") is not None
    finally:
        value.close()

    plain = PluginRuntime(Settings("http://eventserver", "x" * 32, delivery_worker_id="worker"))
    try:
        assert plain.query_client is None
    finally:
        plain.close()
