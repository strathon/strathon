"""OTel trace, span, event, link models.

This is the hot path. Every ingested span lands as a row in `spans`,
optionally with rows in `span_events` and `span_links`. Traces are an
aggregate above spans, populated on first-span-seen and updated as the
trace progresses.

Schema design notes:
- trace_id and span_id are stored as BYTEA — 16 and 8 bytes respectively
  per the OTel spec. Mapped to Python `bytes`. We never store them as hex
  strings; the round-trip cost was measured to be significant at high
  span rates.
- Many gen_ai.* and strathon.* attributes are denormalized as columns
  for indexed queries. The full original attributes blob still lives in
  the JSONB `attributes` column. Repos extract from JSONB and populate
  both at ingest time.
- spans uses a composite PK (trace_id, span_id) since span_id is unique
  only within a trace.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
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

from .base import Base

if TYPE_CHECKING:
    from .core import Project


class Trace(Base):
    """OTel trace. One row per trace_id."""

    __tablename__ = "traces"

    # 16-byte OTel trace_id, stored raw
    id: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )

    start_time_unix_nano: Mapped[int] = mapped_column(BigInteger, nullable=False)
    end_time_unix_nano: Mapped[Optional[int]] = mapped_column(BigInteger)
    # 8-byte OTel span_id of the root span
    root_span_id: Mapped[Optional[bytes]] = mapped_column(LargeBinary)

    # Denormalized for fast dashboard queries
    agent_name: Mapped[Optional[str]] = mapped_column(Text)
    workflow_name: Mapped[Optional[str]] = mapped_column(Text)
    conversation_id: Mapped[Optional[str]] = mapped_column(Text)
    git_commit_sha: Mapped[Optional[str]] = mapped_column(Text)

    # Cost rollup, computed when the trace closes
    total_cost_usd: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 6), server_default=text("0")
    )
    total_input_tokens: Mapped[Optional[int]] = mapped_column(
        Integer, server_default=text("0")
    )
    total_output_tokens: Mapped[Optional[int]] = mapped_column(
        Integer, server_default=text("0")
    )
    span_count: Mapped[Optional[int]] = mapped_column(Integer, server_default=text("0"))

    intervention_state: Mapped[Optional[str]] = mapped_column(
        Text, server_default=text("'running'")
    )
    halt_reason: Mapped[Optional[str]] = mapped_column(Text)

    trace_metadata: Mapped[Optional[dict[str, Any]]] = mapped_column(
        "metadata", JSONB, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    # Relationships
    project: Mapped["Project"] = relationship(back_populates="traces")
    spans: Mapped[list["Span"]] = relationship(
        back_populates="trace", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "intervention_state IN ('running', 'paused', 'halted', 'completed')",
            name="traces_intervention_state_check",
        ),
        Index(
            "idx_traces_project_time",
            "project_id",
            text("start_time_unix_nano DESC"),
        ),
        Index(
            "idx_traces_project_agent",
            "project_id",
            "agent_name",
            postgresql_where=text("agent_name IS NOT NULL"),
        ),
        Index(
            "idx_traces_git_commit",
            "project_id",
            "git_commit_sha",
            postgresql_where=text("git_commit_sha IS NOT NULL"),
        ),
        Index(
            "idx_traces_intervention",
            "project_id",
            "intervention_state",
            postgresql_where=text("intervention_state != 'running'"),
        ),
    )


class Span(Base):
    """OTel span. Composite PK on (trace_id, span_id)."""

    __tablename__ = "spans"

    trace_id: Mapped[bytes] = mapped_column(
        LargeBinary,
        ForeignKey("traces.id", ondelete="CASCADE"),
        primary_key=True,
    )
    span_id: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    parent_span_id: Mapped[Optional[bytes]] = mapped_column(LargeBinary)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    # OTel core
    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    start_time_unix_nano: Mapped[int] = mapped_column(BigInteger, nullable=False)
    end_time_unix_nano: Mapped[Optional[int]] = mapped_column(BigInteger)
    status_code: Mapped[Optional[str]] = mapped_column(Text)
    status_message: Mapped[Optional[str]] = mapped_column(Text)

    # gen_ai.* denormalized
    operation_name: Mapped[Optional[str]] = mapped_column(Text)
    provider_name: Mapped[Optional[str]] = mapped_column(Text)
    request_model: Mapped[Optional[str]] = mapped_column(Text)
    response_model: Mapped[Optional[str]] = mapped_column(Text)
    agent_name: Mapped[Optional[str]] = mapped_column(Text)
    agent_id: Mapped[Optional[str]] = mapped_column(Text)
    tool_name: Mapped[Optional[str]] = mapped_column(Text)
    workflow_name: Mapped[Optional[str]] = mapped_column(Text)
    conversation_id: Mapped[Optional[str]] = mapped_column(Text)

    input_tokens: Mapped[Optional[int]] = mapped_column(Integer)
    output_tokens: Mapped[Optional[int]] = mapped_column(Integer)
    reasoning_tokens: Mapped[Optional[int]] = mapped_column(Integer)
    cache_read_tokens: Mapped[Optional[int]] = mapped_column(Integer)
    cache_creation_tokens: Mapped[Optional[int]] = mapped_column(Integer)

    # strathon.agent.* denormalized
    agent_depth: Mapped[Optional[int]] = mapped_column(Integer)
    spawn_parent_agent_id: Mapped[Optional[str]] = mapped_column(Text)
    spawn_reason: Mapped[Optional[str]] = mapped_column(Text)
    cost_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6))
    cost_cumulative_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6))
    tokens_subtree_input: Mapped[Optional[int]] = mapped_column(Integer)
    tokens_subtree_output: Mapped[Optional[int]] = mapped_column(Integer)
    cost_subtree_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6))

    intervention_state: Mapped[Optional[str]] = mapped_column(Text)
    halt_reason: Mapped[Optional[str]] = mapped_column(Text)

    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    # Relationships
    trace: Mapped["Trace"] = relationship(back_populates="spans")
    events: Mapped[list["SpanEvent"]] = relationship(
        back_populates="span",
        primaryjoin=(
            "and_(SpanEvent.trace_id == Span.trace_id, "
            "SpanEvent.span_id == Span.span_id)"
        ),
        cascade="all, delete-orphan",
    )
    links: Mapped[list["SpanLink"]] = relationship(
        back_populates="span",
        primaryjoin=(
            "and_(SpanLink.trace_id == Span.trace_id, "
            "SpanLink.span_id == Span.span_id)"
        ),
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        CheckConstraint(
            "kind IN ('CLIENT', 'INTERNAL', 'SERVER', 'PRODUCER', 'CONSUMER', 'UNSPECIFIED')",
            name="spans_kind_check",
        ),
        CheckConstraint(
            "status_code IN ('OK', 'ERROR', 'UNSET')",
            name="spans_status_code_check",
        ),
        Index(
            "idx_spans_project_time",
            "project_id",
            text("start_time_unix_nano DESC"),
        ),
        Index("idx_spans_trace_time", "trace_id", "start_time_unix_nano"),
        Index("idx_spans_parent", "trace_id", "parent_span_id"),
        Index(
            "idx_spans_agent",
            "project_id",
            "agent_name",
            text("start_time_unix_nano DESC"),
            postgresql_where=text("agent_name IS NOT NULL"),
        ),
        Index(
            "idx_spans_tool",
            "project_id",
            "tool_name",
            text("start_time_unix_nano DESC"),
            postgresql_where=text("tool_name IS NOT NULL"),
        ),
        Index(
            "idx_spans_operation",
            "project_id",
            "operation_name",
            text("start_time_unix_nano DESC"),
            postgresql_where=text("operation_name IS NOT NULL"),
        ),
        Index(
            "idx_spans_intervention",
            "project_id",
            "intervention_state",
            postgresql_where=text("intervention_state IS NOT NULL"),
        ),
        # Budget aggregation hot path. The monitor's per-budget query
        # is SUM(cost_usd) WHERE project_id = ? AND end_time > N.
        # Partial index keeps the size bounded — only LLM spans get
        # cost_usd populated, which is a fraction of total span volume.
        Index(
            "idx_spans_cost_window",
            "project_id",
            "end_time_unix_nano",
            postgresql_where=text("cost_usd IS NOT NULL"),
        ),
        # GIN index for JSONB attribute containment queries (@>).
        # Uses jsonb_path_ops operator class for a compact index that
        # supports the @> operator used by span search filtering.
        # Created by migration 011.
        Index(
            "idx_spans_attributes_gin",
            "attributes",
            postgresql_using="gin",
            postgresql_ops={"attributes": "jsonb_path_ops"},
        ),
    )


class SpanEvent(Base):
    """OTel span event — used for intervention moments. Append-only audit row."""

    __tablename__ = "span_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trace_id: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    span_id: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    time_unix_nano: Mapped[int] = mapped_column(BigInteger, nullable=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    # Relationships
    span: Mapped["Span"] = relationship(back_populates="events")

    __table_args__ = (
        ForeignKeyConstraint(
            ["trace_id", "span_id"],
            ["spans.trace_id", "spans.span_id"],
            ondelete="CASCADE",
        ),
        Index("idx_span_events_trace", "trace_id", "time_unix_nano"),
        Index(
            "idx_span_events_intervention",
            "project_id",
            text("time_unix_nano DESC"),
            postgresql_where=text(
                "name IN ('strathon.intervention.blocked', "
                "'strathon.intervention.steered', "
                "'strathon.intervention.budget_exceeded', "
                "'strathon.intervention.loop_detected')"
            ),
        ),
    )


class SpanLink(Base):
    """Non-tree edges between spans — tool→llm provenance, retry-from-checkpoint, etc."""

    __tablename__ = "span_links"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trace_id: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    span_id: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    linked_trace_id: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    linked_span_id: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    # Relationships
    span: Mapped["Span"] = relationship(back_populates="links")

    __table_args__ = (
        ForeignKeyConstraint(
            ["trace_id", "span_id"],
            ["spans.trace_id", "spans.span_id"],
            ondelete="CASCADE",
        ),
        Index("idx_span_links_span", "trace_id", "span_id"),
    )
