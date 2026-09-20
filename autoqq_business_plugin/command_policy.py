import json
import re
from pathlib import Path

from .models import AccessPolicy, ParsedCommand

_COMMAND_RE = re.compile(r"^(/[a-z][a-z0-9_-]*)(?:\s+(.+))?$", re.DOTALL)
_QUOTES = {'"': '"', "'": "'", "“": "”", "‘": "’"}
_ADMIN_COMMANDS = frozenset(
    {"/grant", "/revoke", "/admin", "/unadmin", "/permissions", "/userlist"}
)


class CommandPolicyError(ValueError):
    pass


class CommandPolicyRegistry:
    def __init__(self, policies: dict[str, AccessPolicy]) -> None:
        self._policies = dict(policies)
        for name in _ADMIN_COMMANDS:
            if self._policies.get(name) is AccessPolicy.PUBLIC:
                raise CommandPolicyError(f"privileged command {name} cannot be public")

    @classmethod
    def from_file(cls, path: Path) -> "CommandPolicyRegistry":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            raw = payload["commands"]
            if not isinstance(raw, dict):
                raise TypeError("commands must be an object")
            policies = {str(name): AccessPolicy(str(value)) for name, value in raw.items()}
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CommandPolicyError(f"invalid command policy file: {path}") from exc
        return cls(policies)

    def policy_for(self, name: str) -> AccessPolicy | None:
        return self._policies.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._policies))


def parse_command(text: str) -> ParsedCommand | None:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    match = _COMMAND_RE.fullmatch(stripped)
    if match is None:
        return ParsedCommand(name="", args=())
    raw_args = match.group(2) or ""
    return ParsedCommand(name=match.group(1).lower(), args=_tokenize(raw_args))


def _tokenize(raw: str) -> tuple[str, ...]:
    """Split command arguments on whitespace, honouring single or double quotes.

    Quoting is what makes a free-text parameter such as a multi-word weapon name
    expressible at all: `weapon="Torid Prime"` and `"Torid Prime"` both arrive as
    one token, while every other command keeps plain whitespace splitting.
    """
    tokens: list[str] = []
    current: list[str] = []
    closing: str | None = None
    for char in raw:
        if closing is not None:
            if char == closing:
                closing = None
            else:
                current.append(char)
            continue
        if char in _QUOTES:
            closing = _QUOTES[char]
            continue
        if char.isspace():
            if current:
                tokens.append("".join(current))
                current = []
            continue
        current.append(char)
    if current:
        tokens.append("".join(current))
    return tuple(tokens)
