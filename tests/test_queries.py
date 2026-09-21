import json

import pytest
from conftest import FakeClient, make_processor, message

from autoqq_business_plugin.models import (
    AccessPolicy,
    MatchKeyOption,
    PermissionSnapshot,
    QueryAction,
    QueryInfo,
    QueryParam,
    QueryResult,
)
from autoqq_business_plugin.query_catalog import QueryCatalog
from autoqq_business_plugin.query_client import (
    QueryServiceResponseError,
    QueryServiceUnavailable,
)

ACTIVE = PermissionSnapshot("qqbot", "user-openid", "active", "user", False, True)
CETUS_KEY = "warframe.cetus.bounties"
RIVEN_KEY = "warframe.riven.summary"

CATALOGUE = {
    "namespaces": {
        "wf": {
            "display_name": "Warframe 数据查询",
            "description": "按需查询 Warframe 数据。",
            "targets": [
                {
                    "query_key": CETUS_KEY,
                    "aliases": ["地球", "cetus"],
                    "reply": "image_url",
                },
                {
                    "query_key": RIVEN_KEY,
                    "aliases": ["紫卡", "riven"],
                    "reply": "text",
                },
            ],
        }
    }
}

JOBS = (
    MatchKeyOption("RescueBountyResc", "搜索并救援"),
    MatchKeyOption("SabotageBountySab", "破坏原型机"),
)
SHAPES = (
    MatchKeyOption("3P1N", "3正1负", ("3+1",)),
    MatchKeyOption("3P", "3正", ("3+", "3P0N")),
    MatchKeyOption("2P1N", "2正1负", ("2+1",)),
    MatchKeyOption("2P", "2正", ("2+", "2P0N")),
)


def query_catalog(tmp_path) -> QueryCatalog:
    path = tmp_path / "queries.yaml"
    path.write_text(json.dumps(CATALOGUE, ensure_ascii=False), encoding="utf-8")
    return QueryCatalog.from_file(path)


def entries() -> dict[str, QueryInfo]:
    return {
        CETUS_KEY: QueryInfo(
            query_key=CETUS_KEY,
            display_name="希图斯赏金（地球）",
            description="当前与下一轮希图斯赏金",
            route="cetus-bounties",
            params=(
                QueryParam(
                    name="job",
                    label="赏金任务",
                    type="enum",
                    options=JOBS,
                    multiple=True,
                    max_items=8,
                ),
            ),
        ),
        RIVEN_KEY: QueryInfo(
            query_key=RIVEN_KEY,
            display_name="紫卡数据",
            description="某武器在指定紫卡结构下的全部词条上下限",
            route="riven",
            params=(
                QueryParam(
                    name="weapon",
                    label="武器名称",
                    type="text",
                    required=True,
                    max_length=48,
                ),
                QueryParam(
                    name="shape",
                    label="紫卡结构",
                    type="enum",
                    options=SHAPES,
                    default="3P1N",
                ),
            ),
            actions=(
                QueryAction(
                    key="stats",
                    label="词条上下限",
                    aliases=("词条", "上下限"),
                    params=("weapon", "shape"),
                    default=True,
                ),
            ),
        ),
    }


class FakeQueryService:
    def __init__(self, *, catalog_error=None, fetch_error=None) -> None:
        self.calls: list[tuple] = []
        self.entries = entries()
        self.results = {
            CETUS_KEY: QueryResult(
                CETUS_KEY,
                "希图斯赏金（地球）",
                "希图斯赏金（地球）\n当前轮次 09-20 19:48 → 22:18（UTC+08:00）\n- 搜索并救援",
                "https://example.invalid/redacted-signed-url",
            ),
            RIVEN_KEY: QueryResult(
                RIVEN_KEY,
                "托里德 紫卡（3P1N）",
                "托里德（Torid） 步枪 · 倾向 1.3\n3P1N（3正1负） · 满级 rank 8 · roll 0.9~1.1\n"
                "正向词条（2）\n- 暴击几率 164.5~201.1%",
            ),
        }
        self.catalog_error = catalog_error
        self.fetch_error = fetch_error

    def catalog(self, refresh: bool = False):
        if self.catalog_error is not None:
            raise self.catalog_error
        return self.entries

    def fetch(self, query_key: str, params: dict[str, list[str]] | None = None) -> QueryResult:
        self.calls.append((query_key, {k: list(v) for k, v in (params or {}).items()}))
        if self.fetch_error is not None:
            raise self.fetch_error
        return self.results[query_key]


