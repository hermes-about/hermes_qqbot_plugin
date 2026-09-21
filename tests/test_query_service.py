import json
from pathlib import Path

import httpx
import pytest
from test_config_policy_identity import valid_env

from autoqq_business_plugin.config import ConfigurationError, Settings
from autoqq_business_plugin.query_catalog import QueryCatalog, QueryCatalogError
from autoqq_business_plugin.query_client import (
    QueryServiceClient,
    QueryServiceProtocolError,
    QueryServiceResponseError,
    QueryServiceUnavailable,
)

QUERY_KEY = "warframe.cetus.bounties"

CATALOGUE = {
    "queries": [
        {
            "query_key": QUERY_KEY,
            "route": "cetus-bounties",
            "display_name": "希图斯赏金（地球）",
            "description": "当前与下一轮希图斯赏金",
            "params": [
                {
                    "name": "job",
                    "label": "赏金任务",
                    "type": "enum",
                    "required": False,
                    "multiple": True,
                    "max_items": 8,
                    "options": [
                        {
                            "key": "RescueBountyResc",
                            "label": "搜索并救援",
                            "aliases": ["救援"],
                        }
                    ],
                }
            ],
            "actions": [
                {
                    "key": "jobs",
                    "label": "赏金任务筛选",
                    "aliases": ["任务"],
                    "params": ["job"],
                    "default": True,
                }
            ],
        }
    ]
}


def response(status: int, payload) -> httpx.Response:
    return httpx.Response(
        status, content=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )


