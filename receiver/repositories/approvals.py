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
    tool_name: Optional[str] = None,
    tool_args: Optional[str] = None,
    policy_name: Optional[str] = None,
    timeout_seconds: int = 300,
) -> Approval:
    """Create a pending approval. Returns the new row."""
    now = datetime.now(timezone.utc)
    approval = Approval(
        project_id=project_id,
        policy_id=policy_id,
        trace_id=trace_id,
        span_id=span_id,
        span_name=span_name,
        tool_name=tool_name,
        tool_args=tool_args,
        policy_name=policy_name,
        timeout_seconds=timeout_seconds,
        expires_at=now + timedelta(seconds=timeout_seconds),
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


async def resolve_approval(
    session: AsyncSession,
    project_id: UUID,
    approval_id: UUID,
    decision: str,
    resolved_by: Optional[str] = None,
) -> Optional[Approval]:
    """Approve or deny a pending approval. Returns updated row or None.

    Only pending approvals can be resolved. Already resolved or expired
    approvals return None.
    """
    if decision not in ("approved", "denied"):
        raise ValueError(f"decision must be 'approved' or 'denied', got {decision!r}")

    stmt = (
        update(Approval)
        .where(
            Approval.project_id == project_id,
            Approval.id == approval_id,
            Approval.status == "pending",
        )
        .values(
            status=decision,
            resolved_at=sa_func.now(),
            resolved_by=resolved_by,
        )
        .returning(Approval)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


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


async def get_approval_by_id_any_project(
    session: AsyncSession,
    approval_id: UUID,
) -> Optional[Approval]:
    """Get approval without project scoping — for webhook callback URLs."""
    stmt = select(Approval).where(Approval.id == approval_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()
