"""Internal client for a read-only query service.

The query service owns the upstream provider (URL, credentials, parsing) and
returns a channel-neutral answer plus an optional image URL. The Plugin only
knows the `query_key` declared in its own catalogue, so no provider field ever
reaches the Plugin core.
"""

import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from .models import (
    MatchKeyOption,
    QueryAction,
    QueryInfo,
    QueryParam,
    QueryResult,
)


class QueryServiceError(RuntimeError):
    code = "QUERY_SERVICE_ERROR"


class QueryServiceUnavailable(QueryServiceError):
    code = "QUERY_SERVICE_UNAVAILABLE"


class QueryServiceProtocolError(QueryServiceError):
    code = "QUERY_SERVICE_PROTOCOL_ERROR"


class QueryServiceResponseError(QueryServiceError):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str = "",
        options: tuple[MatchKeyOption, ...] = (),
    ) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code or "HTTP_ERROR"
        self.message = message
        self.options = options


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise QueryServiceProtocolError("response must be an object")
    return value


def _text(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise QueryServiceProtocolError(f"invalid {field}")
    return value


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _optional_http_url(value: Any, field: str) -> str | None:
    text = _optional_text(value, field)
    if text is None:
        return None
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        raise QueryServiceProtocolError(f"invalid {field}")
    return text


def _match_key_options(value: Any) -> tuple[MatchKeyOption, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise QueryServiceProtocolError("invalid options")
    options: list[MatchKeyOption] = []
    for item in value:
        data = _object(item)
        options.append(
            MatchKeyOption(
                key=_text(data.get("key"), "options.key"),
                label=_text(data.get("label"), "options.label"),
                aliases=_text_tuple(data.get("aliases"), "options.aliases"),
            )
        )
    return tuple(options)


class QueryServiceClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        connect_timeout: float = 2,
        read_timeout: float = 4,
        max_response_bytes: int = 65_536,
        catalog_ttl_seconds: float = 300,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_response_bytes = max_response_bytes
        self._catalog_ttl_seconds = catalog_ttl_seconds
        self._clock = clock
        self._catalog: tuple[float, dict[str, QueryInfo]] | None = None
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

    def catalog(self, *, refresh: bool = False) -> dict[str, QueryInfo]:
        cached = self._catalog
        now = self._clock()
        if not refresh and cached is not None and now - cached[0] < self._catalog_ttl_seconds:
            return cached[1]
        data = _object(self._request("GET", "/v1/queries"))
        raw = data.get("queries")
        if not isinstance(raw, list):
            raise QueryServiceProtocolError("queries response must be an array")
        result: dict[str, QueryInfo] = {}
        for item in raw:
            info = _query_info(item)
            result[info.query_key] = info
        if len(result) != len(raw):
            raise QueryServiceProtocolError("queries response contains duplicate keys")
        self._catalog = (now, result)
        return result

    def fetch(self, query_key: str, params: dict[str, list[str]] | None = None) -> QueryResult:
        info = self.catalog().get(query_key)
        if info is None:
            info = self.catalog(refresh=True).get(query_key)
        if info is None:
            raise QueryServiceResponseError(404, "QUERY_NOT_FOUND", "未知查询")
        query = [
            (name, value) for name, values in (params or {}).items() for value in values if value
        ]
        payload = self._request(
            "GET",
            f"/v1/queries/{quote(info.route or info.query_key, safe='')}",
            params=query or None,
        )
        return _query_result(payload)

    def _request(self, method: str, path: str, *, params: Any = None) -> Any:
        try:
            response = self._client.request(method, path, params=params)
        except httpx.TransportError as exc:
            raise QueryServiceUnavailable("query service request failed") from exc
        if len(response.content) > self._max_response_bytes:
            raise QueryServiceProtocolError("response exceeds configured size limit")
        if not 200 <= response.status_code < 300:
            try:
                error = _object(response.json())
            except (ValueError, QueryServiceProtocolError):
                error = {}
            raise QueryServiceResponseError(
                response.status_code,
                str(error.get("code") or "HTTP_ERROR"),
                str(error.get("message") or ""),
                _safe_options(error.get("options")),
            )
        try:
            return response.json()
        except ValueError as exc:
            raise QueryServiceProtocolError("response is not valid JSON") from exc


def _safe_options(value: Any) -> tuple[MatchKeyOption, ...]:
    try:
        return _match_key_options(value)
    except QueryServiceProtocolError:
        return ()


def _query_info(value: Any) -> QueryInfo:
    data = _object(value)
    return QueryInfo(
        query_key=_text(data.get("query_key"), "query_key"),
        display_name=_text(data.get("display_name"), "display_name"),
        description=_text(data.get("description"), "description", allow_empty=True),
        route=_text(data.get("route"), "route", allow_empty=True),
        params=_query_params(data.get("params")),
        actions=_query_actions(data.get("actions")),
    )


def _query_params(value: Any) -> tuple[QueryParam, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise QueryServiceProtocolError("invalid params")
    params: list[QueryParam] = []
    for item in value:
        data = _object(item)
        kind = _text(data.get("type"), "params.type")
        if kind not in {"text", "enum"}:
            raise QueryServiceProtocolError("unsupported parameter type")
        params.append(
            QueryParam(
                name=_text(data.get("name"), "params.name"),
                label=_text(data.get("label"), "params.label"),
                type=kind,
                options=_match_key_options(data.get("options")),
                required=bool(data.get("required")),
                multiple=bool(data.get("multiple")),
                max_items=_positive_int(data.get("max_items")),
                max_length=_positive_int(data.get("max_length")) or 64,
                default=str(data.get("default") or ""),
            )
        )
    return tuple(params)


def _query_actions(value: Any) -> tuple[QueryAction, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise QueryServiceProtocolError("invalid actions")
    actions: list[QueryAction] = []
    for item in value:
        data = _object(item)
        actions.append(
            QueryAction(
                key=_text(data.get("key"), "actions.key"),
                label=str(data.get("label") or ""),
                aliases=_text_tuple(data.get("aliases"), "actions.aliases"),
                params=_text_tuple(data.get("params"), "actions.params"),
                default=bool(data.get("default")),
            )
        )
    return tuple(actions)


def _text_tuple(value: Any, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise QueryServiceProtocolError(f"invalid {field}")
    return tuple(value)


def _positive_int(value: Any) -> int:
    return value if isinstance(value, int) and value > 0 else 0


def _query_result(value: Any) -> QueryResult:
    data = _object(value)
    return QueryResult(
        query_key=_text(data.get("query_key"), "query_key"),
        title=_text(data.get("title"), "title"),
        text=_text(data.get("text"), "text"),
        image_url=_optional_http_url(data.get("image_url"), "image_url"),
    )
