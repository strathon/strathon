"""Operator-facing budget management endpoints.

  POST   /v1/budgets             create a budget
  GET    /v1/budgets             list budgets (active by default)
  GET    /v1/budgets/{id}        single budget (with cached spent_usd)
  GET    /v1/budgets/{id}/spend  live aggregated spend (bypasses cache)
  PATCH  /v1/budgets/{id}        update threshold / soft_limit / activate
  DELETE /v1/budgets/{id}        delete

Scopes:
  budgets:read   GET endpoints
  budgets:write  POST + PATCH + DELETE

The /spend endpoint exists separately from GET /{id} because the
aggregation query is more expensive than reading the cached row.
Dashboards that just need "approximately how much have we spent" read
the cached value from GET. Enforcement decisions and operator-driven
"refresh now" reads use /spend.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

import auth as auth_mod
import repositories.budgets as budgets_repo
from database import get_db_session
from models.intervention import Budget

from ._deps import coerce_project_id, require_scope


router = APIRouter(prefix="/v1/budgets", tags=["budgets"])


# ---- Request / response shapes ----------------------------------------


class CreateBudgetRequest(BaseModel):
    """POST /v1/budgets body.

    Exactly one of (max_spend_usd, max_repeated_calls) must be set —
    a budget is either a cost budget or an iteration budget. The
    repository validates and surfaces a 400 with a descriptive
    message on bad combinations.
    """
    name: str = Field(min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=1024)
    scope: str
    scope_value: Optional[str] = None

    # Cost-budget fields
    max_spend_usd: Optional[str] = None  # accept as string to preserve precision
    budget_duration: Optional[str] = None
    soft_limit_ratio: Optional[str] = None

    # Iteration-budget fields
    max_repeated_calls: Optional[int] = None
    loop_window_seconds: Optional[str] = None  # string -> Decimal


class PatchBudgetRequest(BaseModel):
    """PATCH /v1/budgets/{id} body.

    Only fields you want to change. Sending null keeps current value
    (Pydantic distinguishes unset from null; we treat null as "no
    change" for ergonomics). To deactivate a budget, set is_active=False.
    """
    name: Optional[str] = None
    description: Optional[str] = None
    max_spend_usd: Optional[str] = None
    soft_limit_ratio: Optional[str] = None
    max_repeated_calls: Optional[int] = None
    loop_window_seconds: Optional[str] = None
    is_active: Optional[bool] = None


def _str_to_decimal(name: str, raw: Optional[str]) -> Optional[Decimal]:
    if raw is None:
        return None
    try:
        return Decimal(raw)
    except (InvalidOperation, TypeError):
        raise HTTPException(
            status_code=400,
            detail=f"{name} must be a decimal string (got {raw!r})",
        )


# ---- POST /v1/budgets -------------------------------------------------


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_budget(
    body: CreateBudgetRequest,
    request: Request,
    ctx: auth_mod.ApiKeyContext = Depends(  # noqa: ARG001
        require_scope(auth_mod.SCOPE_BUDGETS_WRITE)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    pid = coerce_project_id(request, None)
    try:
        budget = await budgets_repo.create_budget(
            session, pid,
            name=body.name,
            description=body.description,
            scope=body.scope,
            scope_value=body.scope_value,
            max_spend_usd=_str_to_decimal("max_spend_usd", body.max_spend_usd),
            max_repeated_calls=body.max_repeated_calls,
            loop_window_seconds=_str_to_decimal(
                "loop_window_seconds", body.loop_window_seconds,
            ),
            budget_duration=body.budget_duration,
            soft_limit_ratio=_str_to_decimal(
                "soft_limit_ratio", body.soft_limit_ratio,
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"budget": budget.to_json()}


# ---- GET /v1/budgets --------------------------------------------------


@router.get("")
async def list_budgets(
    request: Request,
    include_inactive: bool = False,
    limit: int = 100,
    ctx: auth_mod.ApiKeyContext = Depends(  # noqa: ARG001
        require_scope(auth_mod.SCOPE_BUDGETS_READ)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    pid = coerce_project_id(request, None)
    rows = await budgets_repo.list_budgets(
        session, pid, include_inactive=include_inactive, limit=limit,
    )
    return {"budgets": [r.to_json() for r in rows]}


# ---- GET /v1/budgets/{id} --------------------------------------------


@router.get("/{budget_id}")
async def get_budget(
    budget_id: uuid.UUID,
    request: Request,
    ctx: auth_mod.ApiKeyContext = Depends(  # noqa: ARG001
        require_scope(auth_mod.SCOPE_BUDGETS_READ)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    pid = coerce_project_id(request, None)
    budget = await budgets_repo.get_budget(session, budget_id, pid)
    if budget is None:
        raise HTTPException(status_code=404, detail="budget not found")
    return {"budget": budget.to_json()}


# ---- GET /v1/budgets/{id}/spend --------------------------------------


@router.get("/{budget_id}/spend")
async def get_budget_spend(
    budget_id: uuid.UUID,
    request: Request,
    ctx: auth_mod.ApiKeyContext = Depends(  # noqa: ARG001
        require_scope(auth_mod.SCOPE_BUDGETS_READ)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Live aggregated spend for one budget.

    Runs the SUM(cost_usd) aggregation query, returning the current
    spend in the active window. More expensive than reading the
    cached row from GET /{id}; use this when you need authoritative
    numbers, GET when you can tolerate a few-second staleness.

    For iteration budgets, returns the live count of tool spans in
    the rolling window instead.
    """
    pid = coerce_project_id(request, None)
    budget = await budgets_repo.get_budget(session, budget_id, pid)
    if budget is None:
        raise HTTPException(status_code=404, detail="budget not found")

    if budget.is_cost_budget:
        if budget.budget_reset_at and budget.budget_duration:
            window_start = budgets_repo.window_start_from_reset(
                budget.budget_reset_at, budget.budget_duration,
            )
        else:
            window_start = datetime.now(timezone.utc)
        spent = await budgets_repo.compute_spend_usd(
            session,
            project_id=pid,
            scope=budget.scope,
            scope_value=budget.scope_value,
            window_start=window_start,
        )
        return {
            "budget_id": str(budget_id),
            "kind": "cost",
            "spent_usd": str(spent),
            "max_spend_usd": str(budget.max_spend_usd),
            "window_start": window_start.isoformat(),
            "window_reset_at": (
                budget.budget_reset_at.isoformat() if budget.budget_reset_at else None
            ),
        }

    # Iteration budget
    count = await budgets_repo.compute_iteration_count(
        session,
        project_id=pid,
        scope=budget.scope,
        scope_value=budget.scope_value,
        window_seconds=budget.loop_window_seconds,
    )
    return {
        "budget_id": str(budget_id),
        "kind": "iteration",
        "count": count,
        "max_repeated_calls": budget.max_repeated_calls,
        "window_seconds": str(budget.loop_window_seconds),
    }


# ---- PATCH /v1/budgets/{id} ------------------------------------------


@router.patch("/{budget_id}")
async def patch_budget(
    budget_id: uuid.UUID,
    body: PatchBudgetRequest,
    request: Request,
    ctx: auth_mod.ApiKeyContext = Depends(  # noqa: ARG001
        require_scope(auth_mod.SCOPE_BUDGETS_WRITE)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Partial update of a budget. Only mutable knobs are patchable:
    name, description, max_spend_usd, soft_limit_ratio,
    max_repeated_calls, loop_window_seconds, is_active.

    Scope and budget_duration are NOT patchable: changing scope
    invalidates the existing spend history, and changing duration
    invalidates the existing budget_reset_at. Operators who need
    these create a new budget and delete the old one.
    """
    pid = coerce_project_id(request, None)
    existing = await budgets_repo.get_budget(session, budget_id, pid)
    if existing is None:
        raise HTTPException(status_code=404, detail="budget not found")

    values: dict[str, Any] = {}
    if body.name is not None:
        if not body.name.strip():
            raise HTTPException(status_code=400, detail="name must not be empty")
        values["name"] = body.name.strip()
    if body.description is not None:
        values["description"] = body.description
    if body.max_spend_usd is not None:
        new_max = _str_to_decimal("max_spend_usd", body.max_spend_usd)
        if not existing.is_cost_budget:
            raise HTTPException(
                status_code=400,
                detail="cannot set max_spend_usd on an iteration budget",
            )
        if new_max <= 0:
            raise HTTPException(status_code=400, detail="max_spend_usd must be positive")
        values["max_spend_usd"] = new_max
    if body.soft_limit_ratio is not None:
        values["soft_limit_ratio"] = _str_to_decimal(
            "soft_limit_ratio", body.soft_limit_ratio,
        )
    if body.max_repeated_calls is not None:
        if not existing.is_iteration_budget:
            raise HTTPException(
                status_code=400,
                detail="cannot set max_repeated_calls on a cost budget",
            )
        if body.max_repeated_calls <= 0:
            raise HTTPException(status_code=400, detail="max_repeated_calls must be positive")
        values["max_repeated_calls"] = body.max_repeated_calls
    if body.loop_window_seconds is not None:
        if not existing.is_iteration_budget:
            raise HTTPException(
                status_code=400,
                detail="cannot set loop_window_seconds on a cost budget",
            )
        values["loop_window_seconds"] = _str_to_decimal(
            "loop_window_seconds", body.loop_window_seconds,
        )
    if body.is_active is not None:
        values["is_active"] = body.is_active

    if not values:
        return {"budget": existing.to_json()}

    await session.execute(
        sa_update(Budget).where(Budget.id == budget_id).values(**values)
    )
    await session.flush()
    refreshed = await budgets_repo.get_budget(session, budget_id, pid)
    return {"budget": refreshed.to_json()}


# ---- DELETE /v1/budgets/{id} -----------------------------------------


@router.delete("/{budget_id}")
async def delete_budget(
    budget_id: uuid.UUID,
    request: Request,
    ctx: auth_mod.ApiKeyContext = Depends(  # noqa: ARG001
        require_scope(auth_mod.SCOPE_BUDGETS_WRITE)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    pid = coerce_project_id(request, None)
    deleted = await budgets_repo.delete_budget(session, budget_id, pid)
    if not deleted:
        raise HTTPException(status_code=404, detail="budget not found")
    return {"deleted": True}


__all__ = ["router"]
