"""Channel-aware presentation for generic EventServer delivery metadata."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_MAX_MATCH_ITEMS = 32
_MAX_LABEL_CHARS = 100
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+.!|>~\-])")


def render_delivery_text(message: Mapping[str, Any], *, max_chars: int) -> str | None:
    """Highlight exact matched bullet lines, falling back to the original text safely."""
    text = message.get("text")
    if not isinstance(text, str):
        return None
    labels = _subscription_match_labels(message)
    if not labels:
        return text

    rendered_lines: list[str] = []
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        ending = line[len(content) :]
        label = content.removeprefix("• ") if content.startswith("• ") else None
        if label in labels:
            content = f"• **{_escape_markdown(label)}** 🔴"
        rendered_lines.append(content + ending)
    rendered = "".join(rendered_lines)
    return rendered if len(rendered) <= max_chars else text


def _subscription_match_labels(message: Mapping[str, Any]) -> set[str]:
    data = message.get("data")
    if not isinstance(data, Mapping):
        return set()
    subscription_match = data.get("subscription_match")
    if not isinstance(subscription_match, Mapping):
        return set()
    items = subscription_match.get("items")
    if not isinstance(items, list) or not items or len(items) > _MAX_MATCH_ITEMS:
        return set()

    labels: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping):
            return set()
        key = item.get("key")
        label = item.get("label")
        if (
            not isinstance(key, str)
            or not key
            or not isinstance(label, str)
            or not label
            or len(label) > _MAX_LABEL_CHARS
            or "\n" in label
            or "\r" in label
        ):
            return set()
        labels.add(label)
    return labels


def _escape_markdown(value: str) -> str:
    return _MARKDOWN_SPECIAL.sub(r"\\\1", value)
