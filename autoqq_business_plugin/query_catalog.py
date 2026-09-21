"""Controlled catalogue of on-demand query commands.

The Plugin never learns provider field names or URLs. This file maps a command
(`/wf`) and the user-typed subject aliases (`地球`, `cetus`, …) onto a
`query_key` served by a read-only query service; the query service owns the
upstream URL, the parsing rules and the option list. Adding a subject is a
deployment configuration change, never a user operation.
"""

import json
import re
from pathlib import Path
from typing import Any

from .models import QueryNamespace, QueryTarget

_NAMESPACE = re.compile(r"^[a-z][a-z0-9_-]*$")
_QUERY_KEY = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_RESERVED_ALIASES = frozenset({"help"})
_REPLY_MODES = frozenset({"text", "image_url", "image_url_always"})


class QueryCatalogError(ValueError):
    pass


class QueryCatalog:
    def __init__(self, namespaces: dict[str, QueryNamespace]) -> None:
        self._namespaces = dict(namespaces)

    @classmethod
    def empty(cls) -> "QueryCatalog":
        return cls({})

    @classmethod
    def from_file(cls, path: Path) -> "QueryCatalog":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            raw = payload["namespaces"]
            if not isinstance(raw, dict):
                raise TypeError("namespaces must be an object")
            namespaces = {str(name): _namespace(str(name), value) for name, value in raw.items()}
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise QueryCatalogError(f"invalid query catalogue file: {path}") from exc
        return cls(namespaces)

    def namespaces(self) -> tuple[QueryNamespace, ...]:
        return tuple(self._namespaces[name] for name in sorted(self._namespaces))

    def for_command(self, command: str) -> QueryNamespace | None:
        return self._namespaces.get(command.lstrip("/").lower())


def _namespace(name: str, value: Any) -> QueryNamespace:
    if not _NAMESPACE.fullmatch(name):
        raise QueryCatalogError(f"invalid query namespace: {name}")
    if not isinstance(value, dict):
        raise TypeError("namespace must be an object")
    raw_targets = value.get("targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        raise TypeError("namespace targets must be a non-empty array")
    targets = tuple(_target(item) for item in raw_targets)
    query_keys = [target.query_key for target in targets]
    if len(set(query_keys)) != len(query_keys):
        raise QueryCatalogError(f"duplicate query_key in namespace {name}")
    aliases = [alias.lower() for target in targets for alias in target.aliases]
    if len(set(aliases)) != len(aliases):
        raise QueryCatalogError(f"duplicate alias in namespace {name}")
    return QueryNamespace(
        command=f"/{name}",
        display_name=_text(value.get("display_name"), "display_name", fallback=name),
        description=_text(value.get("description"), "description", allow_empty=True),
        targets=targets,
    )


def _target(value: Any) -> QueryTarget:
    if not isinstance(value, dict):
        raise TypeError("target must be an object")
    query_key = value.get("query_key")
    if not isinstance(query_key, str) or not _QUERY_KEY.fullmatch(query_key):
        raise QueryCatalogError("target query_key is invalid")
    raw_aliases = value.get("aliases")
    if not isinstance(raw_aliases, list) or not raw_aliases:
        raise TypeError("target aliases must be a non-empty array")
    aliases: list[str] = []
    for item in raw_aliases:
        if not isinstance(item, str) or not item.strip():
            raise QueryCatalogError("target aliases must be non-empty strings")
        if item.strip().lower() in _RESERVED_ALIASES:
            raise QueryCatalogError(f"alias {item.strip()} is reserved for help")
        aliases.append(item.strip())
    reply = value.get("reply", "text")
    if not isinstance(reply, str) or reply not in _REPLY_MODES:
        raise QueryCatalogError("target reply must be one of: " + ", ".join(sorted(_REPLY_MODES)))
    return QueryTarget(query_key=query_key, aliases=tuple(aliases), reply=reply)


def _text(value: Any, field: str, *, fallback: str = "", allow_empty: bool = False) -> str:
    if value is None:
        return fallback
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    if not allow_empty and not value.strip():
        return fallback
    return value.strip()
