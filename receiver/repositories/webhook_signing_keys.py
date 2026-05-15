"""Persistence operations for the webhook_signing_keys table.

Two notable design points worth pointing out before reading the code:

* The plaintext signing secret never lives in the database. Persist
  receives only the SHA-256 hash plus a four-character public prefix.
  The plaintext is returned to the caller (the API handler) so it can
  be returned to the operator exactly once in the HTTP response, and
  also pushed into the in-memory keystore so the next delivery can
  sign with it.

* "Active" means revoked_at IS NULL. The webhooks/keystore module
  cares only about active keys; ``list_active_keys`` is the read path
  used at receiver boot time to restore the keystore from operator-
  supplied plaintext via STRATHON_WEBHOOK_SIGNING_SECRETS.

* The keystore lives in receiver memory and is not authoritative; the
  database row is. We never derive the keystore from the database
  alone (we can't — we don't have plaintext). So creating a key adds
  both the row and the keystore entry in a single operation; revoking
  removes both.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.webhooks import WebhookSigningKey
from webhooks.signing import create_signing_key, hash_secret

logger = logging.getLogger("strathon.receiver.repositories.webhook_signing_keys")


# ---- DTOs ---------------------------------------------------------------


@dataclass(frozen=True)
class SigningKeyRow:
    """The non-secret-bearing view of a signing key.

    This is what list endpoints return. Plaintext and hash never appear
    here; the prefix is the only way to refer to a key without revealing
    secret material.
    """
    id: uuid.UUID
    project_id: uuid.UUID
    prefix: str
    created_at: datetime
    revoked_at: datetime | None

    def to_json(self) -> dict:
        return {
            "id": str(self.id),
            "project_id": str(self.project_id),
            "prefix": self.prefix,
            "created_at": self.created_at.isoformat(),
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
        }


@dataclass(frozen=True)
class CreateSigningKeyResult:
    """Returned by create(). The plaintext is in this object, and
    the API handler must pass it to the operator and discard it. Don't
    log it, don't persist it, don't return it from any subsequent
    endpoint."""
    row: SigningKeyRow
    plaintext: str  # whsec_-prefixed, must be returned to operator once


# ---- Repository functions ----------------------------------------------


def _row_to_dto(row: WebhookSigningKey) -> SigningKeyRow:
    return SigningKeyRow(
        id=row.id,
        project_id=row.project_id,
        prefix=row.prefix,
        created_at=row.created_at,
        revoked_at=row.revoked_at,
    )


async def list_keys(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    include_revoked: bool = False,
) -> list[SigningKeyRow]:
    """Return signing keys for the project.

    Active keys (revoked_at IS NULL) come first, then revoked, both in
    creation-time-descending order so the most recent rotation is at
    the top.
    """
    stmt = select(WebhookSigningKey).where(WebhookSigningKey.project_id == project_id)
    if not include_revoked:
        stmt = stmt.where(WebhookSigningKey.revoked_at.is_(None))
    # Sort: active first (revoked_at NULL), then by created_at desc
    stmt = stmt.order_by(
        WebhookSigningKey.revoked_at.is_not(None),  # False (active) sorts first
        WebhookSigningKey.created_at.desc(),
    )
    result = await session.scalars(stmt)
    return [_row_to_dto(r) for r in result.all()]


async def list_active_keys_all_projects(
    session: AsyncSession,
) -> list[tuple[uuid.UUID, bytes]]:
    """Return (project_id, secret_hash) for every active signing key.

    Used at boot time by the keystore-restore routine. The operator
    supplies plaintexts via STRATHON_WEBHOOK_SIGNING_SECRETS; we hash
    each one and match against this list to know which project to
    register it under.
    """
    stmt = (
        select(WebhookSigningKey.project_id, WebhookSigningKey.secret_hash)
        .where(WebhookSigningKey.revoked_at.is_(None))
    )
    result = await session.execute(stmt)
    return [(row[0], bytes(row[1])) for row in result.all()]


async def create_key(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> CreateSigningKeyResult:
    """Create a new signing key for the project.

    Returns the row plus the plaintext. The caller is responsible for:
      (a) returning the plaintext to the operator in the HTTP response
      (b) pushing the plaintext into the in-process keystore so the
          next delivery can sign with it

    The plaintext is generated server-side by create_signing_key();
    operators do not supply their own. This matches Stripe's model and
    means we never accept attacker-influenced key material.
    """
    plaintext, prefix, secret_hash = create_signing_key()

    row = WebhookSigningKey(
        project_id=project_id,
        prefix=prefix,
        secret_hash=secret_hash,
    )
    session.add(row)
    await session.flush()  # populate server defaults (id, created_at)

    logger.info(
        "Created webhook signing key %s (prefix=%s) for project %s",
        row.id, prefix, project_id,
    )
    return CreateSigningKeyResult(row=_row_to_dto(row), plaintext=plaintext)


async def revoke_key(
    session: AsyncSession,
    key_id: uuid.UUID,
    project_id: uuid.UUID,
) -> SigningKeyRow | None:
    """Mark a key as revoked. Idempotent.

    Returns the updated row, or None if no such key exists for the
    given project (404 territory at the API layer). We scope the lookup
    by project_id so a key id leak from one project can't be used to
    revoke a key in another project — defense in depth on top of the
    auth dependency.

    Already-revoked rows are returned unchanged (the second revoke is
    a no-op). The caller is expected to also remove the plaintext from
    the in-process keystore.
    """
    existing = await session.scalar(
        select(WebhookSigningKey).where(
            WebhookSigningKey.id == key_id,
            WebhookSigningKey.project_id == project_id,
        )
    )
    if existing is None:
        return None
    if existing.revoked_at is not None:
        # Already revoked; return current row as-is
        return _row_to_dto(existing)

    await session.execute(
        update(WebhookSigningKey)
        .where(WebhookSigningKey.id == key_id)
        .values(revoked_at=datetime.now(tz=existing.created_at.tzinfo))
    )
    await session.flush()
    # Re-fetch to pick up the new revoked_at
    refreshed = await session.scalar(
        select(WebhookSigningKey).where(WebhookSigningKey.id == key_id)
    )
    logger.info(
        "Revoked webhook signing key %s (prefix=%s) for project %s",
        key_id, existing.prefix, project_id,
    )
    return _row_to_dto(refreshed)


async def find_project_for_secret(
    session: AsyncSession,
    plaintext: str,
) -> uuid.UUID | None:
    """Find which project (if any) a given plaintext secret belongs to.

    Used at boot time by the keystore-restore routine. Compares hashes
    in constant time on the application side; the database query
    returns at most one row by definition since the hash collision
    space is 2^256.

    Returns None if no active key has this hash. A no-match means
    either the operator typo'd the secret in STRATHON_WEBHOOK_SIGNING_SECRETS
    or the corresponding key has been revoked; either way, we don't
    register it and we log so the operator can debug.
    """
    h = hash_secret(plaintext)
    row = await session.scalar(
        select(WebhookSigningKey).where(
            WebhookSigningKey.secret_hash == h,
            WebhookSigningKey.revoked_at.is_(None),
        )
    )
    return row.project_id if row else None


__all__ = [
    "CreateSigningKeyResult",
    "SigningKeyRow",
    "create_key",
    "find_project_for_secret",
    "list_active_keys_all_projects",
    "list_keys",
    "revoke_key",
]
