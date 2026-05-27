"""Column-level encryption for sensitive data at rest.

Encrypts TOTP secrets, webhook signing secrets, and notification
channel credentials before storing in Postgres. Uses Fernet
(AES-256-CBC + HMAC-SHA256) from the cryptography library.

Key management:
  Self-hosted: STRATHON_ENCRYPTION_KEY env var (Fernet key, base64).
               Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  Managed cloud (future): AWS KMS / GCP KMS envelope encryption.

If STRATHON_ENCRYPTION_KEY is not set, encryption is disabled and
values are stored in plaintext (backward compatible for existing
deployments). A warning is logged on startup.

Research: Fernet symmetric encryption (AES-256-CBC + HMAC), column-
level encryption patterns for PostgreSQL + Python (miguelgrinberg.com
2025, AWS blog 2025), OWASP cryptographic storage cheat sheet.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("strathon.encryption")

_fernet_instance = None
_initialized = False


def _get_fernet():
    """Lazy-init Fernet from env var."""
    global _fernet_instance, _initialized
    if _initialized:
        return _fernet_instance

    _initialized = True
    key = os.environ.get("STRATHON_ENCRYPTION_KEY")
    if not key:
        logger.warning(
            "STRATHON_ENCRYPTION_KEY not set. Sensitive columns "
            "(TOTP secrets, webhook secrets) stored in plaintext. "
            "Generate a key: python -c "
            '"from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
        return None

    try:
        from cryptography.fernet import Fernet
        _fernet_instance = Fernet(key.encode() if isinstance(key, str) else key)
        logger.info("Column-level encryption enabled")
        return _fernet_instance
    except Exception:
        logger.exception("Invalid STRATHON_ENCRYPTION_KEY — encryption disabled")
        return None


def encrypt(plaintext: str) -> str:
    """Encrypt a string value. Returns ciphertext prefixed with 'enc:'.

    If encryption is not configured, returns the plaintext unchanged.
    The 'enc:' prefix lets decrypt() distinguish encrypted from
    plaintext values (backward compat for existing rows).
    """
    if not plaintext:
        return plaintext

    f = _get_fernet()
    if f is None:
        return plaintext

    encrypted = f.encrypt(plaintext.encode("utf-8"))
    return "enc:" + encrypted.decode("utf-8")


def decrypt(stored_value: str) -> str:
    """Decrypt a stored value. Handles both encrypted ('enc:' prefix)
    and plaintext (no prefix) values for backward compatibility.

    If the value doesn't start with 'enc:', it's returned as-is
    (plaintext from before encryption was enabled).
    """
    if not stored_value:
        return stored_value

    if not stored_value.startswith("enc:"):
        return stored_value  # Plaintext (backward compat).

    f = _get_fernet()
    if f is None:
        logger.error(
            "Encrypted value found but STRATHON_ENCRYPTION_KEY not set. "
            "Cannot decrypt."
        )
        raise RuntimeError(
            "STRATHON_ENCRYPTION_KEY required to decrypt stored secrets"
        )

    ciphertext = stored_value[4:]  # Strip 'enc:' prefix.
    return f.decrypt(ciphertext.encode("utf-8")).decode("utf-8")


def is_encryption_enabled() -> bool:
    """Check if encryption is configured."""
    return _get_fernet() is not None
