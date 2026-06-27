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
    BigInteger,
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


# ----- Organization -----------------------------------------------------


class Organization(Base, TimestampMixin):
    """Tenancy layer above projects. One organization owns many projects.

    On self-host there is a single default organization and it is largely
    invisible. On hosted/cloud deployments each customer is an organization.
    The ``cloud_*`` columns are null and unused on self-host; they exist so
    hosted billing/usage metering can attach without a schema change.
    """

    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))

    # Hosted-only fields; null on self-host.
    cloud_plan: Mapped[Optional[str]] = mapped_column(Text)
    cloud_billing_cycle_anchor: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True)
    )
    cloud_current_cycle_usage: Mapped[Optional[int]] = mapped_column(BigInteger)
    cloud_config: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)

    projects: Mapped[list["Project"]] = relationship(back_populates="organization")


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
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))

    # Relationships
    organization: Mapped["Organization"] = relationship(back_populates="projects")
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
            "uq_projects_org_slug",
            "org_id",
            "slug",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("idx_projects_org", "org_id"),
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
    deprecated_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True),
        comment="Set on rotation. Key still works until expires_at.",
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True),
        comment="Hard expiry. Auth rejects after this timestamp.",
    )
    rotated_from_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("api_keys.id", ondelete="SET NULL"),
        nullable=True,
        comment="Links replacement key to the deprecated key it replaced.",
    )
    allowed_ips: Mapped[Optional[list[str]]] = mapped_column(
        ARRAY(Text),
        nullable=True,
        comment=(
            "Optional IP allowlist. If set, requests from IPs not "
            "in this list are rejected. Null means allow all."
        ),
    )

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
        Index(
            "idx_api_keys_expires_at",
            "expires_at",
            postgresql_where=text("expires_at IS NOT NULL AND revoked_at IS NULL"),
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
    # as a string-ish via SQLAlchemy's INET dialect type. Drivers differ on
    # the returned type (string vs ipaddress object), so annotate as Any to
    # match whatever the driver returns.
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
    # Per-entity actions for matches by the default + custom value
    # patterns. Keys are entity names (EMAIL_ADDRESS, CREDIT_CARD,
    # etc.); values are one of redact/mask/hash. Missing entries
    # default to "redact" in the redactor. See migration 006.
    pii_redaction_strategy: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # Whole-attribute actions keyed by attribute name. Supports
    # redact/mask/hash/delete (delete is meaningless for value-pattern
    # matches and only valid here). Empty = no key-level redaction.
    pii_redaction_key_actions: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # Allowlist mode: if non-empty, ONLY these attribute keys survive
    # the redactor. Strongest privacy posture, deny-by-default.
    pii_attribute_allowlist: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
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
