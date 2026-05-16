"""Operator-facing halt management endpoints.

  POST    /v1/halts                 create a halt
  GET     /v1/halts                 list active halts (or all with ?include_cleared)
  GET     /v1/halts/{id}            single halt
  DELETE  /v1/halts/{id}            clear a halt

Plus the resurrected sync endpoint in api/intervention.py which reads
the same active-halts list and returns it to the SDK.

Scopes:
  halts:read   GET endpoints + sync endpoint
  halts:write  POST + DELETE (the actions with side effects)
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

import auth as auth_mod
import repositories.halts as halts_repo
from database import get_db_session

from ._deps import coerce_project_id, require_scope


router = APIRouter(prefix="/v1/halts", tags=["halts"])


class CreateHaltRequest(BaseModel):
    """Body for POST /v1/halts.

    scope: ``agent`` (requires scope_value=agent_id) or ``project``
           (scope_value must be omitted; halt applies to all agents).
    state: ``halted`` (default) or ``paused``. Both are "active" — the
           SDK treats them identically as "stop." The distinction is
           semantic: ``paused`` for "operator wants to inspect, may
           resume," ``halted`` for "this should not run again."
    """
    scope: str
    scope_value: str | None = Field(default=None)
    reason: str = Field(min_length=1, max_length=1024)
    state: str = Field(default="halted")


class CreateHaltResponse(BaseModel):
    halt: dict[str, Any]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_halt(
    body: CreateHaltRequest,
    request: Request,
    project_id: str | None = None,
    ctx: auth_mod.ApiKeyContext = Depends(  # noqa: ARG001
        require_scope(auth_mod.SCOPE_HALTS_WRITE)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Create a halt for the project.

    The actor is set to ``user`` because the request came in via the
    REST surface — the only HTTP-reachable actor in the schema's CHECK
    constraint. Programmatic halts (from the budget monitor that lands
    in a future commit) use the in-process repository directly with
    actor=``budget_monitor`` and don't pass through this endpoint.
    """
    pid = coerce_project_id(request, project_id)
    try:
        halt = await halts_repo.create_halt(
            session, pid,
            scope=body.scope,
            scope_value=body.scope_value,
            reason=body.reason,
            actor="user",
            state=body.state,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"halt": halt.to_json()}


@router.get("")
async def list_halts(
    request: Request,
    include_cleared: bool = False,
    limit: int = 100,
    ctx: auth_mod.ApiKeyContext = Depends(  # noqa: ARG001
        require_scope(auth_mod.SCOPE_HALTS_READ)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """List halts for the project, newest first.

    Default returns only active halts (state in paused/halted AND
    not cleared). include_cleared=true returns the full audit trail.
    """
    pid = coerce_project_id(request, None)
    halts = await halts_repo.list_active_halts(
        session, pid,
        include_cleared=include_cleared,
        limit=limit,
    )
    return {"halts": [h.to_json() for h in halts]}


@router.get("/{halt_id}")
async def get_halt(
    halt_id: int,
    request: Request,
    ctx: auth_mod.ApiKeyContext = Depends(  # noqa: ARG001
        require_scope(auth_mod.SCOPE_HALTS_READ)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Fetch a single halt by its id.

    404 if not found OR not in this project (we don't differentiate so
    we don't leak cross-project existence).
    """
    pid = coerce_project_id(request, None)
    halt = await halts_repo.get_halt(session, halt_id, pid)
    if halt is None:
        raise HTTPException(status_code=404, detail="halt not found")
    return {"halt": halt.to_json()}


@router.delete("/{halt_id}")
async def delete_halt(
    halt_id: int,
    request: Request,
    ctx: auth_mod.ApiKeyContext = Depends(  # noqa: ARG001
        require_scope(auth_mod.SCOPE_HALTS_WRITE)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Clear a halt. The active row gets cleared_at set; the audit
    row is preserved in halt_state so the history is intact.

    Returns the updated row (now with cleared_at populated).
    """
    pid = coerce_project_id(request, None)
    try:
        halt = await halts_repo.clear_halt(session, halt_id, pid)
    except ValueError as exc:
        # Already-cleared case
        raise HTTPException(status_code=409, detail=str(exc))

    if halt is None:
        raise HTTPException(status_code=404, detail="halt not found")
    return {"halt": halt.to_json()}


__all__ = ["router"]
