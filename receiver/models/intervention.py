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
    UniqueConstraint,
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

    max_spend_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6))
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

    # Scope dimensions (added in migration 007). scope is one of
    # 'project' | 'agent' | 'model'; scope_value is the agent_id /
    # model_name / NULL for project-scope. The schema doesn't constrain
    # the shape per-scope; the repository validates and the API surface
    # rejects invalid combinations.
    scope: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'project'"),
    )
    scope_value: Mapped[Optional[str]] = mapped_column(Text)

    # Fixed-window reset model. budget_duration is '1h' | '1d' | '7d' |
    # '30d' for v1; the column is TEXT so future commits can add e.g.
    # '12h' without a schema change. budget_reset_at is when this
    # window's spend counter rolls over — computed on create, advanced
    # by the monitor as windows cross.
    budget_duration: Mapped[Optional[str]] = mapped_column(Text)
    budget_reset_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))

    # Bookkeeping: the monitor stamps this on each tick so we can
    # surface "last checked X seconds ago" in dashboards and so the
    # monitor can prioritize budgets it hasn't seen recently.
    last_evaluated_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))

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
        Index(
            "idx_budgets_active_for_monitor",
            "project_id",
            text("last_evaluated_at NULLS FIRST"),
            postgresql_where=text("is_active = true"),
        ),
        CheckConstraint(
            "scope IN ('project', 'agent', 'model')",
            name="budgets_scope_check",
        ),
        CheckConstraint(
            "budget_duration IN ('1h', '1d', '7d', '30d') OR budget_duration IS NULL",
            name="budgets_duration_check",
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


class ModelPriceOverride(Base, TimestampMixin):
    """Per-project, per-model price override.

    The receiver ships a vendored model_prices.json catalog as the
    default; this table is where operators express "we negotiated a
    discount with our provider, our gpt-4o is cheaper than the
    sticker price". Cost computation at ingest checks this table
    first, then falls back to the vendored catalog.

    Unique on (project_id, model_name) so each model has at most one
    override per project. The CHECK constraint on non-negative prices
    is defensive: an operator typo of -0.001 would otherwise produce
    negative spend, which breaks every downstream aggregation.
    """

    __tablename__ = "model_price_overrides"

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
    model_name: Mapped[str] = mapped_column(Text, nullable=False)

    input_cost_per_token: Mapped[Decimal] = mapped_column(
        Numeric(16, 12), nullable=False,
    )
    output_cost_per_token: Mapped[Decimal] = mapped_column(
        Numeric(16, 12), nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "project_id", "model_name",
            name="model_price_overrides_project_model_unique",
        ),
        CheckConstraint(
            "input_cost_per_token >= 0 AND output_cost_per_token >= 0",
            name="model_price_overrides_nonnegative",
        ),
        Index("idx_model_price_overrides_project", "project_id"),
    )
