from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import message

from autoqq_business_plugin.command_policy import (
    CommandPolicyError,
    CommandPolicyRegistry,
    parse_command,
)
from autoqq_business_plugin.config import ConfigurationError, Settings
from autoqq_business_plugin.identity import IdentityError, extract_identity
from autoqq_business_plugin.models import AccessPolicy


def valid_env() -> dict[str, str]:
    return {
        "EVENT_SERVER_URL": "http://eventserver:8080/",
        "INTERNAL_API_TOKEN": "x" * 32,
        "DELIVERY_WORKER_ID": "worker-1",
    }


def test_settings_are_fail_closed_and_strip_base_url() -> None:
    settings = Settings.from_env(valid_env())
    assert settings.event_server_url == "http://eventserver:8080"
    assert settings.delivery_poll_enabled is True


@pytest.mark.parametrize(
    "update",
    [
        {"INTERNAL_API_TOKEN": "short"},
        {"EVENT_SERVER_URL": "eventserver:8080"},
        {"EVENT_SERVER_URL": "http://user@eventserver:8080"},
        {"DELIVERY_WORKER_ID": ""},
        {"DELIVERY_CLAIM_BATCH_SIZE": "0"},
    ],
)
def test_invalid_settings_are_rejected(update: dict[str, str]) -> None:
    env = valid_env() | update
    with pytest.raises(ConfigurationError):
        Settings.from_env(env)


def test_disabled_polling_does_not_require_worker_id() -> None:
    env = valid_env() | {"DELIVERY_POLL_ENABLED": "false", "DELIVERY_WORKER_ID": ""}
    assert Settings.from_env(env).delivery_worker_id == ""


def test_identity_uses_only_normalized_trusted_source() -> None:
    identity = extract_identity(message("hello"))
    assert identity.openid == "user-openid"
    assert identity.chat_type == "dm"
    with pytest.raises(IdentityError):
        extract_identity(SimpleNamespace(text="hello", source=SimpleNamespace(platform="qqbot")))


def test_command_parser_keeps_malformed_slash_messages_out_of_llm() -> None:
    assert parse_command("ordinary") is None
    assert parse_command("/bind demo.event").args == ("demo.event",)
    assert parse_command("/BAD!").name == ""


def test_privileged_command_cannot_be_public(tmp_path: Path) -> None:
    path = tmp_path / "commands.yaml"
    path.write_text('{"commands":{"/grant":"public"}}', encoding="utf-8")
    with pytest.raises(CommandPolicyError):
        CommandPolicyRegistry.from_file(path)


def test_policy_lookup_is_explicit(policy: CommandPolicyRegistry) -> None:
    assert policy.policy_for("/help") is AccessPolicy.PUBLIC
    assert policy.policy_for("/bind") is AccessPolicy.AUTHORIZED
    assert policy.policy_for("/grant") is AccessPolicy.ADMIN
    assert policy.policy_for("/missing") is None
