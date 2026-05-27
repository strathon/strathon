"""Declarative base class for all ORM models.

All Strathon receiver models inherit from `Base` here. There is exactly
one DeclarativeBase per metadata namespace — multiple bases would mean
Alembic can't see them all under one `target_metadata`, defeating the
purpose of the refactor.

Shared column conventions:
- Primary keys: UUID with server-side gen_random_uuid() (or BIGSERIAL
  for high-volume append-only tables like span_events).
- Timestamps: TIMESTAMPTZ with server-side NOW() defaults so the DB,
  not the app, decides on time. This matters because app clocks can be
  wrong and historical comparisons need a single source of truth.
- JSONB columns: typed as `Mapped[dict[str, Any]]` via the postgres JSONB
  dialect type.
- BYTEA OTel trace/span ids: typed as `Mapped[bytes]`.

Mixins live here too — currently just `TimestampMixin` for tables that
have created_at/updated_at and benefit from a shared definition.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import TIMESTAMP, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Shared base. All models extend this."""

    # Inherit subclasses get strict type-checking of column annotations.
    pass


class TimestampMixin:
    """Adds standard created_at / updated_at columns with DB-side defaults.

    Used on tables that track their own lifecycle. Append-only audit tables
    (span_events, policy_matches, etc.) don't use this — they have a single
    insertion timestamp and never update.
    """

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        # Note: we do NOT set onupdate=func.now() at the SQLAlchemy level.
        # The existing schema relies on the application to bump updated_at,
        # which means migrations from raw SQL don't need a trigger. Repos
        # explicitly set updated_at when they mutate, mirroring the asyncpg
        # patterns we're replacing.
    )
