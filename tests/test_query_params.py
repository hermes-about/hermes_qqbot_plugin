import pytest

from autoqq_business_plugin.models import MatchKeyOption, QueryParam
from autoqq_business_plugin.query_params import (
    assign,
    normalise_tokens,
    usage,
    validate,
)

WEAPON = QueryParam(name="weapon", label="武器名称", type="text", required=True, max_length=32)
SHAPE = QueryParam(
    name="shape",
    label="紫卡结构",
    type="enum",
    default="3P1N",
    options=(MatchKeyOption("3P1N", "3 正 1 负", ("3+1",)), MatchKeyOption("3P", "3 正")),
)
REQUIRED_SHAPE = QueryParam(
    name="shape",
    label="紫卡结构",
    type="enum",
    required=True,
    options=(MatchKeyOption("3P1N", "3 正 1 负"),),
)
JOBS = QueryParam(
    name="job",
    label="赏金任务",
    type="enum",
    multiple=True,
    max_items=2,
    options=(
        MatchKeyOption("A", "任务A"),
        MatchKeyOption("B", "任务B"),
        MatchKeyOption("C", "任务C"),
    ),
)


def run(params, tokens):
    return validate(params, assign(params, normalise_tokens(tokens)))


def test_positional_values_fill_parameters_in_order() -> None:
    values, error = run((WEAPON, SHAPE), ("托里德", "3P"))
    assert error is None
    assert values == {"weapon": ["托里德"], "shape": ["3P"]}


def test_default_is_applied_when_the_optional_parameter_is_absent() -> None:
    values, error = run((WEAPON, SHAPE), ("托里德",))
    assert error is None
    assert values == {"weapon": ["托里德"], "shape": ["3P1N"]}


def test_trailing_tokens_are_peeled_into_a_multi_value_enum() -> None:
    values, error = run((JOBS,), ("任务A", "C"))
    assert error is None
    assert values == {"job": ["A", "C"]}


def test_comma_separated_values_are_split_only_for_multi_value_parameters() -> None:
    values, error = run((JOBS,), ("任务A,任务B",))
    assert error is None
    assert values == {"job": ["A", "B"]}


def test_named_values_win_over_position() -> None:
    values, error = run((WEAPON, SHAPE), ("weapon=Torid Prime",))
    assert error is None
    assert values == {"weapon": ["Torid Prime"], "shape": ["3P1N"]}


def test_enum_aliases_are_normalised() -> None:
    values, error = run((WEAPON, SHAPE), ("托里德", "3+1"))
    assert error is None
    assert values == {"weapon": ["托里德"], "shape": ["3P1N"]}


def test_unknown_parameter_name_is_reported() -> None:
    values, error = run((WEAPON, SHAPE), ("weapon=托里德", "foo=bar"))
    assert values == {}
    assert error == "未知参数名：foo"


def test_extra_tokens_are_reported_when_the_last_parameter_is_required() -> None:
    values, error = run((WEAPON, REQUIRED_SHAPE), ("托里德", "3P1N", "多余"))
    assert values == {}
    assert error == "参数过多：多余"


def test_missing_required_parameter_is_reported() -> None:
    values, error = run((WEAPON, SHAPE), ())
    assert values == {}
    assert error == "缺少必填参数：武器名称"


def test_unknown_enum_value_lists_the_labels() -> None:
    values, error = run((WEAPON, SHAPE), ("托里德", "9P"))
    assert values == {}
    assert error == "紫卡结构不在可选范围内：9P。可选：3 正 1 负、3 正"


def test_multi_value_parameter_respects_its_limit() -> None:
    values, error = run((JOBS,), ("任务A", "任务B", "任务C"))
    assert values == {}
    assert error == "赏金任务一次最多 2 项"


def test_text_length_and_control_characters_are_rejected() -> None:
    assert run((WEAPON,), ("x" * 33,))[1] == "武器名称过长（最多 32 字）"
    assert run((WEAPON,), ("托\t里德",))[1] == "武器名称包含不可用字符"


def test_repeated_single_value_parameter_is_rejected() -> None:
    values, error = run((WEAPON, SHAPE), ("weapon=托里德", "weapon=托里"))
    assert values == {}
    assert error == "参数重复：武器名称"


def test_tokens_are_deduplicated_and_bounded() -> None:
    assert normalise_tokens(("托里德", "托里德", " ")) == ("托里德",)
    with pytest.raises(ValueError):
        normalise_tokens(tuple(f"token-{index}" for index in range(40)))


def test_usage_marks_optional_and_multi_value_parameters() -> None:
    assert usage((WEAPON, SHAPE)) == "武器名称 [紫卡结构]"
    assert usage((JOBS,)) == "[赏金任务...]"
