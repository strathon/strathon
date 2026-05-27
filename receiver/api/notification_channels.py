"""Notification channel management + Slack interactive actions handler.

  POST   /v1/notification-channels                CRUD
  GET    /v1/notification-channels
  PATCH  /v1/notification-channels/{id}
  DELETE /v1/notification-channels/{id}
  POST   /v1/integrations/slack/actions            Slack interactive buttons
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional
from uuid import UUID

from fastapi import (
    APIRouter, Depends, HTTPException, Request, Response, status,
)
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import auth as auth_mod
from database import get_db_session
from integrations import slack as slack_mod

from ._deps import require_scope

logger = logging.getLogger("strathon.api.notification_channels")

router = APIRouter(tags=["notifications"])

VALID_CHANNEL_TYPES = {"slack", "discord", "github", "webhook"}
VALID_EVENTS = {
    "approval_request", "incident", "policy_blocked", "policy_steered",
    "policy_throttled", "policy_alert", "budget_alert", "budget_halt",
}


# ---- Request models ---------------------------------------------------------

class CreateChannelRequest(BaseModel):
    channel_type: str
    name: str = Field(..., min_length=1, max_length=200)
    config: dict[str, Any] = Field(default_factory=dict)
    events: list[str] = Field(default_factory=list)
    enabled: bool = True

    model_config = {"extra": "forbid"}


class UpdateChannelRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    config: Optional[dict[str, Any]] = None
    events: Optional[list[str]] = None
    enabled: Optional[bool] = None

    model_config = {"extra": "forbid"}


# ---- CRUD -------------------------------------------------------------------

@router.post("/v1/notification-channels", status_code=status.HTTP_201_CREATED)
async def create_channel(
    body: CreateChannelRequest,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_PROJECT_SETTINGS_WRITE)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    if body.channel_type not in VALID_CHANNEL_TYPES:
        raise HTTPException(400, f"channel_type must be one of {sorted(VALID_CHANNEL_TYPES)}")
    invalid_events = set(body.events) - VALID_EVENTS
    if invalid_events:
        raise HTTPException(400, f"invalid events: {sorted(invalid_events)}")

    result = await session.execute(text(
        "INSERT INTO notification_channels "
        "(project_id, channel_type, name, config, events, enabled) "
        "VALUES (:pid, :ct, :name, :config, :events, :enabled) "
        "RETURNING *"
    ), {
        "pid": ctx.project_id,
        "ct": body.channel_type,
        "name": body.name,
        "config": json.dumps(body.config),
        "events": body.events,
        "enabled": body.enabled,
    })
    row = result.mappings().first()
    await session.commit()
    return _serialize(row)


@router.get("/v1/notification-channels")
async def list_channels(
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_PROJECT_SETTINGS_READ)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    result = await session.execute(text(
        "SELECT * FROM notification_channels "
        "WHERE project_id = :pid ORDER BY created_at DESC"
    ), {"pid": ctx.project_id})
    return {"data": [_serialize(r) for r in result.mappings().all()]}


@router.patch("/v1/notification-channels/{channel_id}")
async def update_channel(
    channel_id: str,
    body: UpdateChannelRequest,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_PROJECT_SETTINGS_WRITE)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    try:
        cid = UUID(channel_id)
    except ValueError:
        raise HTTPException(400, "invalid channel_id")

    updates = {}
    if body.name is not None:
        updates["name"] = body.name
    if body.config is not None:
        updates["config"] = json.dumps(body.config)
    if body.events is not None:
        invalid = set(body.events) - VALID_EVENTS
        if invalid:
            raise HTTPException(400, f"invalid events: {sorted(invalid)}")
        updates["events"] = body.events
    if body.enabled is not None:
        updates["enabled"] = body.enabled

    if not updates:
        raise HTTPException(400, "nothing to update")

    set_clause = ", ".join(f"{k} = :{k}" for k in updates)
    updates["cid"] = cid
    updates["pid"] = ctx.project_id

    result = await session.execute(text(
        f"UPDATE notification_channels SET {set_clause}, "
        f"updated_at = NOW() "
        f"WHERE id = :cid AND project_id = :pid "
        f"RETURNING *"
    ), updates)
    row = result.mappings().first()
    if row is None:
        raise HTTPException(404, "channel not found")
    await session.commit()
    return _serialize(row)


@router.delete(
    "/v1/notification-channels/{channel_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_channel(
    channel_id: str,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_PROJECT_SETTINGS_WRITE)
    ),
    session: AsyncSession = Depends(get_db_session),
):
    try:
        cid = UUID(channel_id)
    except ValueError:
        raise HTTPException(400, "invalid channel_id")

    result = await session.execute(text(
        "DELETE FROM notification_channels "
        "WHERE id = :cid AND project_id = :pid"
    ), {"cid": cid, "pid": ctx.project_id})
    if not result.rowcount:
        raise HTTPException(404, "channel not found")
    await session.commit()


# ---- Slack Interactive Actions Handler --------------------------------------

@router.post("/v1/integrations/slack/actions")
async def handle_slack_action(request: Request):
    """Handle Slack interactive button clicks (approve/deny).

    Slack sends a POST with Content-Type: application/x-www-form-urlencoded
    containing a payload JSON string. We verify the signature, parse the
    action, and resolve the approval.
    """
    signing_secret = os.environ.get("STRATHON_SLACK_SIGNING_SECRET")
    if not signing_secret:
        raise HTTPException(500, "Slack signing secret not configured")

    body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    if not slack_mod.verify_slack_signature(signing_secret, timestamp, body, signature):
        raise HTTPException(403, "invalid Slack signature")

    # Parse the Slack payload.
    form_data = await request.form()
    payload = json.loads(form_data.get("payload", "{}"))
    actions = payload.get("actions", [])
    if not actions:
        return Response(status_code=200)

    action = actions[0]
    action_id = action.get("action_id", "")
    value = json.loads(action.get("value", "{}"))
    approval_id = value.get("approval_id")
    base_url = value.get("base_url", "http://localhost:4318")
    response_url = payload.get("response_url")
    user_name = payload.get("user", {}).get("name", "unknown")

    if not approval_id:
        return Response(status_code=200)

    # Resolve the approval via internal API call.
    import httpx
    if action_id == "strathon_approve":
        endpoint = f"{base_url}/v1/approvals/{approval_id}/approve"
        decision = "approved"
    elif action_id == "strathon_deny":
        endpoint = f"{base_url}/v1/approvals/{approval_id}/deny"
        decision = "denied"
    else:
        return Response(status_code=200)

    # Use internal admin key for the approval action.
    admin_key = os.environ.get(
        "STRATHON_INTERNAL_API_KEY",
        "stra_dev_local_default_project_do_not_use_in_production",
    )
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                endpoint,
                headers={"Authorization": f"Bearer {admin_key}"},
                timeout=10.0,
            )
    except Exception:
        logger.exception("Failed to resolve approval via internal API")

    # Update the Slack message.
    if response_url:
        await slack_mod.update_slack_message(
            response_url,
            f":white_check_mark: {decision.title()} by @{user_name}",
        )

    return Response(status_code=200)


# ---- Helpers ----------------------------------------------------------------

def _serialize(row) -> dict[str, Any]:
    if row is None:
        return {}
    d = dict(row)
    for k in ("id", "project_id"):
        if k in d:
            d[k] = str(d[k])
    for k in ("created_at", "updated_at"):
        if k in d and d[k]:
            d[k] = d[k].isoformat()
    return d