def client(handler, **kwargs) -> QueryServiceClient:
    return QueryServiceClient(
        "http://wfdata-publisher:8081",
        "q" * 32,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def write_catalogue(tmp_path: Path, payload) -> Path:
    path = tmp_path / "queries.yaml"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_query_settings_are_optional_but_validated() -> None:
    assert Settings.from_env(valid_env()).query_service_url == ""
    configured = Settings.from_env(
        valid_env()
        | {
            "QUERY_SERVICE_URL": "http://wfdata-publisher:8081/",
            "QUERY_SERVICE_TOKEN": "q" * 32,
        }
    )
    assert configured.query_service_url == "http://wfdata-publisher:8081"
    assert configured.query_read_timeout_seconds == 4.0


@pytest.mark.parametrize(
    "update",
    [
        {"QUERY_SERVICE_URL": "wfdata-publisher:8081"},
        {"QUERY_SERVICE_URL": "http://user@wfdata-publisher:8081", "QUERY_SERVICE_TOKEN": "q" * 32},
        {"QUERY_SERVICE_URL": "http://wfdata-publisher:8081", "QUERY_SERVICE_TOKEN": "short"},
        {"QUERY_SERVICE_TOKEN": "q" * 32},
        {
            "QUERY_SERVICE_URL": "http://wfdata-publisher:8081",
            "QUERY_SERVICE_TOKEN": "q" * 32,
            "QUERY_REPLY_MAX_CHARS": "10",
        },
    ],
)
def test_invalid_query_settings_are_rejected(update: dict[str, str]) -> None:
    with pytest.raises(ConfigurationError):
        Settings.from_env(valid_env() | update)


def test_query_catalogue_file_is_a_controlled_whitelist(tmp_path: Path) -> None:
    path = write_catalogue(
        tmp_path,
        {
            "namespaces": {
                "wf": {
                    "display_name": "Warframe 数据查询",
                    "description": "按需查询",
                    "targets": [
                        {
                            "query_key": QUERY_KEY,
                            "aliases": ["地球", "cetus"],
                            "reply": "image_url",
                        }
                    ],
                }
            }
        },
    )
    catalogue = QueryCatalog.from_file(path)
    namespace = catalogue.for_command("/wf")
    assert namespace is not None
    assert namespace.command == "/wf"
    assert namespace.resolve("地球") is not None
    assert namespace.resolve("CETUS") is not None
    assert namespace.resolve(QUERY_KEY) is not None
    assert namespace.resolve("火星") is None
    assert namespace.targets[0].reply == "image_url"
    assert catalogue.for_command("/nope") is None


def test_query_catalogue_defaults_to_the_text_reply(tmp_path: Path) -> None:
    path = write_catalogue(
        tmp_path,
        {"namespaces": {"wf": {"targets": [{"query_key": QUERY_KEY, "aliases": ["地球"]}]}}},
    )
    namespace = QueryCatalog.from_file(path).for_command("/wf")
    assert namespace is not None
    assert namespace.targets[0].reply == "text"


@pytest.mark.parametrize(
    "namespaces",
    [
        {"WF": {"targets": [{"query_key": QUERY_KEY, "aliases": ["地球"]}]}},
        {"wf": {"targets": []}},
        {"wf": {"targets": [{"query_key": "bad key", "aliases": ["地球"]}]}},
        {"wf": {"targets": [{"query_key": QUERY_KEY, "aliases": ["help"]}]}},
        {"wf": {"targets": [{"query_key": QUERY_KEY, "aliases": ["地球"], "reply": "image"}]}},
        {
            "wf": {
                "targets": [
                    {"query_key": QUERY_KEY, "aliases": ["地球"]},
                    {"query_key": "warframe.cetus.other", "aliases": ["地球"]},
                ]
            }
        },
    ],
)
def test_invalid_query_catalogues_are_rejected(tmp_path: Path, namespaces: dict) -> None:
    path = write_catalogue(tmp_path, {"namespaces": namespaces})
    with pytest.raises(QueryCatalogError):
        QueryCatalog.from_file(path)


def test_catalogue_is_fetched_with_the_service_token_and_cached() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return response(200, CATALOGUE)

    clock = [0.0]
    query = client(handler, catalog_ttl_seconds=60, clock=lambda: clock[0])
    first = query.catalog()
    assert seen[0].url.path == "/v1/queries"
    assert seen[0].headers["Authorization"] == f"Bearer {'q' * 32}"
    assert first[QUERY_KEY].route == "cetus-bounties"
    job = first[QUERY_KEY].param("job")
    assert job is not None
    assert job.multiple is True and job.max_items == 8
    assert job.option_key_for("搜索并救援") == "RescueBountyResc"
    assert job.option_key_for("救援") == "RescueBountyResc"
    assert job.option_key_for("不存在") is None
    assert first[QUERY_KEY].default_action().key == "jobs"
    assert first[QUERY_KEY].action_for("任务").key == "jobs"
    clock[0] = 30.0
    query.catalog()
    assert len(seen) == 1
    clock[0] = 61.0
    query.catalog()
    assert len(seen) == 2


def test_fetch_uses_the_route_and_repeats_named_parameters() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/queries":
            return response(200, CATALOGUE)
        return response(
            200,
            {
                "query_key": QUERY_KEY,
                "title": "希图斯赏金（地球）",
                "text": "希图斯赏金（地球）",
                "image_url": "https://wf-data.example/image",
            },
        )

    result = client(handler).fetch(QUERY_KEY, {"job": ["RescueBountyResc", "SabotageBountySab"]})
    assert result.image_url == "https://wf-data.example/image"
    assert seen[1].url.path == "/v1/queries/cetus-bounties"
    assert seen[1].url.params.get_list("job") == [
        "RescueBountyResc",
        "SabotageBountySab",
    ]


def test_fetch_without_parameters_sends_no_query_string() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/queries":
            return response(200, CATALOGUE)
        return response(200, {"query_key": QUERY_KEY, "title": "t", "text": "t"})

    client(handler).fetch(QUERY_KEY)
    assert seen[1].url.query == b""


@pytest.mark.parametrize("image_url", ["file:///etc/passwd", "/tmp/image.png", "not-a-url"])
def test_query_result_rejects_non_http_image_sources(image_url: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/queries":
            return response(200, CATALOGUE)
        return response(
            200,
            {"query_key": QUERY_KEY, "title": "t", "text": "t", "image_url": image_url},
        )

    with pytest.raises(QueryServiceProtocolError):
        client(handler).fetch(QUERY_KEY)


def test_unknown_query_key_is_reported_without_a_second_guess() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return response(200, CATALOGUE)

    with pytest.raises(QueryServiceResponseError) as raised:
        client(handler).fetch("warframe.nope")
    assert raised.value.status_code == 404


def test_service_error_body_is_preserved_as_a_typed_response() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return response(
            400,
            {
                "code": "QUERY_OPTION_UNKNOWN",
                "message": "不在可选范围内的参数：Nope",
                "options": [{"key": "RescueBountyResc", "label": "搜索并救援"}],
            },
        )

    with pytest.raises(QueryServiceResponseError) as raised:
        client(handler).fetch(QUERY_KEY)
    assert raised.value.status_code == 400
    assert raised.value.code == "QUERY_OPTION_UNKNOWN"
    assert raised.value.options[0].label == "搜索并救援"


def test_transport_failures_are_unavailable_not_protocol_errors() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(QueryServiceUnavailable):
        client(handler).catalog()


def test_oversized_responses_are_rejected() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return response(200, {"queries": [], "padding": "x" * 2000})

    with pytest.raises(QueryServiceProtocolError):
        client(handler, max_response_bytes=1024).catalog()


def test_unsupported_parameter_type_is_a_protocol_error() -> None:
    catalogue = {
        "queries": [
            {
                "query_key": QUERY_KEY,
                "route": "cetus-bounties",
                "display_name": "希图斯赏金（地球）",
                "description": "",
                "params": [{"name": "weapon", "label": "武器", "type": "number"}],
            }
        ]
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return response(200, catalogue)

    with pytest.raises(QueryServiceProtocolError):
        client(handler).catalog()
