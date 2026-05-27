"""Human approval workflow endpoints.

  GET    /v1/approvals               list approvals (filter by status)
  GET    /v1/approvals/{id}          single approval
  POST   /v1/approvals/{id}/approve  approve a pending approval
  POST   /v1/approvals/{id}/deny     deny a pending approval

Scopes: approvals:read for GET, approvals:write for POST.
"""

from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

import auth as auth_mod
import repositories.approvals as approvals_repo
import repositories.audit as audit_repo
from audit.actions import (
    APPROVAL_APPROVE,
    APPROVAL_DENY,
    CATEGORY_APPROVAL,
)
from database import get_db_session

from ._deps import build_audit_context, require_scope

router = APIRouter(prefix="/v1/approvals", tags=["approvals"])


@router.get("")
async def list_approvals(
    request: Request,
    status_filter: Optional[str] = None,
    limit: int = 100,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_POLICIES_READ)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """List approvals for the project, newest first.

    Filter by status: pending, approved, denied, expired.
    """
    valid_statuses = {"pending", "approved", "denied", "expired"}
    if status_filter and status_filter not in valid_statuses:
        raise HTTPException(
            status_code=400,
            detail=f"status must be one of {sorted(valid_statuses)}",
        )
    approvals = await approvals_repo.list_approvals(
        session, ctx.project_id, status=status_filter, limit=limit,
    )
    return {"approvals": [a.to_json() for a in approvals]}


@router.get("/{approval_id}")
async def get_approval(
    approval_id: str,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_POLICIES_READ)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Get a single approval by ID."""
    try:
        aid = UUID(approval_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid approval id")
    approval = await approvals_repo.get_approval(session, ctx.project_id, aid)
    if approval is None:
        raise HTTPException(status_code=404, detail="approval not found")
    return {"approval": approval.to_json()}


@router.post("/{approval_id}/approve")
async def approve_approval(
    approval_id: str,
    request: Request,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_POLICIES_WRITE)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Approve a pending approval. The held tool call will proceed."""
    try:
        aid = UUID(approval_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid approval id")

    approval = await approvals_repo.resolve_approval(
        session, ctx.project_id, aid,
        decision="approved",
        resolved_by=str(ctx.key_id),
    )
    if approval is None:
        raise HTTPException(
            status_code=404,
            detail="approval not found or already resolved",
        )

    await audit_repo.emit(
        session,
        build_audit_context(request, ctx),
        APPROVAL_APPROVE,
        CATEGORY_APPROVAL,
        resource_type="approval",
        resource_id=str(aid),
        after_state=approval.to_json(),
    )
    return {"approval": approval.to_json()}


@router.post("/{approval_id}/deny")
async def deny_approval(
    approval_id: str,
    request: Request,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_POLICIES_WRITE)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Deny a pending approval. The held tool call will be blocked."""
    try:
        aid = UUID(approval_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid approval id")

    approval = await approvals_repo.resolve_approval(
        session, ctx.project_id, aid,
        decision="denied",
        resolved_by=str(ctx.key_id),
    )
    if approval is None:
        raise HTTPException(
            status_code=404,
            detail="approval not found or already resolved",
        )

    await audit_repo.emit(
        session,
        build_audit_context(request, ctx),
        APPROVAL_DENY,
        CATEGORY_APPROVAL,
        resource_type="approval",
        resource_id=str(aid),
        after_state=approval.to_json(),
    )
    return {"approval": approval.to_json()}
