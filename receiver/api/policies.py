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
from pydantic import BaseModel, Field

from schemas.responses import BatchResponse, PolicyVersionListResponse
from sqlalchemy.ext.asyncio import AsyncSession

import auth as auth_mod
import repositories.audit as audit_repo
import repositories.policies as policies_repo
import repositories.project_settings as project_settings_repo
from audit.actions import (
    CATEGORY_POLICY,
    POLICY_CREATE,
    POLICY_DELETE,
    POLICY_EXPORT,
    POLICY_IMPORT,
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
            shadow=payload.get("shadow", False),
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


# ---- Export / Import (staging -> prod promotion) -------------------------


class PolicyExportItem(BaseModel):
    """Single policy in the portable export format.

    Excludes id, project_id, and timestamps — those are assigned fresh
    on import into the target project.
    """

    name: str = Field(min_length=1)
    description: str | None = None
    match_expression: str
    action: str
    action_config: dict[str, Any] = Field(default_factory=dict)
    applies_to: list[str] = Field(default_factory=list)
    enabled: bool = True
    priority: int = 0
    shadow: bool = False


class PolicyImportResult(BaseModel):
    created: int = 0
    skipped: int = 0
    errors: list[dict[str, str]] = Field(default_factory=list)


@router.get("/export")
async def export_policies_endpoint(
    request: Request,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_POLICIES_READ)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Export all policies as portable JSON for staging -> prod promotion.

    Returns only the portable fields (no id, project_id, timestamps).
    The output can be POSTed to /v1/policies/import on another instance.
    """
    policies = await policies_repo.list_policies(session, ctx.project_id)

    items = [
        PolicyExportItem(
            name=p.name,
            description=p.description,
            match_expression=p.match_expression,
            action=p.action,
            action_config=p.action_config,
            applies_to=p.applies_to,
            enabled=p.enabled,
            priority=p.priority,
            shadow=p.shadow,
        ).model_dump()
        for p in policies
    ]

    await audit_repo.emit(
        session,
        build_audit_context(request, ctx),
        POLICY_EXPORT,
        CATEGORY_POLICY,
        resource_type="policy_export",
        resource_id="all",
        after_state={"count": len(items)},
    )

    return {"policies": items, "count": len(items)}


@router.post("/import", response_model=PolicyImportResult)
async def import_policies_endpoint(
    body: dict[str, Any],
    request: Request,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_POLICIES_WRITE)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Bulk-create policies from a portable JSON export.

    Accepts the output of GET /v1/policies/export. Skips policies
    whose name + match_expression already exist in the target project
    (idempotent re-import). Validates each policy individually;
    invalid ones are reported in the errors list without blocking
    the rest.
    """
    policies_data = body.get("policies")
    if not isinstance(policies_data, list):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="request body must contain a 'policies' array",
        )

    # Load existing policy names+expressions for duplicate detection.
    existing = await policies_repo.list_policies(session, ctx.project_id)
    existing_signatures = {
        (p.name, p.match_expression) for p in existing
    }

    created = 0
    skipped = 0
    errors: list[dict[str, str]] = []

    for i, item in enumerate(policies_data):
        try:
            parsed = PolicyExportItem.model_validate(item)
        except Exception as e:
            errors.append({"index": str(i), "error": f"validation: {e}"})
            continue

        sig = (parsed.name, parsed.match_expression)
        if sig in existing_signatures:
            skipped += 1
            continue

        try:
            await policies_repo.create_policy(
                session,
                ctx.project_id,
                name=parsed.name,
                match_expression=parsed.match_expression,
                action=parsed.action,
                description=parsed.description,
                action_config=parsed.action_config,
                applies_to=parsed.applies_to,
                enabled=parsed.enabled,
                priority=parsed.priority,
                shadow=parsed.shadow,
            )
            existing_signatures.add(sig)
            created += 1
        except (ValueError, PolicyExpressionError) as e:
            errors.append({"index": str(i), "name": parsed.name, "error": str(e)})

    await audit_repo.emit(
        session,
        build_audit_context(request, ctx),
        POLICY_IMPORT,
        CATEGORY_POLICY,
        resource_type="policy_import",
        resource_id="bulk",
        after_state={
            "created": created,
            "skipped": skipped,
            "errors": len(errors),
            "total": len(policies_data),
        },
    )

    return {"created": created, "skipped": skipped, "errors": errors}


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


# ---- Version history endpoints -----------------------------------------------