def processor(service, policy, tmp_path, catalog=None, **kwargs):
    options = {
        "query_catalog": catalog if catalog is not None else query_catalog(tmp_path),
        "query_service": service,
    }
    options.update(kwargs)
    return make_processor(FakeClient(ACTIVE), policy, **options)


def test_wf_is_registered_as_a_public_command(policy) -> None:
    assert policy.policy_for("/wf") is AccessPolicy.PUBLIC


def test_wf_help_lists_subjects_and_their_parameters(policy, tmp_path) -> None:
    decision = processor(FakeQueryService(), policy, tmp_path).process(message("/wf help"))
    reply = decision.reply or ""
    assert decision.reason == "command-handled"
    assert "/wf：Warframe 数据查询" in reply
    assert "- 地球、cetus：希图斯赏金（地球）" in reply
    assert "参数：赏金任务（可选）" in reply
    assert "- 紫卡、riven：紫卡数据" in reply
    assert "参数：武器名称（必填）、紫卡结构（可选）" in reply


def test_wf_help_degrades_when_the_service_is_unavailable(policy, tmp_path) -> None:
    service = FakeQueryService(catalog_error=QueryServiceUnavailable("down"))
    decision = processor(service, policy, tmp_path).process(message("/wf help"))
    reply = decision.reply or ""
    assert "地球" in reply
    assert "查询服务暂时不可用" in reply


def test_wf_help_marks_a_missing_query_service(policy, tmp_path) -> None:
    result = processor(FakeQueryService(), policy, tmp_path, query_service=None)
    decision = result.process(message("/wf help"))
    assert "未配置查询服务" in (decision.reply or "")


def test_wf_bounty_query_requests_a_native_image_reply(policy, tmp_path) -> None:
    service = FakeQueryService()
    decision = processor(service, policy, tmp_path).process(message("/wf 地球"))
    assert service.calls == [(CETUS_KEY, {})]
    assert decision.reply is None
    assert decision.image_url == "https://example.invalid/redacted-signed-url"


def test_wf_bounty_query_falls_back_to_text_when_the_answer_has_no_image(policy, tmp_path) -> None:
    service = FakeQueryService()
    service.results[CETUS_KEY] = QueryResult(CETUS_KEY, "标题", "希图斯赏金（地球）\n- 搜索并救援")
    decision = processor(service, policy, tmp_path).process(message("/wf 地球"))
    assert decision.reply == "希图斯赏金（地球）\n- 搜索并救援"


def test_filtered_bounty_query_keeps_the_text_form(policy, tmp_path) -> None:
    service = FakeQueryService()
    decision = processor(service, policy, tmp_path).process(message("/wf 地球 搜索并救援"))
    reply = decision.reply or ""
    assert service.calls == [(CETUS_KEY, {"job": ["RescueBountyResc"]})]
    assert "当前轮次 09-20 19:48" in reply
    assert reply.endswith("图片：https://example.invalid/redacted-signed-url")


def test_bounty_query_maps_labels_and_comma_lists(policy, tmp_path) -> None:
    service = FakeQueryService()
    processor(service, policy, tmp_path).process(message("/wf cetus 搜索并救援,破坏原型机"))
    assert service.calls == [(CETUS_KEY, {"job": ["RescueBountyResc", "SabotageBountySab"]})]


def test_bounty_query_rejects_an_unknown_task_without_calling_the_service(policy, tmp_path) -> None:
    service = FakeQueryService()
    decision = processor(service, policy, tmp_path).process(message("/wf 地球 不存在的任务"))
    reply = decision.reply or ""
    assert "赏金任务不在可选范围内：不存在的任务" in reply
    assert "发送 /wf 地球 help" in reply
    assert service.calls == []


def test_riven_query_uses_the_declared_default_shape(policy, tmp_path) -> None:
    service = FakeQueryService()
    decision = processor(service, policy, tmp_path).process(message("/wf 紫卡 托里德"))
    assert service.calls == [(RIVEN_KEY, {"weapon": ["托里德"], "shape": ["3P1N"]})]
    assert "托里德（Torid） 步枪 · 倾向 1.3" in (decision.reply or "")
    assert "- 暴击几率 164.5~201.1%" in (decision.reply or "")


