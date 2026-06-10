"""Project settings endpoints.

Per-project knobs that don't fit cleanly under any of the other API
surfaces. For v1 the only field exposed here is
``intervention_default_action``, which toggles whether unmatched
tool-boundary calls default to allow (permissive) or block
(allow-list mode). PII redaction settings live on the same DB row but
are not yet exposed via this endpoint; the SDK doesn't need them, and
the dashboard isn't shipped yet.

Scope-protected:
  - GET   /v1/project/settings   requires project_settings:read
  - PATCH /v1/project/settings   requires project_settings:write

The endpoint is project-scoped via the calling API key's resolved
project context (``ctx.project_id``); no caller has the ability to
read or mutate another project's settings.
"""

from __future__ import annotations

from typing import Any

from fastapi import Body, APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

import auth as auth_mod
import repositories.audit as audit_repo
import repositories.project_settings as project_settings_repo
from audit.actions import CATEGORY_PROJECT_SETTINGS, PROJECT_SETTINGS_UPDATE
from database import get_db_session

from ._deps import build_audit_context, require_scope


router = APIRouter(prefix="/v1/project/settings", tags=["project-settings"])


@router.get("")
async def get_project_settings_endpoint(
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_PROJECT_SETTINGS_READ),
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Return the caller's project settings.

    Currently exposes only ``intervention_default_action``. Future
    fields (e.g. surfaced PII redaction config) will extend this
    response shape additively.
    """
    default_action = await project_settings_repo.load_intervention_default_action(
        session, ctx.project_id,
    )
    retention_days = await project_settings_repo.load_trace_retention_days(
        session, ctx.project_id,
    )
    return {
        "intervention_default_action": default_action,
        "trace_retention_days": retention_days,
    }


@router.patch("")
async def update_project_settings_endpoint(
    request: Request,
    payload: dict[str, Any] = Body(default={}),
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_PROJECT_SETTINGS_WRITE),
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Update the caller's project settings.

    For v1, only ``intervention_default_action`` is settable. Unknown
    keys are rejected with 400 rather than silently ignored — silent
    acceptance of e.g. ``intervention_defualt_action`` (typo) would
    leave the operator believing they had switched into allow-list
    mode when they had not.
    """
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="request body must be a JSON object",
        )

    allowed_keys = {"intervention_default_action", "trace_retention_days"}
    unknown = set(payload.keys()) - allowed_keys
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown settings keys: {sorted(unknown)}",
        )

    before_action = await project_settings_repo.load_intervention_default_action(
        session, ctx.project_id,
    )
    before_retention = await project_settings_repo.load_trace_retention_days(
        session, ctx.project_id,
    )

    if "intervention_default_action" in payload:
        try:
            await project_settings_repo.update_intervention_default_action(
                session, ctx.project_id, payload["intervention_default_action"],
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from None

    if "trace_retention_days" in payload:
        try:
            await project_settings_repo.update_trace_retention_days(
                session, ctx.project_id, payload["trace_retention_days"],
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from None

    default_action = await project_settings_repo.load_intervention_default_action(
        session, ctx.project_id,
    )
    retention_days = await project_settings_repo.load_trace_retention_days(
        session, ctx.project_id,
    )
    await audit_repo.emit(
        session,
        build_audit_context(request, ctx),
        PROJECT_SETTINGS_UPDATE,
        CATEGORY_PROJECT_SETTINGS,
        resource_type="project_settings",
        resource_id=str(ctx.project_id),
        before_state={
            "intervention_default_action": before_action,
            "trace_retention_days": before_retention,
        },
        after_state={
            "intervention_default_action": default_action,
            "trace_retention_days": retention_days,
        },
    )
    return {
        "intervention_default_action": default_action,
        "trace_retention_days": retention_days,
    }
