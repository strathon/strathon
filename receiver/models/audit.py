"""ORM models for the audit log data plane.

Three tables in the dedicated ``audit`` schema:

- ``audit.events`` — every operator mutation, partitioned monthly by
  ``occurred_at``. Append-only at the DB level via triggers and
  revoked grants; this module never UPDATEs or DELETEs an event.

- ``audit.anchors`` — per-interval Merkle root checkpoints over
  ``audit.events`` for tamper detection.

- ``audit.streams`` — operator-registered webhook destinations that
  receive every committed audit event.

All three live in a separate Postgres schema so the access boundary
between control-plane data and audit data is enforceable at the
catalog level. SQLAlchemy ``__table_args__ = {"schema": "audit"}``
puts them there.

The hash chain columns (``prev_hash``, ``row_hash``) are computed at
the application layer; see :mod:`audit.hash_chain`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Integer,
    LargeBinary,
    SmallInteger,
    TIMESTAMP,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class AuditEvent(Base):
    """One row per operator mutation.

    The PRIMARY KEY ``(occurred_at, id)`` includes the partition key
    as required by Postgres; the ``id`` UUID is globally unique with
    overwhelming probability and identifies the event end-to-end.
    """

    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint(
            "actor_type IN ('human','service_account','agent','system','anonymous')",
            name="events_actor_type_check",
        ),
        CheckConstraint(
            "outcome IN ('allow','deny','error','partial')",
            name="events_outcome_check",
        ),
        {"schema": "audit"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    # BIGSERIAL backed by the schema-local sequence
    # audit.events_sequence_no_seq. The PK on (occurred_at, id) makes
    # this column unconstrained at the table level, but the sequence
    # is monotonic per schema which is what the hash-chain logic needs.
    sequence_no: Mapped[int] = mapped_column(BigInteger, nullable=False)

    occurred_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        primary_key=True,
        nullable=False,
    )
    ingested_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("clock_timestamp()"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )

    # Actor
    actor_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[str] = mapped_column(Text, nullable=False)
    actor_display: Mapped[Optional[str]] = mapped_column(Text)
    on_behalf_of: Mapped[Optional[str]] = mapped_column(Text)

    # Action
    action: Mapped[str] = mapped_column(Text, nullable=False)
    action_category: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text)

    # Resource
    resource_type: Mapped[str] = mapped_column(Text, nullable=False)
    resource_id: Mapped[str] = mapped_column(Text, nullable=False)
    resource_parent: Mapped[Optional[str]] = mapped_column(Text)
    cascade_root_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True)
    )

    # Request envelope
    request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    source_ip: Mapped[Optional[str]] = mapped_column(INET)
    user_agent: Mapped[Optional[str]] = mapped_column(Text)
    api_key_id: Mapped[Optional[str]] = mapped_column(Text)
    auth_method: Mapped[Optional[str]] = mapped_column(Text)

    # Change payload
    before_state: Mapped[Optional[dict]] = mapped_column(JSONB)
    after_state: Mapped[Optional[dict]] = mapped_column(JSONB)
    diff: Mapped[Optional[list]] = mapped_column(JSONB)

    # Integrity
    prev_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    row_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    hmac_key_id: Mapped[int] = mapped_column(SmallInteger, nullable=False)

    # Compliance metadata
    pii_classes: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        nullable=False,
        server_default=text("'{}'::text[]"),
    )
    schema_version: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        server_default=text("1"),
    )


class AuditAnchor(Base):
    """Per-interval Merkle root over ``audit.events``.

    The sealer worker computes one of these every
    ``audit_anchor_interval_seconds`` (default 60s), recording the
    final sequence number of the prior interval and the Merkle root
    over those events' ``row_hash`` values. Verification of a single
    event's integrity requires the row plus a Merkle inclusion proof
    against this anchor.

    ``signature`` and ``signing_key_id`` are nullable in Stage 1
    (anchors are plaintext-verifiable). Stage 2 wires KMS signing.
    """

    __tablename__ = "anchors"
    __table_args__ = {"schema": "audit"}

    anchor_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), primary_key=True
    )
    last_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_row_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    merkle_root: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    event_count: Mapped[int] = mapped_column(Integer, nullable=False)
    signature: Mapped[Optional[bytes]] = mapped_column(LargeBinary)
    signing_key_id: Mapped[Optional[str]] = mapped_column(Text)


class AuditStream(Base):
    """Operator-registered webhook destination for audit events.

    When an audit event is committed, the audit emit logic enqueues a
    webhook delivery per active stream whose category filter matches
    the event. Delivery rides the existing webhook_deliveries
    machinery.
    """

    __tablename__ = "streams"
    __table_args__ = {"schema": "audit"}

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    signing_key_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True)
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("TRUE")
    )
    paused_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True)
    )
    pause_reason: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("clock_timestamp()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("clock_timestamp()"),
    )
    categories: Mapped[Optional[list[str]]] = mapped_column(ARRAY(Text))
