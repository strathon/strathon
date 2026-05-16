"""Per-project model price override endpoints.

  POST   /v1/model_prices              upsert an override
  GET    /v1/model_prices              list overrides
  DELETE /v1/model_prices/{model_name} remove an override

Scopes:
  model_prices:read   GET
  model_prices:write  POST + DELETE

POST is idempotent on (project, model_name): the second POST for the
same model_name replaces the first. This matches the operator mental
model — "set the price for gpt-4o to X" should be safe to issue
repeatedly without checking whether it already exists.

We do NOT expose a GET that includes the catalog defaults. Operators
who want to know the effective price for a model query the catalog
directly via the documentation or a future GET /v1/model_prices/effective
endpoint; the override surface is just for, well, overrides.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

import auth as auth_mod
import repositories.model_prices as prices_repo
from database import get_db_session

from ._deps import coerce_project_id, require_scope


router = APIRouter(prefix="/v1/model_prices", tags=["model_prices"])


class UpsertPriceRequest(BaseModel):
    model_name: str = Field(min_length=1, max_length=200)
    # Accept as strings to preserve precision; Pydantic float parsing
    # rounds at ~15 decimal digits and our column stores 12.
    input_cost_per_token: str
    output_cost_per_token: str


def _str_to_decimal(name: str, raw: str) -> Decimal:
    try:
        return Decimal(raw)
    except (InvalidOperation, TypeError):
        raise HTTPException(
            status_code=400,
            detail=f"{name} must be a decimal string (got {raw!r})",
        )


@router.post("", status_code=status.HTTP_200_OK)
async def upsert_price(
    body: UpsertPriceRequest,
    request: Request,
    ctx: auth_mod.ApiKeyContext = Depends(  # noqa: ARG001
        require_scope(auth_mod.SCOPE_MODEL_PRICES_WRITE)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    pid = coerce_project_id(request, None)
    in_cost = _str_to_decimal("input_cost_per_token", body.input_cost_per_token)
    out_cost = _str_to_decimal("output_cost_per_token", body.output_cost_per_token)
    try:
        row = await prices_repo.upsert_override(
            session, pid,
            model_name=body.model_name,
            input_cost_per_token=in_cost,
            output_cost_per_token=out_cost,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"override": row.to_json()}


@router.get("")
async def list_prices(
    request: Request,
    ctx: auth_mod.ApiKeyContext = Depends(  # noqa: ARG001
        require_scope(auth_mod.SCOPE_MODEL_PRICES_READ)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    pid = coerce_project_id(request, None)
    rows = await prices_repo.list_overrides(session, pid)
    return {"overrides": [r.to_json() for r in rows]}


@router.delete("/{model_name}")
async def delete_price(
    model_name: str,
    request: Request,
    ctx: auth_mod.ApiKeyContext = Depends(  # noqa: ARG001
        require_scope(auth_mod.SCOPE_MODEL_PRICES_WRITE)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    pid = coerce_project_id(request, None)
    deleted = await prices_repo.delete_override(session, pid, model_name)
    if not deleted:
        raise HTTPException(status_code=404, detail="override not found")
    return {"deleted": True}


__all__ = ["router"]
