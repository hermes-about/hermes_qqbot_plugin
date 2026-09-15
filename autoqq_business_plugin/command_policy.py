import json
import re
from pathlib import Path

from .models import AccessPolicy, ParsedCommand

_COMMAND_RE = re.compile(r"^(/[a-z][a-z0-9_-]*)(?:\s+(.+))?$", re.DOTALL)
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
    return ParsedCommand(name=match.group(1).lower(), args=tuple(raw_args.split()))
