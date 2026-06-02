"""Runtime intervention policies and their audit log."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import (
    ARRAY,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    TIMESTAMP,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin

if TYPE_CHECKING:
    from .core import Project


class Policy(Base, TimestampMixin):
    """CEL-expression-based runtime policy."""

    __tablename__ = "policies"

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
    description: Mapped[Optional[str]] = mapped_column(Text)

    match_expression: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    action_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    applies_to: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    enabled: Mapped[bool] = mapped_column(nullable=False, server_default=text("TRUE"))
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    match_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"),
        comment="Cumulative count of spans this policy matched.",
    )
    last_matched_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True),
        comment="Timestamp of the most recent match.",
    )
    shadow: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("FALSE"),
        comment=(
            "Shadow mode: policy evaluates and records matches "
            "but does not enforce block/steer/throttle actions. "
            "Log and alert actions still fire."
        ),
    )

    # Relationships
    project: Mapped["Project"] = relationship(back_populates="policies")
    matches: Mapped[list["PolicyMatch"]] = relationship(
        back_populates="policy", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "action IN ('log', 'alert', 'block', 'steer')",
            name="policies_action_check",
        ),
        Index(
            "idx_policies_project_enabled",
            "project_id",
            "enabled",
            postgresql_where=text("enabled = TRUE"),
        ),
        Index(
            "idx_policies_priority",
            "project_id",
            text("priority DESC"),
            postgresql_where=text("enabled = TRUE"),
        ),
    )


class PolicyMatch(Base):
    """Audit row: every policy evaluation that matched."""

    __tablename__ = "policy_matches"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    policy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("policies.id", ondelete="CASCADE"),
        nullable=False,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    trace_id: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    span_id: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    action_outcome: Mapped[Optional[str]] = mapped_column(Text)
    matched_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    match_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    # Relationships
    policy: Mapped["Policy"] = relationship(back_populates="matches")

    __table_args__ = (
        Index("idx_policy_matches_policy", "policy_id", text("matched_at DESC")),
        Index("idx_policy_matches_project", "project_id", text("matched_at DESC")),
        Index("idx_policy_matches_trace", "trace_id"),
    )


class PolicyVersion(Base):
    """Snapshot of a policy at a point in time."""

    __tablename__ = "policy_versions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    policy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("policies.id", ondelete="CASCADE"),
        nullable=False,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)

    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    match_expression: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    action_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    applies_to: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    enabled: Mapped[bool] = mapped_column(nullable=False, server_default=text("TRUE"))
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    change_type: Mapped[str] = mapped_column(Text, nullable=False)
    changed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("idx_policy_versions_policy", "policy_id", text("version DESC")),
        UniqueConstraint(
            "policy_id", "version",
            name="policy_versions_policy_id_version_key",
        ),
    )
