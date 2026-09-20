"""Deterministic parsing of user tokens into declared query parameters.

The command shape is fixed by the controlled catalogue:

```text
/<命令> <领域> [<指令>] <参数...>
```

Users may write parameters positionally (`/wf 紫卡 托里德 3P1N`) or by name
(`/wf 紫卡 weapon=Dual Toxocyst`). Parsing never guesses domain meaning: it only
maps tokens onto the parameter list the query service declared, so a new domain
never requires new parsing code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import QueryParam

_PARAM_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_MAX_TOKENS = 32


@dataclass(frozen=True)
class Assignment:
    values: dict[str, list[str]]
    unknown_names: tuple[str, ...] = ()
    leftovers: tuple[str, ...] = ()


def normalise_tokens(tokens: tuple[str, ...]) -> tuple[str, ...]:
    """Drop blank tokens and keep the first occurrence of each token."""
    unique: list[str] = []
    for token in tokens:
        value = token.strip()
        if value and value not in unique:
            unique.append(value)
    if len(unique) > _MAX_TOKENS:
        raise ValueError("too many tokens")
    return tuple(unique)


def assign(params: tuple[QueryParam, ...], tokens: tuple[str, ...]) -> Assignment:
    """Split tokens into declared parameters without interpreting their meaning.

    Trailing tokens that resolve to a later enum parameter are peeled off first,
    so `/wf 紫卡 托里德 3P1N` splits into weapon + shape while a multi-word
    weapon name without a shape stays in one text parameter.
    """
    by_name = {param.name: param for param in params}
    values: dict[str, list[str]] = {}
    positional: list[str] = []
    unknown: list[str] = []
    for token in tokens:
        name, separator, value = token.partition("=")
        key = name.strip()
        if separator and key in by_name:
            if value.strip():
                values.setdefault(key, []).append(value.strip())
            else:
                unknown.append(key)
        elif separator and _PARAM_NAME.fullmatch(key):
            unknown.append(key)
        else:
            positional.append(token)

    remaining = list(positional)
    for param in reversed(params):
        if param.name in values or param.type != "enum" or not remaining:
            continue
        taken: list[str] = []
        while remaining and param.option_key_for(remaining[-1]) is not None:
            taken.insert(0, remaining.pop())
        if taken:
            values[param.name] = taken
    for param in params:
        if param.name in values or not remaining:
            continue
        values[param.name] = [remaining.pop(0)]
    return Assignment(values=values, unknown_names=tuple(unknown), leftovers=tuple(remaining))


def validate(
    params: tuple[QueryParam, ...], assignment: Assignment
) -> tuple[dict[str, list[str]], str | None]:
    """Return the canonical parameter values, or one user-facing error."""
    if assignment.unknown_names:
        return {}, f"未知参数名：{assignment.unknown_names[0]}"
    if assignment.leftovers:
        return {}, f"参数过多：{' '.join(assignment.leftovers)}"

    resolved: dict[str, list[str]] = {}
    for param in params:
        items = _split(assignment.values.get(param.name, []), param)
        if not items and param.default:
            items = [param.default]
        if not items:
            if param.required:
                return {}, f"缺少必填参数：{param.label}"
            continue
        if param.type == "enum":
            canonical: list[str] = []
            for token in items:
                key = param.option_key_for(token)
                if key is None:
                    options = "、".join(option.label for option in param.options)
                    return {}, f"{param.label}不在可选范围内：{token}。可选：{options}"
                if key not in canonical:
                    canonical.append(key)
            if param.max_items and len(canonical) > param.max_items:
                return {}, f"{param.label}一次最多 {param.max_items} 项"
            resolved[param.name] = canonical
            continue
        if len(items) > 1:
            return {}, f"参数重复：{param.label}"
        text = items[0].strip()
        if len(text) > param.max_length:
            return {}, f"{param.label}过长（最多 {param.max_length} 字）"
        if any(ord(char) < 32 for char in text):
            return {}, f"{param.label}包含不可用字符"
        resolved[param.name] = [text]
    return resolved, None


def _split(values: list[str], param: QueryParam) -> list[str]:
    if not param.multiple:
        return list(values)
    items: list[str] = []
    for value in values:
        for part in value.split(","):
            token = part.strip()
            if token and token not in items:
                items.append(token)
    return items


def usage(params: tuple[QueryParam, ...]) -> str:
    """Render the positional form of a parameter list."""
    parts: list[str] = []
    for param in params:
        label = param.label + ("..." if param.multiple else "")
        parts.append(label if param.required else f"[{label}]")
    return " ".join(parts)
