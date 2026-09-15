import logging

from autoqq_business_plugin.models import PermissionSnapshot
from autoqq_business_plugin.observability import mask_identifier, redact_text
from autoqq_business_plugin.permission_cache import PermissionCache


def test_permission_cache_ttl_and_invalidation() -> None:
    now = [100.0]
    calls = []
    cache = PermissionCache(30, clock=lambda: now[0])
    snapshot = PermissionSnapshot("qqbot", "openid", "active", "user", True, False)

    def load():
        calls.append(1)
        return snapshot

    assert cache.get_or_load("qqbot", "openid", load) is snapshot
    assert cache.get_or_load("qqbot", "openid", load) is snapshot
    assert len(calls) == 1
    cache.invalidate("qqbot", "openid")
    cache.get_or_load("qqbot", "openid", load)
    now[0] += 31
    cache.get_or_load("qqbot", "openid", load)
    assert len(calls) == 3


def test_sensitive_values_are_redacted_and_openids_are_hashed(caplog) -> None:
    raw_openid = "real-user-openid-never-log"
    assert raw_openid not in mask_identifier(raw_openid)
    text = 'Authorization: Bearer abc.def lease_token="secret-value"'
    redacted = redact_text(text)
    assert "abc.def" not in redacted
    assert "secret-value" not in redacted

    with caplog.at_level(logging.INFO, logger="autoqq.plugin"):
        logging.getLogger("autoqq.plugin").info("actor=%s", mask_identifier(raw_openid))
    assert raw_openid not in caplog.text
