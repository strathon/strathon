"""Tests for column-level encryption."""

from __future__ import annotations

import os

import pytest


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
