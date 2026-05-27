"""User identity and project membership.

Users authenticate via email+password (Argon2id) for dashboard access.
GitHub OAuth fields are optional (future SSO linking). Project membership
assigns one of four fixed roles: owner, admin, operator, viewer. SDK API
keys remain scope-based and don't flow through the role system.

Role definitions (enforced by rbac.py, checked by require_role):
  owner:    full access, can delete project and manage all members
  admin:    full access except project deletion, can manage non-owner members
  operator: read/write on policies, halts, budgets, webhooks, traces read
  viewer:   read-only on all resources
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import TIMESTAMP, BigInteger, Boolean, CheckConstraint, ForeignKey, Index, Text, func, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .core import Project


class User(Base):
    """Dashboard user. Email+password is the primary identity for v1."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    email: Mapped[Optional[str]] = mapped_column(Text)
    password_hash: Mapped[Optional[str]] = mapped_column(Text)
    display_name: Mapped[Optional[str]] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    # GitHub OAuth fields — optional, for future SSO linking
    github_id: Mapped[Optional[int]] = mapped_column(BigInteger, unique=True)
    github_username: Mapped[Optional[str]] = mapped_column(Text)
    avatar_url: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    last_login_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    totp_secret: Mapped[Optional[str]] = mapped_column(
        Text, comment="Base32-encoded TOTP secret. Null when MFA not set up.",
    )
    mfa_enabled: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("FALSE"),
        comment="Whether TOTP MFA is active for this account.",
    )
    backup_codes: Mapped[Optional[list[str]]] = mapped_column(
        ARRAY(Text), nullable=True,
        comment="SHA-256 hashed single-use backup codes.",
    )

    __table_args__ = (
        Index("idx_users_github_id", "github_id"),
        Index(
            "idx_users_email_lower",
            text("LOWER(email)"),
            unique=True,
            postgresql_where=text("email IS NOT NULL"),
        ),
    )


class ProjectMember(Base):
    """Composite-PK join between users and projects with a role."""

    __tablename__ = "project_members"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    invited_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    accepted_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    invited_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id")
    )

    # Relationships
    project: Mapped["Project"] = relationship(back_populates="members")

    __table_args__ = (
        CheckConstraint(
            "role IN ('owner', 'admin', 'operator', 'viewer')",
            name="project_members_role_check",
        ),
        Index("idx_project_members_user", "user_id"),
    )


class PasswordResetToken(Base):
    """Token for secure password reset flow.

    Token is SHA-256 hashed before storage. The raw token is sent via
    email (or returned to admin for admin-reset). Expires after 1 hour.
    Single-use: used_at is set on consumption.
    """

    __tablename__ = "password_reset_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False,
    )
    used_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (
        Index("idx_password_reset_tokens_user", "user_id"),
        Index("idx_password_reset_tokens_hash", "token_hash"),
    )
