"""Intervention bookkeeping: budgets, halt state, and the decision audit log."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    TIMESTAMP,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin

if TYPE_CHECKING:
    from .core import Project


class Budget(Base, TimestampMixin):
    """Per-project spend cap with optional parent-budget rollup."""

    __tablename__ = "budgets"

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

    max_spend_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    spent_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, server_default=text("0")
    )
    soft_limit_ratio: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(4, 3), server_default=text("0.9")
    )

    parent_budget_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("budgets.id", ondelete="SET NULL"),
    )

    max_repeated_calls: Mapped[Optional[int]] = mapped_column(Integer)
    loop_window_seconds: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 2))

    is_active: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))
    expires_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))

    # Relationships
    project: Mapped["Project"] = relationship(back_populates="budgets")

    __table_args__ = (
        Index(
            "idx_budgets_project",
            "project_id",
            postgresql_where=text("is_active = true"),
        ),
        Index(
            "idx_budgets_parent",
            "parent_budget_id",
            postgresql_where=text("parent_budget_id IS NOT NULL"),
        ),
    )


class HaltState(Base):
    """Write-ahead log of halt decisions. Append-only, survives restarts."""

    __tablename__ = "halt_state"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )

    # At least one of trace_id / agent_id / budget_id must be non-null (CHECK below)
    trace_id: Mapped[Optional[bytes]] = mapped_column(LargeBinary)
    agent_id: Mapped[Optional[str]] = mapped_column(Text)
    budget_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("budgets.id", ondelete="CASCADE"),
    )

    state: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)

    set_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    cleared_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    cleared_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
    )

    halt_metadata: Mapped[Optional[dict[str, Any]]] = mapped_column(
        "metadata", JSONB, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (
        CheckConstraint(
            "state IN ('paused', 'halted', 'resumed', 'cleared')",
            name="halt_state_state_check",
        ),
        CheckConstraint(
            "actor IN ('budget_monitor', 'loop_detector', 'user', "
            "'policy_engine', 'system')",
            name="halt_state_actor_check",
        ),
        CheckConstraint(
            "trace_id IS NOT NULL OR agent_id IS NOT NULL OR budget_id IS NOT NULL",
            name="chk_halt_scope",
        ),
        Index(
            "idx_halt_state_trace",
            "project_id",
            "trace_id",
            text("set_at DESC"),
            postgresql_where=text("trace_id IS NOT NULL"),
        ),
        Index(
            "idx_halt_state_agent",
            "project_id",
            "agent_id",
            text("set_at DESC"),
            postgresql_where=text("agent_id IS NOT NULL"),
        ),
        Index(
            "idx_halt_state_active",
            "project_id",
            "state",
            text("set_at DESC"),
            postgresql_where=text("cleared_at IS NULL"),
        ),
    )


class InterventionLog(Base):
    """Decision audit: every allow/block/pause/resume the SDK reported."""

    __tablename__ = "intervention_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    trace_id: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    span_id: Mapped[Optional[bytes]] = mapped_column(LargeBinary)

    decision: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text)

    estimated_cost_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6))
    budget_remaining_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6))
    loop_count: Mapped[Optional[int]] = mapped_column(Integer)

    decided_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    log_metadata: Mapped[Optional[dict[str, Any]]] = mapped_column(
        "metadata", JSONB, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (
        CheckConstraint(
            "decision IN ('allowed', 'blocked', 'paused', 'resumed')",
            name="intervention_log_decision_check",
        ),
        Index("idx_intervention_log_trace", "trace_id", text("decided_at DESC")),
        Index("idx_intervention_log_project_time", "project_id", text("decided_at DESC")),
    )
