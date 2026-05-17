"""Runtime policy management endpoints.

Five endpoints — list, create, get, update, delete — that power the
intervention layer. SDKs poll `GET /v1/policies` for client-side block
and steer enforcement; humans use the write endpoints to manage rules.

Scope-protected:
  - GET   /v1/policies(/{id})   requires policies:read
  - POST  /v1/policies          requires policies:write
  - PATCH /v1/policies/{id}     requires policies:write
  - DELETE /v1/policies/{id}    requires policies:write

CEL expression and action enum validation happens inside the repository
layer (repositories/policies.py). PolicyExpressionError from the CEL
compiler is translated to 400 here; ValueError (e.g. unknown action)
likewise.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

import auth as auth_mod
import repositories.audit as audit_repo
import repositories.policies as policies_repo
import repositories.project_settings as project_settings_repo
from audit.actions import (
    CATEGORY_POLICY,
    POLICY_CREATE,
    POLICY_DELETE,
    POLICY_UPDATE,
)
from database import get_db_session
from policies import PolicyExpressionError

from ._deps import build_audit_context, require_scope


router = APIRouter(prefix="/v1/policies", tags=["policies"])


@router.get("")
async def list_policies_endpoint(
    ctx: auth_mod.ApiKeyContext = Depends(require_scope(auth_mod.SCOPE_POLICIES_READ)),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """List policies plus the project's intervention default action.

    The default-action field is part of the SDK's enforcement
    contract — it determines whether unmatched calls allow or deny.
    Returning it alongside policies in this single endpoint means
    the SDK refresh path stays one HTTP round-trip; a separate fetch
    would let the two pieces of state drift across the refresh
    window.
    """
    policies = await policies_repo.list_policies(session, ctx.project_id)
    default_action = await project_settings_repo.load_intervention_default_action(
        session, ctx.project_id,
    )
    return {
        "policies": [p.model_dump(mode="json") for p in policies],
        "intervention_default_action": default_action,
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_policy_endpoint(
    payload: dict[str, Any],
    request: Request,
    ctx: auth_mod.ApiKeyContext = Depends(require_scope(auth_mod.SCOPE_POLICIES_WRITE)),
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
    await audit_repo.emit(
        session,
        build_audit_context(request, ctx),
        POLICY_CREATE,
        CATEGORY_POLICY,
        resource_type="policy",
        resource_id=str(policy.id),
        after_state=policy.model_dump(mode="json"),
    )
    return policy.model_dump(mode="json")


@router.get("/{policy_id}")
async def get_policy_endpoint(
    policy_id: str,
    ctx: auth_mod.ApiKeyContext = Depends(require_scope(auth_mod.SCOPE_POLICIES_READ)),
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
    request: Request,
    ctx: auth_mod.ApiKeyContext = Depends(require_scope(auth_mod.SCOPE_POLICIES_WRITE)),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    try:
        pid_uuid = UUID(policy_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid policy_id")
    before = await policies_repo.get_policy(session, ctx.project_id, pid_uuid)
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
    await audit_repo.emit(
        session,
        build_audit_context(request, ctx),
        POLICY_UPDATE,
        CATEGORY_POLICY,
        resource_type="policy",
        resource_id=str(pid_uuid),
        before_state=before.model_dump(mode="json") if before else None,
        after_state=policy.model_dump(mode="json"),
    )
    return policy.model_dump(mode="json")


@router.delete("/{policy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_policy_endpoint(
    policy_id: str,
    request: Request,
    ctx: auth_mod.ApiKeyContext = Depends(require_scope(auth_mod.SCOPE_POLICIES_WRITE)),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    try:
        pid_uuid = UUID(policy_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid policy_id")
    before = await policies_repo.get_policy(session, ctx.project_id, pid_uuid)
    deleted = await policies_repo.delete_policy(session, ctx.project_id, pid_uuid)
    if not deleted:
        raise HTTPException(status_code=404, detail="policy not found")
    await audit_repo.emit(
        session,
        build_audit_context(request, ctx),
        POLICY_DELETE,
        CATEGORY_POLICY,
        resource_type="policy",
        resource_id=str(pid_uuid),
        before_state=before.model_dump(mode="json") if before else None,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
