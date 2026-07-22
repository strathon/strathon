"""Tests for column-level encryption."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_encryption_state(monkeypatch):
    """Contain this file's env and singleton writes to one test.

    Each test here assigns STRATHON_ENCRYPTION_KEY directly and resets the
    encryption module's memoized Fernet (``_initialized`` /
    ``_fernet_instance``). Direct assignment is not rolled back, so without
    this fixture the key outlives the file and every later test in the
    process runs with encryption switched on and a half-reset singleton --
    behaviour that then depends on test order. That is precisely the
    shared-state mutation behind the long-standing CI flake.

    Registering the env var and both globals with monkeypatch makes pytest
    restore whatever the test bodies overwrite, at teardown.
    """
    import encryption

    monkeypatch.delenv("STRATHON_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(encryption, "_initialized", False, raising=False)
    monkeypatch.setattr(encryption, "_fernet_instance", None, raising=False)
    yield


def test_encrypt_decrypt_roundtrip():
    """Encrypted value decrypts to original."""
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    os.environ["STRATHON_ENCRYPTION_KEY"] = key

    # Reset singleton.
    import encryption
    encryption._initialized = False
    encryption._fernet_instance = None

    result = encryption.encrypt("my-totp-secret-ABCDEF")
    assert result.startswith("enc:")
    assert result != "my-totp-secret-ABCDEF"

    decrypted = encryption.decrypt(result)
    assert decrypted == "my-totp-secret-ABCDEF"

    # Cleanup.
    del os.environ["STRATHON_ENCRYPTION_KEY"]
    encryption._initialized = False
    encryption._fernet_instance = None


def test_decrypt_plaintext_passthrough():
    """Plaintext values (no enc: prefix) pass through unchanged."""
    import encryption
    encryption._initialized = False
    encryption._fernet_instance = None

    assert encryption.decrypt("plain-secret") == "plain-secret"
    assert encryption.decrypt("") == ""
    assert encryption.decrypt(None) is None


def test_encrypt_without_key_returns_plaintext():
    """Without encryption key, encrypt returns plaintext."""
    os.environ.pop("STRATHON_ENCRYPTION_KEY", None)

    import encryption
    encryption._initialized = False
    encryption._fernet_instance = None

    result = encryption.encrypt("my-secret")
    assert result == "my-secret"
    assert not result.startswith("enc:")


def test_decrypt_encrypted_without_key_raises():
    """Trying to decrypt enc: value without key raises RuntimeError."""
    os.environ.pop("STRATHON_ENCRYPTION_KEY", None)

    import encryption
    encryption._initialized = False
    encryption._fernet_instance = None

    with pytest.raises(RuntimeError, match="STRATHON_ENCRYPTION_KEY"):
        encryption.decrypt("enc:gAAAAA...")


def test_is_encryption_enabled():
    """is_encryption_enabled reflects key presence."""
    from cryptography.fernet import Fernet
    import encryption

    os.environ.pop("STRATHON_ENCRYPTION_KEY", None)
    encryption._initialized = False
    encryption._fernet_instance = None
    assert encryption.is_encryption_enabled() is False

    os.environ["STRATHON_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    encryption._initialized = False
    encryption._fernet_instance = None
    assert encryption.is_encryption_enabled() is True

    del os.environ["STRATHON_ENCRYPTION_KEY"]
    encryption._initialized = False
    encryption._fernet_instance = None
