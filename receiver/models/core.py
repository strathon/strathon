"""Core tenant-and-auth models.

These four tables form the dependency root of the rest of the schema.
Every trace, span, policy, budget, etc. ultimately references projects(id).
api_keys gate request authentication. sessions back the dashboard's auth
flow (not yet wired but the table exists). project_settings holds
per-project knobs (retention, content capture, intervention defaults).
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
    TIMESTAMP,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin

if TYPE_CHECKING:
    from .identity import ProjectMember
    from .intervention import Budget
    from .policies import Policy
    from .traces import Trace


# ----- Project ----------------------------------------------------------

class Project(Base, TimestampMixin):
    """Tenant unit. Everything else hangs off a project."""

    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))

    # Relationships
    api_keys: Mapped[list["ApiKey"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    settings: Mapped[Optional["ProjectSettings"]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        uselist=False,
    )
    traces: Mapped[list["Trace"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    policies: Mapped[list["Policy"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    members: Mapped[list["ProjectMember"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    budgets: Mapped[list["Budget"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index(
            "idx_projects_slug",
            "slug",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


# ----- ApiKey -----------------------------------------------------------

class ApiKey(Base):
    """API key for project-scoped auth. SHA-256 hash stored, never the raw value."""

    __tablename__ = "api_keys"

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
    name: Mapped[str] = mapped_column(Text, nullable=False)
    key_hash: Mapped[str] = mapped_column(Text, nullable=False)
    key_prefix: Mapped[str] = mapped_column(Text, nullable=False)
    # Capability scopes. '*' is the wildcard granting full access. The full
    # set of valid scope strings lives in receiver/auth.py:KNOWN_SCOPES.
    # Defaulted server-side so the schema is the source of truth: every
    # row has at least the SDK-friendly defaults even if the application
    # forgets to set them explicitly.
    scopes: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        nullable=False,
        server_default=text("ARRAY['traces:write', 'policies:read']::text[]"),
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    last_used_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    revoked_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))

    # Relationships
    project: Mapped["Project"] = relationship(back_populates="api_keys")

    __table_args__ = (
        Index(
            "idx_api_keys_project",
            "project_id",
            postgresql_where=text("revoked_at IS NULL"),
        ),
        Index(
            "idx_api_keys_prefix",
            "key_prefix",
            postgresql_where=text("revoked_at IS NULL"),
        ),
        CheckConstraint(
            "cardinality(scopes) > 0",
            name="api_keys_scopes_not_empty",
        ),
    )


# ----- Session ----------------------------------------------------------

class Session(Base):
    """Dashboard auth session. Not wired up yet; schema exists for when it is.

    Named `Session` in code but `sessions` in the DB. The SQLAlchemy
    `Session` class (sync) is in `sqlalchemy.orm.Session`; we never import
    that one here. The async session is `AsyncSession` from
    `sqlalchemy.ext.asyncio`. So the names don't collide in practice.
    """

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    # INET maps to ipaddress.IPv4Address / IPv6Address; we keep it loose
    # as a string-ish via SQLAlchemy's INET dialect type. asyncpg used to
    # return strings; psycopg3 returns ipaddress objects. Annotate as Any
    # so the type matches whatever the driver returns.
    ip_address: Mapped[Optional[Any]] = mapped_column(INET)
    user_agent: Mapped[Optional[str]] = mapped_column(Text)

    __table_args__ = (
        Index("idx_sessions_user", "user_id"),
        Index("idx_sessions_token", "token_hash"),
    )


# ----- ProjectSettings --------------------------------------------------

class ProjectSettings(Base):
    """Per-project knobs. One row per project, project_id as PK."""

    __tablename__ = "project_settings"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    pii_redaction_enabled: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("true")
    )
    pii_redaction_patterns: Mapped[Optional[list[Any]]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    content_capture_enabled: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false")
    )
    trace_retention_days: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("30")
    )
    intervention_default_action: Mapped[Optional[str]] = mapped_column(
        Text, server_default=text("'allow'")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
    )

    # Relationships
    project: Mapped["Project"] = relationship(back_populates="settings")

    __table_args__ = (
        CheckConstraint(
            "intervention_default_action IN ('allow', 'block')",
            name="project_settings_intervention_default_action_check",
        ),
    )
