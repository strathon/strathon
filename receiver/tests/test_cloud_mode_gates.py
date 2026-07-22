"""Cloud-mode fail-loud gates for security-critical env vars.

Mirrors test_audit_repository's HMAC cloud-gate tests: in multi-tenant
cloud mode, missing STRATHON_ENCRYPTION_KEY or STRATHON_PASSWORD_PEPPER
must raise instead of silently degrading (plaintext TOTP secrets /
unpeppered password hashes). Self-hosted mode keeps the documented
graceful fallback.
"""

from __future__ import annotations

import os
import sys

import pytest

_RECEIVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _RECEIVER_DIR)


def _reset(monkeypatch, mode: str):
    """Put the process into a known state for one gate test.

    The encryption module memoizes its Fernet instance in module globals.
    Setting them through monkeypatch (rather than assigning directly) means
    pytest restores the originals at teardown -- otherwise this test leaves
    the encryption module de-initialized for every test that runs after it,
    which is exactly the shared-state mutation that caused the CI flake.
    """
    monkeypatch.setenv("STRATHON_MODE", mode)
    monkeypatch.delenv("STRATHON_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("STRATHON_PASSWORD_PEPPER", raising=False)
    import config
    config.get_settings.cache_clear()
    import encryption
    monkeypatch.setattr(encryption, "_initialized", False)
    monkeypatch.setattr(encryption, "_fernet_instance", None)


def test_encryption_key_required_in_cloud(monkeypatch):
    _reset(monkeypatch, "cloud")
    import encryption
    with pytest.raises(RuntimeError, match="STRATHON_ENCRYPTION_KEY"):
        encryption._get_fernet()
    import config
    config.get_settings.cache_clear()


def test_encryption_key_optional_when_self_hosted(monkeypatch):
    _reset(monkeypatch, "self-hosted")
    import encryption
    assert encryption._get_fernet() is None  # plaintext fallback, no raise
    import config
    config.get_settings.cache_clear()


def test_pepper_required_in_cloud(monkeypatch):
    _reset(monkeypatch, "cloud")
    import password
    with pytest.raises(RuntimeError, match="STRATHON_PASSWORD_PEPPER"):
        password._apply_pepper("hunter2")
    import config
    config.get_settings.cache_clear()


def test_pepper_optional_when_self_hosted(monkeypatch):
    _reset(monkeypatch, "self-hosted")
    import password
    assert password._apply_pepper("hunter2") == "hunter2"
    import config
    config.get_settings.cache_clear()


def test_all_gates_apply_with_require_security_keys_flag(monkeypatch):
    """STRATHON_REQUIRE_SECURITY_KEYS=true opts a self-host deployment into
    the same hard key gates as cloud: missing keys refuse to run instead
    of degrading with a warning."""
    _reset(monkeypatch, "self-hosted")
    monkeypatch.setenv("STRATHON_REQUIRE_SECURITY_KEYS", "true")
    import config
    config.get_settings.cache_clear()
    monkeypatch.setenv("STRATHON_AUDIT_HMAC_KEY", "")
    import encryption
    import password
    import repositories.audit as audit_repo
    # One-shot warning flag lives on the function object; clear it for this
    # test and let monkeypatch put the original back afterwards.
    monkeypatch.delattr(audit_repo._get_hmac_key, "_warned", raising=False)

    with pytest.raises(RuntimeError, match="STRATHON_ENCRYPTION_KEY"):
        encryption._get_fernet()
    with pytest.raises(RuntimeError, match="STRATHON_PASSWORD_PEPPER"):
        password._apply_pepper("hunter2")
    with pytest.raises(RuntimeError, match="STRATHON_AUDIT_HMAC_KEY"):
        audit_repo._get_hmac_key()

    import config
    config.get_settings.cache_clear()
