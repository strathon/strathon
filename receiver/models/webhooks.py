"""Webhook delivery and signing-key ORM models.

These tables back the receiver's reliable webhook layer (commit C).
The durable state lives here in Postgres; Dramatiq + Redis is the queue
that drives the actual HTTP sends. If Redis is unreachable, no work is
done — but no work is lost either, because the rows in webhook_deliveries
are the source of truth and a sweeper task re-enqueues stragglers.

Schema rationale lives in the migration
(receiver/alembic/versions/005_webhook_deliveries.py); this file is the
ORM mirror. Keep the two in sync — if you change one and not the other,
the receiver test_alembic_check_no_drift test will fail in CI.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    TIMESTAMP,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

if TYPE_CHECKING:
    pass


class WebhookSigningKey(Base):
    """Per-project HMAC signing secret used to sign outbound alert webhooks.

    Multiple active keys per project are allowed and are the mechanism for
    graceful key rotation: during a rotation window the receiver signs each
    delivery with every active key, concatenating the resulting v1
    signatures space-delimited in the webhook-signature header per the
    Standard Webhooks spec. A consumer that already accepts the old key
    keeps working; once the operator confirms the new key is rotated in
    everywhere, the old key is revoked (revoked_at <- NOW()) and only the
    new signature is emitted.

    secret_hash is SHA-256 of the plaintext whsec_ value. The plaintext
    is returned to the operator once at POST time and never persisted.
    If lost, the operator creates a new key — there is no recovery path,
    intentionally.

    prefix is the four-character public identifier ('k7m4' etc.) shown in
    operator UIs so a row can be referenced without revealing the secret.
    """

    __tablename__ = "webhook_signing_keys"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    prefix: Mapped[str] = mapped_column(Text, nullable=False)
    secret_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True)
    )

    __table_args__ = (
        CheckConstraint(
            "char_length(prefix) = 4",
            name="webhook_signing_keys_prefix_len",
        ),
        Index(
            "idx_webhook_signing_keys_project",
            "project_id",
            text("revoked_at NULLS FIRST"),
            text("created_at DESC"),
        ),
    )


class WebhookDelivery(Base):
    """One row per webhook send attempt to a destination URL.

    The row is inserted in the same transaction as the matching
    policy_matches row, so a successfully-recorded match always has a
    corresponding delivery row (atomicity by Postgres construction).
    Dramatiq then drives the actual HTTP send with exponential-backoff
    retries; this row is updated in place as the delivery progresses.

    Status transitions:
        pending          -> succeeded           (2xx response)
        pending          -> failed_retrying     (5xx/timeout, more attempts left)
        failed_retrying  -> succeeded           (a later attempt got 2xx)
        failed_retrying  -> dlq                 (max_attempts reached)
        pending          -> abandoned           (4xx non-429, won't help to retry)

    webhook_id is the Standard Webhooks msg id sent on every attempt as
    the webhook-id header. It is stable across retries so the consumer
    can use it as an idempotency key.
    """

    __tablename__ = "webhook_deliveries"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    policy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("policies.id", ondelete="CASCADE"),
        nullable=False,
    )
    webhook_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("8")
    )

    next_attempt_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    last_attempt_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True)
    )
    last_response_status: Mapped[Optional[int]] = mapped_column(Integer)
    last_error: Mapped[Optional[str]] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'succeeded', 'failed_retrying', 'dlq', 'abandoned')",
            name="webhook_deliveries_status_valid",
        ),
        CheckConstraint(
            "attempts >= 0 AND attempts <= max_attempts",
            name="webhook_deliveries_attempts_nonneg",
        ),
        Index(
            "idx_webhook_deliveries_due",
            "next_attempt_at",
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "idx_webhook_deliveries_project",
            "project_id",
            "status",
            text("created_at DESC"),
        ),
    )
