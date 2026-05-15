"""Runtime policy management endpoints.

Five endpoints — list, create, get, update, delete — that power the
intervention layer. SDKs poll `GET /v1/policies` for client-side block
and steer enforcement; humans use the write endpoints to manage rules.

CEL expression and action enum validation happens inside the repository
layer (repositories/policies.py). PolicyExpressionError from the CEL
compiler is translated to 400 here; ValueError (e.g. unknown action)
likewise.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

import auth as auth_mod
import repositories.policies as policies_repo
from database import get_db_session
from policies import PolicyExpressionError

from ._deps import require_auth


router = APIRouter(prefix="/v1/policies", tags=["policies"])


@router.get("")
async def list_policies_endpoint(
    ctx: auth_mod.ApiKeyContext = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    policies = await policies_repo.list_policies(session, ctx.project_id)
    return {"policies": [p.model_dump(mode="json") for p in policies]}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_policy_endpoint(
    payload: dict[str, Any],
    ctx: auth_mod.ApiKeyContext = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    required = {"name", "match_expression", "action"}
    missing = required - set(payload.keys())
    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"missing required fields: {sorted(missing)}",
        )
    try:
        policy = await policies_repo.create_policy(
            session,
            ctx.project_id,
            name=payload["name"],
            description=payload.get("description"),
            match_expression=payload["match_expression"],
            action=payload["action"],
            action_config=payload.get("action_config"),
            applies_to=payload.get("applies_to"),
            enabled=payload.get("enabled", True),
            priority=payload.get("priority", 0),
        )
    except PolicyExpressionError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid match expression: {exc}",
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    return policy.model_dump(mode="json")


@router.get("/{policy_id}")
async def get_policy_endpoint(
    policy_id: str,
    ctx: auth_mod.ApiKeyContext = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    try:
        pid_uuid = UUID(policy_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid policy_id")
    policy = await policies_repo.get_policy(session, ctx.project_id, pid_uuid)
    if not policy:
        raise HTTPException(status_code=404, detail="policy not found")
    return policy.model_dump(mode="json")


@router.patch("/{policy_id}")
async def update_policy_endpoint(
    policy_id: str,
    payload: dict[str, Any],
    ctx: auth_mod.ApiKeyContext = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    try:
        pid_uuid = UUID(policy_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid policy_id")
    try:
        policy = await policies_repo.update_policy(
            session, ctx.project_id, pid_uuid, **payload
        )
    except PolicyExpressionError as exc:
        raise HTTPException(status_code=400, detail=f"invalid match expression: {exc}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not policy:
        raise HTTPException(status_code=404, detail="policy not found")
    return policy.model_dump(mode="json")


@router.delete("/{policy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_policy_endpoint(
    policy_id: str,
    ctx: auth_mod.ApiKeyContext = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    try:
        pid_uuid = UUID(policy_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid policy_id")
    deleted = await policies_repo.delete_policy(session, ctx.project_id, pid_uuid)
    if not deleted:
        raise HTTPException(status_code=404, detail="policy not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