@pytest.mark.parametrize(
    ("typed", "canonical"),
    [
        ("3P1N", "3P1N"),
        ("3+1", "3P1N"),
        ("3P", "3P"),
        ("3+", "3P"),
        ("3P0N", "3P"),
        ("2P1N", "2P1N"),
        ("2+1", "2P1N"),
        ("2P", "2P"),
        ("2+", "2P"),
        ("2P0N", "2P"),
        ("3正1负", "3P1N"),
        ("2正", "2P"),
    ],
)
def test_riven_query_normalises_every_shape_spelling(
    policy, tmp_path, typed: str, canonical: str
) -> None:
    service = FakeQueryService()
    processor(service, policy, tmp_path).process(message(f"/wf 紫卡 托里德 {typed}"))
    assert service.calls == [(RIVEN_KEY, {"weapon": ["托里德"], "shape": [canonical]})]


def test_riven_query_accepts_an_optional_action_token(policy, tmp_path) -> None:
    service = FakeQueryService()
    processor(service, policy, tmp_path).process(message("/wf 紫卡 词条 托里德 3P"))
    assert service.calls == [(RIVEN_KEY, {"weapon": ["托里德"], "shape": ["3P"]})]


def test_riven_query_supports_quoted_named_parameters_with_spaces(policy, tmp_path) -> None:
    service = FakeQueryService()
    processor(service, policy, tmp_path).process(message('/wf 紫卡 weapon="Torid Prime"'))
    assert service.calls == [(RIVEN_KEY, {"weapon": ["Torid Prime"], "shape": ["3P1N"]})]


def test_riven_query_supports_a_quoted_positional_weapon_name(policy, tmp_path) -> None:
    service = FakeQueryService()
    processor(service, policy, tmp_path).process(message('/wf 紫卡 "Dual Toxocyst" 3P'))
    assert service.calls == [(RIVEN_KEY, {"weapon": ["Dual Toxocyst"], "shape": ["3P"]})]


def test_riven_query_requires_the_weapon(policy, tmp_path) -> None:
    service = FakeQueryService()
    decision = processor(service, policy, tmp_path).process(message("/wf 紫卡"))
    reply = decision.reply or ""
    assert "缺少必填参数：武器名称" in reply
    assert "发送 /wf 紫卡 help" in reply
    assert service.calls == []


def test_riven_query_rejects_an_unknown_shape(policy, tmp_path) -> None:
    service = FakeQueryService()
    decision = processor(service, policy, tmp_path).process(message("/wf 紫卡 托里德 9P"))
    assert "紫卡结构不在可选范围内：9P" in (decision.reply or "")
    assert service.calls == []


def test_query_rejects_unknown_parameter_names(policy, tmp_path) -> None:
    service = FakeQueryService()
    decision = processor(service, policy, tmp_path).process(
        message("/wf 紫卡 weapon=托里德 foo=bar")
    )
    assert "未知参数名：foo" in (decision.reply or "")
    assert service.calls == []


def test_query_rejects_extra_positional_tokens(policy, tmp_path) -> None:
    service = FakeQueryService()
    decision = processor(service, policy, tmp_path).process(message("/wf 紫卡 托里德 3P1N extra"))
    assert "参数过多：extra" in (decision.reply or "")
    assert service.calls == []


def test_riven_query_reports_an_unknown_subject(policy, tmp_path) -> None:
    service = FakeQueryService()
    decision = processor(service, policy, tmp_path).process(message("/wf 火星"))
    assert "没有找到领域 火星" in (decision.reply or "")
    assert service.calls == []


def test_wf_without_arguments_shows_usage(policy, tmp_path) -> None:
    decision = processor(FakeQueryService(), policy, tmp_path).process(message("/wf"))
    assert decision.reason == "command-handled"
    assert "用法：/wf <领域> [指令] [参数...]" in (decision.reply or "")


def test_riven_target_help_lists_actions_and_values(policy, tmp_path) -> None:
    decision = processor(FakeQueryService(), policy, tmp_path).process(message("/wf 紫卡 help"))
    reply = decision.reply or ""
    assert "指令：词条上下限（默认，可省略）" in reply
    assert "用法：/wf 紫卡 武器名称 [紫卡结构]" in reply
    assert "武器名称（必填）" in reply
    assert (
        "紫卡结构（可选，默认 3P1N，取值：3正1负（3P1N）、3正（3P）、2正1负（2P1N）、2正（2P））"
        in reply
    )
    assert "weapon=Dual Toxocyst" in reply


