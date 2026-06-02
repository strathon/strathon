"""Webhook delivery inspection and manual replay endpoints.

REST surface for operator visibility into the webhook delivery
pipeline:

  GET    /v1/webhook_deliveries                list (paginated)
  GET    /v1/webhook_deliveries/{id}           single delivery + payload
  POST   /v1/webhook_deliveries/{id}/replay    re-enqueue a failed delivery

Scopes:
  webhook_deliveries:read   GET endpoints
  webhook_deliveries:write  POST replay

What this endpoint surface answers
==================================

"Did this alert fire?" — list with status=succeeded for the policy.

"Why didn't this alert fire?" — list with status=dlq or =abandoned,
inspect the row's last_response_status and last_error.

"The consumer was down for two days; I want to retry the failed ones
now." — find each failed row's id from the list, POST replay.

The list response is paginated with an opaque cursor and a hard 200-row
cap per page; the standard GitHub-style pattern. The single-get endpoint
returns the full payload so operators can see exactly what was sent.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

import auth as auth_mod
import repositories.audit as audit_repo
import repositories.webhook_deliveries as deliveries_repo
from audit.actions import (
    CATEGORY_WEBHOOK_DELIVERY,
    WEBHOOK_DELIVERY_REPLAY,
)
from database import get_db_session

from ._deps import build_audit_context, coerce_project_id, require_scope


router = APIRouter(prefix="/v1/webhook_deliveries", tags=["webhook_deliveries"])


@router.get("")
async def list_webhook_deliveries(
    request: Request,
    project_id: str | None = None,
    status_filter: str | None = None,
    policy_id: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
    ctx: auth_mod.ApiKeyContext = Depends(  # noqa: ARG001
        require_scope(auth_mod.SCOPE_WEBHOOK_DELIVERIES_READ)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """List webhook deliveries, newest first.

    Query parameters:
      status      Filter by status (pending/succeeded/failed_retrying/dlq/abandoned)
      policy_id   Restrict to one policy
      limit       Page size, default 50, max 200
      cursor      Opaque pagination cursor from a prior response

    NB: the query param is named ``status`` in the OpenAPI surface but
    bound to ``status_filter`` here because FastAPI imports ``status``
    as a sentinel module (used for status.HTTP_200_OK etc.) and
    shadowing it inside the handler would be confusing.
    """
    pid = coerce_project_id(request, project_id, ctx)

    pol_uuid: UUID | None = None
    if policy_id is not None:
        try:
            pol_uuid = UUID(policy_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid policy_id")

    try:
        rows, next_cursor = await deliveries_repo.list_deliveries(
            session, pid,
            status=status_filter,
            policy_id=pol_uuid,
            limit=limit,
            cursor=cursor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {
        "webhook_deliveries": [r.to_summary_json() for r in rows],
        "next_cursor": next_cursor,
    }


@router.get("/{delivery_id}")
async def get_webhook_delivery(
    delivery_id: str,
    request: Request,
    ctx: auth_mod.ApiKeyContext = Depends(  # noqa: ARG001
        require_scope(auth_mod.SCOPE_WEBHOOK_DELIVERIES_READ)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Fetch a single delivery with its full payload.

    404 if the delivery does not exist OR does not belong to this
    project — we intentionally don't differentiate, to avoid leaking
    existence information across projects.
    """
    try:
        did = UUID(delivery_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid delivery_id")

    pid = coerce_project_id(request, None, ctx)
    row_json = await deliveries_repo.get_delivery(session, did, pid)
    if row_json is None:
        raise HTTPException(status_code=404, detail="delivery not found")
    return row_json


@router.post("/{delivery_id}/replay", status_code=status.HTTP_202_ACCEPTED)
async def replay_webhook_delivery(
    delivery_id: str,
    request: Request,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_WEBHOOK_DELIVERIES_WRITE)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Reset a failed delivery and re-enqueue it.

    Only deliveries in ``dlq`` or ``abandoned`` can be replayed. The
    row's attempts counter resets to 0, status goes back to ``pending``,
    last_error and last_response_status are cleared, and a Dramatiq
    message is dispatched after the transaction commits.

    Returns 202 Accepted with the updated row — replay is asynchronous
    by definition (the actual HTTP send happens off-request), so the
    client should not treat the 202 as evidence of consumer success.
    To check whether the replay succeeded, GET the delivery again and
    inspect its status.
    """
    try:
        did = UUID(delivery_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid delivery_id")

    pid = coerce_project_id(request, None, ctx)
    try:
        updated = await deliveries_repo.replay_delivery(session, did, pid)
    except ValueError as exc:
        # Wrong-status: the row exists but can't be replayed.
        raise HTTPException(status_code=409, detail=str(exc))

    if updated is None:
        raise HTTPException(status_code=404, detail="delivery not found")

    # Dispatch the Dramatiq message after the DB transaction commits.
    # We use the same after_commit pattern as enqueue_delivery so a
    # rolled-back replay (rare; would happen if the response handler
    # raised after this point) doesn't produce a phantom send.
    from sqlalchemy import event
    from webhooks.actor import deliver_webhook

    sync_session = session.sync_session
    _already_sent: list[bool] = []
    did_str = str(updated.id)

    def _on_commit(_sync_sess):
        if _already_sent:
            return
        _already_sent.append(True)
        try:
            deliver_webhook.send(did_str)
        except Exception:
            # The sweeper will reclaim the pending row if the dispatch
            # fails here. Logged in the actor module.
            pass

    event.listen(sync_session, "after_commit", _on_commit)
    await audit_repo.emit(
        session,
        build_audit_context(request, ctx),
        WEBHOOK_DELIVERY_REPLAY,
        CATEGORY_WEBHOOK_DELIVERY,
        resource_type="webhook_delivery",
        resource_id=str(updated.id),
    )
    return updated.to_summary_json()


__all__ = ["router"]
