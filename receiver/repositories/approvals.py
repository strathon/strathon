"""Repository for human approval workflow.

CRUD operations for approval records. Called by the ingest path (create),
the API endpoints (resolve), and the background reaper (expire).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy import func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession

from models.approvals import Approval

logger = logging.getLogger(__name__)


async def create_approval(
    session: AsyncSession,
    project_id: UUID,
    policy_id: UUID,
    *,
    trace_id: Optional[bytes] = None,
    span_id: Optional[bytes] = None,
    span_name: Optional[str] = None,
    agent_name: Optional[str] = None,
    tool_name: Optional[str] = None,
    tool_args: Optional[str] = None,
    policy_name: Optional[str] = None,
    timeout_seconds: int = 300,
    approvers_required: int = 1,
) -> Approval:
    """Create a pending approval. Returns the new row."""
    now = datetime.now(timezone.utc)
    approval = Approval(
        project_id=project_id,
        policy_id=policy_id,
        trace_id=trace_id,
        span_id=span_id,
        span_name=span_name,
        agent_name=agent_name,
        tool_name=tool_name,
        tool_args=tool_args,
        policy_name=policy_name,
        timeout_seconds=timeout_seconds,
        expires_at=now + timedelta(seconds=timeout_seconds),
        approvers_required=max(approvers_required, 1),
    )
    session.add(approval)
    await session.flush()
    await session.refresh(approval)
    return approval


async def list_approvals(
    session: AsyncSession,
    project_id: UUID,
    status: Optional[str] = None,
    limit: int = 100,
) -> list[Approval]:
    """List approvals for a project, newest first."""
    stmt = select(Approval).where(Approval.project_id == project_id)
    if status:
        stmt = stmt.where(Approval.status == status)
    stmt = stmt.order_by(Approval.requested_at.desc()).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_approval(
    session: AsyncSession,
    project_id: UUID,
    approval_id: UUID,
) -> Optional[Approval]:
    """Get a single approval by ID, scoped to project."""
    stmt = select(Approval).where(
        Approval.project_id == project_id,
        Approval.id == approval_id,
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_approval_by_id(
    session: AsyncSession, approval_id: UUID
) -> Optional[Approval]:
    """Look up an approval by ID alone, without project scoping.

    Used by trusted internal callers that have authenticated by another
    means (e.g. a verified Slack request signature) and only have the
    approval ID, not the owning project. Project-scoped reads should use
    get_approval instead.
    """
    stmt = select(Approval).where(Approval.id == approval_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def resolve_approval(
    session: AsyncSession,
    project_id: UUID,
    approval_id: UUID,
    decision: str,
    resolved_by: Optional[str] = None,
) -> Optional[Approval]:
    """Record an approve or deny decision on a pending approval.

    Multi-party logic:
    - **deny**: immediate veto. First deny sets status='denied' regardless
      of how many approvals have been collected.
    - **approve**: increments current_approvals. Only sets status='approved'
      when current_approvals >= approvers_required. Until then, status
      stays 'pending' and the caller sees the updated counts.

    Returns the updated row, or None if not found / already resolved.
    """
    if decision not in ("approved", "denied"):
        raise ValueError(f"decision must be 'approved' or 'denied', got {decision!r}")

    # Fetch the approval (must be pending). Use SELECT FOR UPDATE to
    # prevent concurrent approve/deny race conditions.
    stmt = select(Approval).where(
        Approval.project_id == project_id,
        Approval.id == approval_id,
        Approval.status == "pending",
    ).with_for_update()
    result = await session.execute(stmt)
    approval = result.scalar_one_or_none()
    if approval is None:
        return None

    now = datetime.now(timezone.utc)
    decision_record = {
        "actor": resolved_by or "unknown",
        "decision": decision,
        "timestamp": now.isoformat(),
    }

    # Check for duplicate approver (same actor can't approve twice).
    existing_actors = {
        d.get("actor") for d in (approval.approval_decisions or [])
        if d.get("decision") == "approved"
    }
    if decision == "approved" and resolved_by in existing_actors:
        return None  # Duplicate vote.

    # Append to the decisions array.
    existing_decisions = list(approval.approval_decisions or [])
    existing_decisions.append(decision_record)
    approval.approval_decisions = existing_decisions

    # Increment version for optimistic locking.
    approval.version = (getattr(approval, "version", 1) or 1) + 1

    if decision == "denied":
        # Immediate veto.
        approval.status = "denied"
        approval.resolved_at = now
        approval.resolved_by = resolved_by
    else:
        # Approve: increment count, check threshold.
        approval.current_approvals = (approval.current_approvals or 0) + 1
        if approval.current_approvals >= approval.approvers_required:
            approval.status = "approved"
            approval.resolved_at = now
            approval.resolved_by = resolved_by
        # else: stays pending, waiting for more approvals.

    await session.flush()
    await session.refresh(approval)
    return approval


async def expire_pending_approvals(session: AsyncSession) -> int:
    """Expire all pending approvals past their expires_at. Returns count."""
    now = datetime.now(timezone.utc)
    stmt = (
        update(Approval)
        .where(
            Approval.status == "pending",
            Approval.expires_at <= now,
        )
        .values(
            status="expired",
            resolved_at=sa_func.now(),
            resolved_by="timeout",
        )
    )
    result = await session.execute(stmt)
    count = result.rowcount
    if count:
        logger.info("Expired %d pending approval(s)", count)
    return count
