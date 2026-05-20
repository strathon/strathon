"""MFA (TOTP) repository operations.

Handles TOTP secret generation, verification, backup code management.
Uses pyotp for RFC 6238 TOTP implementation.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from typing import Optional
from uuid import UUID

import pyotp
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.identity import User

logger = logging.getLogger(__name__)

BACKUP_CODE_COUNT = 8
BACKUP_CODE_LENGTH = 8  # 8 hex chars = 32 bits entropy each


def generate_totp_secret() -> str:
    """Generate a random base32 TOTP secret (160 bits per RFC 4226)."""
    return pyotp.random_base32(length=32)


def get_totp_uri(secret: str, email: str) -> str:
    """Generate an otpauth:// URI for QR code scanning."""
    totp = pyotp.TOTP(secret)
    return totp.provisioning_uri(name=email, issuer_name="Strathon")


def verify_totp_code(secret: str, code: str) -> bool:
    """Verify a TOTP code against a secret. Allows +-1 window for clock skew."""
    totp = pyotp.TOTP(secret)
    return totp.verify(code, valid_window=1)


def _hash_backup_code(code: str) -> str:
    """SHA-256 hash a backup code for storage."""
    return hashlib.sha256(code.encode()).hexdigest()


def generate_backup_codes() -> tuple[list[str], list[str]]:
    """Generate backup codes. Returns (plaintext_list, hashed_list)."""
    plain = [secrets.token_hex(BACKUP_CODE_LENGTH // 2) for _ in range(BACKUP_CODE_COUNT)]
    hashed = [_hash_backup_code(c) for c in plain]
    return plain, hashed


async def setup_totp(
    session: AsyncSession,
    user_id: UUID,
) -> tuple[str, str]:
    """Generate and store a pending TOTP secret. Returns (secret, uri).

    Does NOT enable MFA yet — call enable_mfa after the user verifies
    a code from their authenticator app.
    """
    user = await _get_user(session, user_id)
    if user is None:
        raise ValueError("user not found")

    secret = generate_totp_secret()
    # Store the secret but don't enable MFA yet.
    await session.execute(
        update(User)
        .where(User.id == user_id)
        .values(totp_secret=secret)
    )
    uri = get_totp_uri(secret, user.email or str(user_id))
    return secret, uri


async def verify_and_enable_mfa(
    session: AsyncSession,
    user_id: UUID,
    code: str,
) -> Optional[list[str]]:
    """Verify a TOTP code and enable MFA. Returns plaintext backup codes.

    Returns None if the code is invalid or no secret is pending.
    """
    user = await _get_user(session, user_id)
    if user is None or not user.totp_secret:
        return None

    if not verify_totp_code(user.totp_secret, code):
        return None

    # Generate backup codes.
    plain_codes, hashed_codes = generate_backup_codes()

    await session.execute(
        update(User)
        .where(User.id == user_id)
        .values(
            mfa_enabled=True,
            backup_codes=hashed_codes,
        )
    )
    return plain_codes


async def disable_mfa(
    session: AsyncSession,
    user_id: UUID,
) -> bool:
    """Disable MFA and clear TOTP secret + backup codes."""
    result = await session.execute(
        update(User)
        .where(User.id == user_id)
        .values(
            mfa_enabled=False,
            totp_secret=None,
            backup_codes=None,
        )
    )
    return result.rowcount > 0


async def verify_mfa_code(
    session: AsyncSession,
    user_id: UUID,
    code: str,
) -> bool:
    """Verify a TOTP code or backup code for login.

    If a backup code is used, it's consumed (removed from the list).
    """
    user = await _get_user(session, user_id)
    if user is None or not user.totp_secret:
        return False

    # Try TOTP first.
    if verify_totp_code(user.totp_secret, code):
        return True

    # Try backup codes.
    if user.backup_codes:
        code_hash = _hash_backup_code(code)
        if code_hash in user.backup_codes:
            # Consume the backup code (single-use).
            remaining = [c for c in user.backup_codes if c != code_hash]
            await session.execute(
                update(User)
                .where(User.id == user_id)
                .values(backup_codes=remaining)
            )
            return True

    return False


async def _get_user(
    session: AsyncSession,
    user_id: UUID,
) -> Optional[User]:
    """Get a user by ID."""
    result = await session.execute(
        select(User).where(User.id == user_id)
    )
    return result.scalar_one_or_none()
