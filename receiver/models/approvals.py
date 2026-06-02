"""Approval model for human-in-the-loop workflow."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    JSON,
    LargeBinary,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base


class Approval(Base):
    """Pending human approval for a require_approval policy match."""

    __tablename__ = "approvals"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    policy_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    trace_id: Mapped[Optional[bytes]] = mapped_column(LargeBinary)
    span_id: Mapped[Optional[bytes]] = mapped_column(LargeBinary)
    span_name: Mapped[Optional[str]] = mapped_column(Text)
    tool_name: Mapped[Optional[str]] = mapped_column(Text)
    tool_args: Mapped[Optional[str]] = mapped_column(Text)
    policy_name: Mapped[Optional[str]] = mapped_column(Text)

    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'"),
    )
    requested_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()"),
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    resolved_by: Mapped[Optional[str]] = mapped_column(Text)
    timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("300"),
    )
    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False,
        comment="requested_at + timeout_seconds. Background reaper checks this.",
    )
    approvers_required: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1"),
        comment="Number of approvals needed to resolve as approved.",
    )
    current_approvals: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"),
        comment="Running count of approve decisions received.",
    )
    approval_decisions: Mapped[list] = mapped_column(
        JSON, nullable=False, server_default=text("'[]'::json"),
        comment="Array of {actor, decision, timestamp} records.",
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1"),
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'denied', 'expired')",
            name="approvals_status_check",
        ),
        Index(
            "idx_approvals_project_status",
            "project_id", "status",
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "idx_approvals_expires_at",
            "expires_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    def to_json(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "project_id": str(self.project_id),
            "policy_id": str(self.policy_id),
            "trace_id": self.trace_id.hex() if self.trace_id else None,
            "span_id": self.span_id.hex() if self.span_id else None,
            "span_name": self.span_name,
            "tool_name": self.tool_name,
            "tool_args": self.tool_args,
            "policy_name": self.policy_name,
            "status": self.status,
            "requested_at": self.requested_at.isoformat() if self.requested_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "resolved_by": self.resolved_by,
            "timeout_seconds": self.timeout_seconds,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "approvers_required": self.approvers_required,
            "current_approvals": self.current_approvals,
            "approval_decisions": self.approval_decisions or [],
        }
