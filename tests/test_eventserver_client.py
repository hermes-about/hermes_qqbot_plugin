import json
from datetime import UTC, datetime

import httpx
import pytest

from autoqq_business_plugin.eventserver_client import (
    EventServerClient,
    EventServerProtocolError,
    EventServerResponseError,
)


def response(status: int, payload) -> httpx.Response:
    return httpx.Response(
        status, content=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )


def test_permission_request_matches_eventserver_contract() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return response(
            200,
            {
                "platform": "qqbot",
                "openid": "user/id",
                "account_status": "active",
                "role": "user",
                "permissions": {"chat": True, "command": False},
            },
        )

    client = EventServerClient(
        "http://eventserver:8080", "x" * 32, transport=httpx.MockTransport(handler)
    )
    result = client.get_permission("qqbot", "user/id")
    assert result.chat is True and result.command is False
    assert seen[0].url.path == "/v1/users/user/id/permission"
    assert seen[0].url.params["platform"] == "qqbot"
    assert seen[0].headers["Authorization"] == f"Bearer {'x' * 32}"
    assert seen[0].headers["X-Request-ID"]


def test_claim_and_ack_match_implemented_contract() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/claim"):
            return response(
                200,
                {
                    "items": [
                        {
                            "delivery_id": "delivery-1",
                            "lease_token": "l" * 32,
                            "event_id": "event-1",
                            "event_key": "demo.event.changed",
                            "target": {"platform": "qqbot", "openid": "target-openid"},
                            "message": {"text": "hello"},
                            "attempt": 1,
                            "created_at": datetime.now(UTC).isoformat(),
                        }
                    ]
                },
            )
        return response(
            200,
            {
                "delivery_id": "delivery-1",
                "status": "sent",
                "attempts": 1,
                "next_attempt_at": datetime.now(UTC).isoformat(),
            },
        )

    client = EventServerClient(
        "http://eventserver:8080", "x" * 32, transport=httpx.MockTransport(handler)
    )
    item = client.claim_deliveries("worker-1", "qqbot", 10, 60)[0]
    client.ack_delivery(item.delivery_id, item.lease_token, "message-1")
    claim_body = json.loads(requests[0].content)
    ack_body = json.loads(requests[1].content)
    assert claim_body == {
        "worker_id": "worker-1",
        "platform": "qqbot",
        "limit": 10,
        "lease_seconds": 60,
    }
    assert ack_body == {"lease_token": "l" * 32, "platform_message_id": "message-1"}


def test_error_shape_is_preserved_without_sensitive_message() -> None:
    client = EventServerClient(
        "http://eventserver:8080",
        "x" * 32,
        transport=httpx.MockTransport(
            lambda _request: response(
                403,
                {"code": "FORBIDDEN", "message": "sensitive detail", "request_id": "req-1"},
            )
        ),
    )
    with pytest.raises(EventServerResponseError) as captured:
        client.list_events()
    assert captured.value.code == "FORBIDDEN"
    assert "sensitive detail" not in str(captured.value)


def test_write_requests_are_not_retried() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return response(503, {"code": "NOT_READY", "message": "down", "request_id": "r"})

    client = EventServerClient(
        "http://eventserver:8080",
        "x" * 32,
        safe_read_retries=3,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(EventServerResponseError):
        client.subscribe("qqbot", "user-openid", "demo.event.changed")
    assert calls == 1


def test_oversized_or_malformed_response_is_rejected() -> None:
    client = EventServerClient(
        "http://eventserver:8080",
        "x" * 32,
        max_response_bytes=20,
        transport=httpx.MockTransport(lambda _request: response(200, {"items": ["x" * 100]})),
    )
    with pytest.raises(EventServerProtocolError):
        client.claim_deliveries("worker", "qqbot", 1, 60)