def test_wf_without_a_query_service_refuses_to_query(policy, tmp_path) -> None:
    result = processor(FakeQueryService(), policy, tmp_path, query_service=None)
    decision = result.process(message("/wf 地球"))
    assert "查询服务未启用" in (decision.reply or "")


def test_wf_reports_service_errors_without_leaking_details(policy, tmp_path) -> None:
    service = FakeQueryService(
        fetch_error=QueryServiceResponseError(
            502, "QUERY_UPSTREAM_FAILED", "上游 wf-data 暂时不可用"
        )
    )
    decision = processor(service, policy, tmp_path).process(message("/wf 地球"))
    reply = decision.reply or ""
    assert decision.reason == "command-handled"
    assert "上游数据源暂时不可用" in reply
    assert "wf-data" not in reply


def test_wf_reports_unknown_targets_from_the_query_service(policy, tmp_path) -> None:
    service = FakeQueryService(
        fetch_error=QueryServiceResponseError(
            404, "QUERY_TARGET_NOT_FOUND", "未找到可上紫卡的武器「托里」。可能想找：托里德（torid）"
        )
    )
    decision = processor(service, policy, tmp_path).process(message("/wf 紫卡 托里"))
    reply = decision.reply or ""
    assert "未找到可上紫卡的武器「托里」" in reply
    assert "托里德（torid）" in reply
    assert "发送 /wf 紫卡 help" in reply


def test_wf_reports_service_option_rejections_with_the_option_labels(policy, tmp_path) -> None:
    service = FakeQueryService(
        fetch_error=QueryServiceResponseError(
            400,
            "QUERY_OPTION_UNKNOWN",
            "不在可选范围内的参数：Nope",
            (MatchKeyOption("RescueBountyResc", "搜索并救援"),),
        )
    )
    decision = processor(service, policy, tmp_path).process(message("/wf 地球"))
    reply = decision.reply or ""
    assert "不在可选范围内的参数：Nope" in reply
    assert "可选：搜索并救援" in reply


def test_wf_reply_text_is_capped_but_keeps_the_image_link(policy, tmp_path) -> None:
    service = FakeQueryService()
    service.results[CETUS_KEY] = QueryResult(
        CETUS_KEY, "标题", "长" * 500, "https://example.invalid/image"
    )
    decision = processor(service, policy, tmp_path, query_reply_max_chars=100).process(
        message("/wf 地球 搜索并救援")
    )
    reply = decision.reply or ""
    assert reply.endswith("图片：https://example.invalid/image")
    assert "…" in reply.splitlines()[0]


def test_command_help_convention_covers_other_commands(policy) -> None:
    result = make_processor(FakeClient(ACTIVE), policy)
    assert "用法：/bind <event_key> [关注项...]" in (
        result.process(message("/bind help")).reply or ""
    )
    assert "用法：/whoami" in (result.process(message("/whoami help")).reply or "")
    assert "/events <event_key>" in (result.process(message("/events help")).reply or "")


def test_admin_command_help_still_requires_the_admin_policy(policy) -> None:
    admin = PermissionSnapshot("qqbot", "admin-openid", "active", "admin", False, True)
    user_result = make_processor(FakeClient(ACTIVE), policy)
    assert user_result.process(message("/grant help")).reason == "command-denied"
    admin_result = make_processor(FakeClient(admin), policy)
    reply = admin_result.process(message("/grant help", openid="admin-openid")).reply or ""
    assert "用法：/grant" in reply


def test_help_with_a_command_argument_matches_command_help(policy, tmp_path) -> None:
    service = FakeQueryService()
    result = processor(service, policy, tmp_path)
    direct = result.process(message("/wf help")).reply
    assert result.process(message("/help /wf")).reply == direct
    assert result.process(message("/help wf")).reply == direct


def test_help_overview_lists_commands_by_policy(policy) -> None:
    reply = make_processor(FakeClient(ACTIVE), policy).process(message("/help")).reply or ""
    assert "/events <event_key>" in reply
    assert "/bind <event_key> [关注项...]" in reply
    assert "/wf <领域> [指令] [参数...]" in reply
    assert "管理员：/admin、/grant、/permissions、/revoke、/unadmin、/userlist" in reply
    assert "发送 /<命令> help" in reply


def test_unknown_command_help_is_reported(policy) -> None:
    decision = make_processor(FakeClient(ACTIVE), policy).process(message("/missing help"))
    assert decision.reason == "unknown-command"
