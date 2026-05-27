"""Policy template endpoints.

  GET  /v1/policy-templates             browse the catalog
  GET  /v1/policy-templates/{id}        get a single template
  POST /v1/policy-templates/{id}/apply  create a policy from the template

Scope: policies:read for browsing, policies:write for applying.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

import auth as auth_mod
import policy_templates as catalog
import repositories.policies as policies_repo
from database import get_db_session

from ._deps import require_scope


router = APIRouter(prefix="/v1/policy-templates", tags=["policy-templates"])


@router.get("")
async def list_templates(
    tag: Optional[str] = Query(
        default=None,
        description="Filter by tag (security, cost, compliance, etc.)",
    ),
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_POLICIES_READ)
    ),
) -> dict[str, Any]:
    """Browse the policy template catalog."""
    return {"data": catalog.list_templates(tag=tag)}


@router.get("/{template_id}")
async def get_template(
    template_id: str,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_POLICIES_READ)
    ),
) -> dict[str, Any]:
    """Get a single template by ID."""
    t = catalog.get_template(template_id)
    if t is None:
        raise HTTPException(status_code=404, detail="template not found")
    return t


@router.post("/{template_id}/apply", status_code=status.HTTP_201_CREATED)
async def apply_template(
    template_id: str,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_POLICIES_WRITE)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Create a policy from a template.

    Creates a real policy using the template's CEL expression, action,
    and action_config. The policy is immediately active.
    """
    t = catalog.TEMPLATES_BY_ID.get(template_id)
    if t is None:
        raise HTTPException(status_code=404, detail="template not found")

    policy = await policies_repo.create_policy(
        session,
        ctx.project_id,
        name=t.name,
        match_expression=t.match_expression,
        action=t.action,
        action_config=t.action_config or None,
        applies_to=t.applies_to or None,
        description=t.description,
    )
    return {
        "template_id": template_id,
        "policy": policy.model_dump(mode="json"),
    }