@router.get("/{policy_id}/versions", response_model=PolicyVersionListResponse)
async def list_policy_versions(
    policy_id: str,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_POLICIES_READ)
    ),
    session: AsyncSession = Depends(get_db_session),
):
    """List the version history of a policy, newest first."""
    try:
        pid_uuid = UUID(policy_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid policy_id")
    versions = await policies_repo.list_versions(
        session, ctx.project_id, pid_uuid
    )
    # Serialize UUIDs and datetimes for JSON.
    for v in versions:
        v["policy_id"] = str(v["policy_id"])
        v["project_id"] = str(v["project_id"])
        if v.get("changed_at"):
            v["changed_at"] = v["changed_at"].isoformat()
    return {"data": versions}


@router.get("/{policy_id}/versions/{version}")
async def get_policy_version(
    policy_id: str,
    version: int,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_POLICIES_READ)
    ),
    session: AsyncSession = Depends(get_db_session),
):
    """Get a specific version snapshot of a policy."""
    try:
        pid_uuid = UUID(policy_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid policy_id")
    v = await policies_repo.get_version(
        session, ctx.project_id, pid_uuid, version
    )
    if v is None:
        raise HTTPException(status_code=404, detail="version not found")
    v["policy_id"] = str(v["policy_id"])
    v["project_id"] = str(v["project_id"])
    if v.get("changed_at"):
        v["changed_at"] = v["changed_at"].isoformat()
    return v


# ---- Shadow stats ------------------------------------------------------------


@router.get("/{policy_id}/shadow-stats")
async def get_shadow_stats(
    policy_id: str,
    ctx: auth_mod.ApiKeyContext = Depends(require_scope(auth_mod.SCOPE_POLICIES_READ)),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Return match statistics for a shadow policy.

    Useful for evaluating a shadow policy's match rate before promoting
    it to enforcement. Returns match_count, last_matched_at, and the
    shadow flag.
    """
    try:
        pid_uuid = UUID(policy_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid policy id")
    policy = await policies_repo.get_policy(session, ctx.project_id, pid_uuid)
    if policy is None:
        raise HTTPException(status_code=404, detail="policy not found")
    return {
        "policy_id": str(policy.id),
        "name": policy.name,
        "shadow": policy.shadow,
        "match_count": policy.match_count,
        "last_matched_at": (
            policy.last_matched_at.isoformat()
            if policy.last_matched_at else None
        ),
        "enabled": policy.enabled,
        "action": policy.action,
    }


# ---- Batch operations --------------------------------------------------------

MAX_BATCH_SIZE = 100


class BatchRequest(BaseModel):
    action: str = Field(
        ...,
        description="Operation: enable, disable, or delete",
    )
    policy_ids: list[str] = Field(
        ...,
        min_length=1,
        max_length=MAX_BATCH_SIZE,
    )


@router.post("/batch", response_model=BatchResponse)
async def batch_policies(
    body: BatchRequest,
    request: Request,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_POLICIES_WRITE)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Apply a bulk operation to multiple policies atomically.

    Supported actions: enable, disable, delete. All changes happen in
    a single transaction — if any policy_id is invalid the entire
    batch is rejected.

    Research: adidas API guidelines (atomic bulk), CyberArk bulk API
    patterns, OneUptime bulk design. Atomic over partial: cleaner
    error handling, no ambiguous partial-commit states.
    """
    if body.action not in ("enable", "disable", "delete"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"action must be enable, disable, or delete; got {body.action!r}",
        )

    uuids = []
    for pid in body.policy_ids:
        try:
            uuids.append(UUID(pid))
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"invalid policy_id: {pid!r}",
            )

    affected = 0
    for pid in uuids:
        if body.action == "delete":
            deleted = await policies_repo.delete_policy(
                session, ctx.project_id, pid
            )
            if deleted:
                affected += 1
        else:
            new_enabled = body.action == "enable"
            result = await policies_repo.update_policy(
                session, ctx.project_id, pid, enabled=new_enabled
            )
            if result is not None:
                affected += 1

    # Emit a single audit event for the batch.
    await audit_repo.emit(
        session,
        build_audit_context(request, ctx),
        f"policy.batch_{body.action}",
        CATEGORY_POLICY,
        resource_type="policy_batch",
        resource_id=",".join(str(u) for u in uuids),
        after_state={"action": body.action, "affected": affected, "total": len(uuids)},
    )

    return {
        "action": body.action,
        "affected": affected,
        "total": len(uuids),
    }
