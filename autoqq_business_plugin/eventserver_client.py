import time
from collections.abc import Callable
from datetime import datetime
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import httpx

from .models import (
    BindingContext,
    DeliveryItem,
    EventInfo,
    MatchKeyOption,
    PermissionSnapshot,
    SubscriptionInfo,
)


class EventServerError(RuntimeError):
    code = "EVENTSERVER_ERROR"


class EventServerUnavailable(EventServerError):
    code = "EVENTSERVER_UNAVAILABLE"


class EventServerProtocolError(EventServerError):
    code = "EVENTSERVER_PROTOCOL_ERROR"


class EventServerResponseError(EventServerError):
    def __init__(self, status_code: int, code: str, request_id: str = "") -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code or "HTTP_ERROR"
        self.request_id = request_id


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EventServerProtocolError("response must be an object")
    return value


def _text(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise EventServerProtocolError(f"invalid {field}")
    return value


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise EventServerProtocolError(f"invalid {field}")
    return value


def _optional_text(value: Any, field: str) -> str | None:
    return None if value is None else _text(value, field)


def _optional_boolean(value: Any, field: str) -> bool:
    return False if value is None else _boolean(value, field)


def _match_keys(value: Any, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise EventServerProtocolError(f"invalid {field}")
    return tuple(value)


def _bind_scopes(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise EventServerProtocolError("invalid permissions.bind")
    return tuple(value)


def _match_key_options(value: Any) -> tuple[MatchKeyOption, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise EventServerProtocolError("invalid match_key_options")
    options: list[MatchKeyOption] = []
    for item in value:
        data = _object(item)
        options.append(
            MatchKeyOption(
                key=_text(data.get("key"), "match_key_options.key"),
                label=_text(data.get("label"), "match_key_options.label"),
            )
        )
    return tuple(options)


def _binding_context(value: Any) -> BindingContext | None:
    if value is None:
        return None
    data = _object(value)
    chat_type = _text(data.get("chat_type"), "binding_context.chat_type")
    if chat_type not in {"dm", "group"}:
        raise EventServerProtocolError("invalid binding_context.chat_type")
    return BindingContext(
        chat_type=chat_type,
        chat_id=_text(data.get("chat_id"), "binding_context.chat_id"),
    )


class EventServerClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        connect_timeout: float = 3,
        read_timeout: float = 5,
        safe_read_retries: int = 1,
        max_response_bytes: int = 262_144,
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._safe_read_retries = safe_read_retries
        self._max_response_bytes = max_response_bytes
        self._sleeper = sleeper
        self._client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}", "User-Agent": "autoqq-plugin/0.1"},
            timeout=httpx.Timeout(read_timeout, connect=connect_timeout),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> Any:
        attempts = self._safe_read_retries + 1 if method == "GET" else 1
        response: httpx.Response | None = None
        for attempt in range(attempts):
            try:
                response = self._client.request(
                    method,
                    path,
                    json=payload,
                    params=params,
                    headers={"X-Request-ID": str(uuid4())},
                )
            except httpx.TransportError as exc:
                if attempt + 1 >= attempts:
                    raise EventServerUnavailable("EventServer request failed") from exc
                self._sleeper(0.05 * (attempt + 1))
                continue
            if response.status_code not in {502, 503, 504} or attempt + 1 >= attempts:
                break
            self._sleeper(0.05 * (attempt + 1))
        if response is None:
            raise EventServerUnavailable("EventServer request failed")
        if len(response.content) > self._max_response_bytes:
            raise EventServerProtocolError("response exceeds configured size limit")
        if not 200 <= response.status_code < 300:
            try:
                error = _object(response.json())
            except (ValueError, EventServerProtocolError):
                error = {}
            raise EventServerResponseError(
                response.status_code,
                str(error.get("code") or "HTTP_ERROR"),
                str(error.get("request_id") or ""),
            )
        try:
            return response.json()
        except ValueError as exc:
            raise EventServerProtocolError("response is not valid JSON") from exc

    @staticmethod
    def _permission(payload: Any) -> PermissionSnapshot:
        data = _object(payload)
        permissions = _object(data.get("permissions"))
        status = _text(data.get("account_status"), "account_status")
        role = _text(data.get("role"), "role")
        if status not in {"unknown", "active", "blocked"} or role not in {"user", "admin"}:
            raise EventServerProtocolError("invalid permission enum")
        return PermissionSnapshot(
            platform=_text(data.get("platform"), "platform"),
            openid=_text(data.get("openid"), "openid"),
            account_status=status,
            role=role,
            chat=_boolean(permissions.get("chat"), "permissions.chat"),
            command=_boolean(permissions.get("command"), "permissions.command"),
            bind=_bind_scopes(permissions.get("bind")),
        )

    def get_permission(self, platform: str, openid: str) -> PermissionSnapshot:
        payload = self._request(
            "GET",
            f"/v1/users/{quote(openid, safe='')}/permission",
            params={"platform": platform},
        )
        return self._permission(payload)

    def list_events(self) -> list[EventInfo]:
        payload = self._request("GET", "/v1/events")
        if not isinstance(payload, list):
            raise EventServerProtocolError("events response must be an array")
        result: list[EventInfo] = []
        for item in payload:
            data = _object(item)
            result.append(
                EventInfo(
                    event_key=_text(data.get("event_key"), "event_key"),
                    display_name=_text(data.get("display_name"), "display_name"),
                    description=_text(data.get("description"), "description", allow_empty=True),
                    deprecated=_boolean(data.get("deprecated"), "deprecated"),
                    match_key_field=_optional_text(data.get("match_key_field"), "match_key_field"),
                    match_keys_required=_optional_boolean(
                        data.get("match_keys_required"), "match_keys_required"
                    ),
                    match_key_options=_match_key_options(data.get("match_key_options")),
                )
            )
        return result

    def list_subscriptions(self, platform: str, openid: str) -> list[SubscriptionInfo]:
        payload = self._request(
            "GET",
            f"/v1/users/{quote(openid, safe='')}/subscriptions",
            params={"platform": platform},
        )
        if not isinstance(payload, list):
            raise EventServerProtocolError("subscriptions response must be an array")
        return [
            SubscriptionInfo(
                event_key=_text(_object(item).get("event_key"), "event_key"),
                locale=_text(_object(item).get("locale"), "locale"),
                match_keys=_match_keys(_object(item).get("match_keys"), "match_keys"),
                binding_context=_binding_context(_object(item).get("binding_context")),
            )
            for item in payload
        ]

    def subscribe(
        self,
        platform: str,
        openid: str,
        event_key: str,
        match_keys: tuple[str, ...] | None = None,
        binding_context: BindingContext | None = None,
    ) -> SubscriptionInfo:
        body: dict[str, Any] = {"event_key": event_key, "match_keys": list(match_keys or ())}
        if binding_context is not None:
            body["binding_context"] = {
                "chat_type": binding_context.chat_type,
                "chat_id": binding_context.chat_id,
            }
        data = _object(
            self._request(
                "POST",
                f"/v1/users/{quote(openid, safe='')}/subscriptions",
                payload=body,
                params={"platform": platform},
            )
        )
        created = _boolean(data.get("created"), "created")
        updated = _optional_boolean(data.get("updated"), "updated")
        return SubscriptionInfo(
            event_key=_text(data.get("event_key"), "event_key"),
            locale=_text(data.get("locale"), "locale"),
            changed=created or updated,
            match_keys=_match_keys(data.get("match_keys"), "match_keys"),
            updated=updated,
            binding_context=_binding_context(data.get("binding_context")),
        )

    def unsubscribe(self, platform: str, openid: str, event_key: str) -> SubscriptionInfo:
        data = _object(
            self._request(
                "DELETE",
                f"/v1/users/{quote(openid, safe='')}/subscriptions/{quote(event_key, safe='')}",
                params={"platform": platform},
            )
        )
        return SubscriptionInfo(
            event_key=_text(data.get("event_key"), "event_key"),
            locale="",
            changed=_boolean(data.get("deleted"), "deleted"),
        )

    def create_pairing_code(self, platform: str, openid: str) -> tuple[str, datetime]:
        data = _object(
            self._request(
                "POST", "/v1/pairing-codes", payload={"platform": platform, "openid": openid}
            )
        )
        code = _text(data.get("code"), "code")
        try:
            expires_at = datetime.fromisoformat(_text(data.get("expires_at"), "expires_at"))
        except ValueError as exc:
            raise EventServerProtocolError("invalid expires_at") from exc
        return code, expires_at

    def grant(
        self, platform: str, target: str, permissions: list[str], operator: str
    ) -> PermissionSnapshot:
        return self._permission(
            self._request(
                "POST",
                f"/v1/users/{quote(target, safe='')}/grant",
                payload={"permissions": permissions, "operator_openid": operator},
                params={"platform": platform},
            )
        )

    def grant_bind(
        self, platform: str, target: str, scopes: list[str], operator: str
    ) -> PermissionSnapshot:
        return self._permission(
            self._request(
                "POST",
                f"/v1/users/{quote(target, safe='')}/grant",
                payload={"permissions": [], "bind": scopes, "operator_openid": operator},
                params={"platform": platform},
            )
        )

    def approve_pairing(
        self, platform: str, code: str, permissions: list[str], operator: str
    ) -> PermissionSnapshot:
        return self._permission(
            self._request(
                "POST",
                f"/v1/pairing-codes/{quote(code, safe='')}/approve",
                payload={
                    "permissions": permissions,
                    "operator_openid": operator,
                    "platform": platform,
                },
            )
        )

    def revoke(
        self, platform: str, target: str, permissions: list[str], operator: str
    ) -> PermissionSnapshot:
        return self._permission(
            self._request(
                "POST",
                f"/v1/users/{quote(target, safe='')}/revoke",
                payload={"permissions": permissions, "operator_openid": operator},
                params={"platform": platform},
            )
        )

    def revoke_bind(
        self,
        platform: str,
        target: str,
        scopes: list[str],
        operator: str,
        *,
        clear_all: bool = False,
    ) -> PermissionSnapshot:
        return self._permission(
            self._request(
                "POST",
                f"/v1/users/{quote(target, safe='')}/revoke",
                payload={
                    "permissions": [],
                    "bind": scopes,
                    "clear_bind": clear_all,
                    "operator_openid": operator,
                },
                params={"platform": platform},
            )
        )

    def change_role(
        self, platform: str, target: str, role: str, operator: str
    ) -> PermissionSnapshot:
        return self._permission(
            self._request(
                "POST",
                f"/v1/users/{quote(target, safe='')}/role",
                payload={"role": role, "operator_openid": operator},
                params={"platform": platform},
            )
        )

    def claim_deliveries(
        self, worker_id: str, platform: str, limit: int, lease_seconds: int
    ) -> list[DeliveryItem]:
        payload = self._request(
            "POST",
            "/v1/deliveries/claim",
            payload={
                "worker_id": worker_id,
                "platform": platform,
                "limit": limit,
                "lease_seconds": lease_seconds,
            },
        )
        raw_items = _object(payload).get("items")
        if not isinstance(raw_items, list):
            raise EventServerProtocolError("delivery items must be an array")
        result: list[DeliveryItem] = []
        for item in raw_items:
            data = _object(item)
            target = _object(data.get("target"))
            message = _object(data.get("message"))
            target_openid = _text(target.get("openid"), "target.openid")
            chat_type = _optional_text(target.get("chat_type"), "target.chat_type") or "dm"
            if chat_type not in {"dm", "group"}:
                raise EventServerProtocolError("invalid target.chat_type")
            chat_id = _optional_text(target.get("chat_id"), "target.chat_id") or target_openid
            try:
                created_at = datetime.fromisoformat(_text(data.get("created_at"), "created_at"))
                attempt = int(data.get("attempt"))
            except (TypeError, ValueError) as exc:
                raise EventServerProtocolError("invalid delivery timestamp or attempt") from exc
            result.append(
                DeliveryItem(
                    delivery_id=_text(data.get("delivery_id"), "delivery_id"),
                    lease_token=_text(data.get("lease_token"), "lease_token"),
                    event_id=_text(data.get("event_id"), "event_id"),
                    event_key=_text(data.get("event_key"), "event_key"),
                    platform=_text(target.get("platform"), "target.platform"),
                    openid=target_openid,
                    message=message,
                    attempt=attempt,
                    created_at=created_at,
                    chat_type=chat_type,
                    chat_id=chat_id,
                )
            )
        return result

    def ack_delivery(
        self, delivery_id: str, lease_token: str, platform_message_id: str | None
    ) -> None:
        payload: dict[str, Any] = {"lease_token": lease_token}
        if platform_message_id:
            payload["platform_message_id"] = platform_message_id
        self._request("POST", f"/v1/deliveries/{quote(delivery_id, safe='')}/ack", payload=payload)

    def fail_delivery(
        self, delivery_id: str, lease_token: str, error_code: str, retryable: bool
    ) -> None:
        self._request(
            "POST",
            f"/v1/deliveries/{quote(delivery_id, safe='')}/fail",
            payload={
                "lease_token": lease_token,
                "error_code": error_code,
                "retryable": retryable,
            },
        )
