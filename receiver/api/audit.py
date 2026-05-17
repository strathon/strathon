"""Operator-facing audit log endpoints.

  GET    /v1/audit/events                list with SCIM filter + cursor
  GET    /v1/audit/events/{event_id}     single event
  GET    /v1/audit/events/{event_id}/verify  hash-chain check
  GET    /v1/audit/anchors               recent integrity anchors
  POST   /v1/audit/export                async NDJSON export (stub for Stage 1)
  GET    /v1/audit/streams               list webhook destinations
  POST   /v1/audit/streams               create webhook destination
  DELETE /v1/audit/streams/{stream_id}   remove webhook destination

Scopes:
  audit:read   GET endpoints + POST /export
  audit:write  POST + DELETE /streams

The audit log of the audit log is closed: every GET of /events or
/anchors emits an ``audit.read`` event itself. This matches the
research §10 anti-pattern #11 ("audit-of-the-audit-log missing")
and the Vault meta-audit pattern.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

import auth as auth_mod
import repositories.audit as audit_repo
from audit.actions import (
    AUDIT_READ,
    AUDIT_STREAM_CREATE,
    AUDIT_STREAM_DELETE,
    CATEGORY_AUDIT,
    CATEGORY_AUDIT_STREAM,
)
from audit.scim_filter import ParseError, compile_to_sql
from database import get_db_session
from schemas.audit import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    AuditAnchorListResponse,
    AuditAnchorRead,
    AuditEventActor,
    AuditEventListResponse,
    AuditEventRead,
    AuditEventResource,
    AuditStreamCreate,
    AuditStreamListResponse,
    AuditStreamRead,
    AuditVerifyResponse,
)

from ._deps import build_audit_context, require_scope


logger = logging.getLogger("strathon.receiver.api.audit")


router = APIRouter(prefix="/v1/audit", tags=["audit"])


# --- Helpers -----------------------------------------------------------------


def _row_to_read(row: dict[str, Any]) -> AuditEventRead:
    """Map a raw DB row mapping to the AuditEventRead Pydantic shape."""
    return AuditEventRead(
        id=row["id"],
        sequence_no=row["sequence_no"],
        occurred_at=row["occurred_at"],
        ingested_at=row["ingested_at"],
        project_id=row["project_id"],
        actor=AuditEventActor(
            type=row["actor_type"],
            id=row["actor_id"],
            display=row.get("actor_display"),
            on_behalf_of=row.get("on_behalf_of"),
        ),
        action=row["action"],
        action_category=row["action_category"],
        outcome=row["outcome"],
        reason=row.get("reason"),
        resource=AuditEventResource(
            type=row["resource_type"],
            id=row["resource_id"],
            parent=row.get("resource_parent"),
        ),
        cascade_root_id=row.get("cascade_root_id"),
        request_id=row["request_id"],
        source_ip=str(row["source_ip"]) if row.get("source_ip") else None,
        user_agent=row.get("user_agent"),
        api_key_id=row.get("api_key_id"),
        auth_method=row.get("auth_method"),
        before_state=row.get("before_state"),
        after_state=row.get("after_state"),
        diff=row.get("diff"),
        pii_classes=list(row.get("pii_classes") or []),
        schema_version=row.get("schema_version", 1),
        prev_hash=bytes(row["prev_hash"]).hex(),
        row_hash=bytes(row["row_hash"]).hex(),
        hmac_key_id=row["hmac_key_id"],
    )


# --- Events ------------------------------------------------------------------


@router.get("/events", response_model=AuditEventListResponse)
async def list_events_endpoint(
    request: Request,
    response: Response,
    filter_expr: Optional[str] = Query(
        default=None,
        alias="filter",
        description=(
            "SCIM 2.0 filter expression. Example: "
            'action_category eq "policy" and outcome eq "deny"'
        ),
    ),
    cursor: Optional[str] = Query(
        default=None,
        description="Opaque cursor from a prior next_cursor value.",
    ),
    limit: int = Query(
        default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT,
        description="Page size, max 1000.",
    ),
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_AUDIT_READ)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> AuditEventListResponse:
    """List audit events. The read itself is audited."""
    where_clause: Optional[str] = None
    where_params: Optional[list[Any]] = None
    if filter_expr:
        try:
            where_clause, where_params = compile_to_sql(filter_expr)
        except ParseError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"invalid filter: {exc}",
            ) from exc

    try:
        result = await audit_repo.list_events(
            session,
            ctx.project_id,
            limit=limit,
            cursor=cursor,
            where_clause=where_clause,
            where_params=where_params,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    events = [_row_to_read(row) for row in result.events]

    # Audit-of-audit: emit a read event for this query. Inside the
    # same transaction so a failed audit-write would roll back our
    # response. limit/filter recorded in after_state for review.
    await audit_repo.emit(
        session,
        build_audit_context(request, ctx),
        AUDIT_READ,
        CATEGORY_AUDIT,
        resource_type="audit_events",
        resource_id="list",
        after_state={
            "filter": filter_expr,
            "limit": limit,
            "returned": len(events),
        },
    )

    if result.next_cursor:
        response.headers["Link"] = (
            f'<{router.prefix}/events?cursor={result.next_cursor}>; rel="next"'
        )
    return AuditEventListResponse(
        data=events,
        next_cursor=result.next_cursor,
    )


@router.get("/events/{event_id}", response_model=AuditEventRead)
async def get_event_endpoint(
    event_id: UUID,
    request: Request,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_AUDIT_READ)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> AuditEventRead:
    row = await audit_repo.get_event(session, ctx.project_id, event_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"audit event {event_id} not found",
        )
    # Audit-of-audit.
    await audit_repo.emit(
        session,
        build_audit_context(request, ctx),
        AUDIT_READ,
        CATEGORY_AUDIT,
        resource_type="audit_event",
        resource_id=str(event_id),
    )
    return _row_to_read(row)


@router.get("/events/{event_id}/verify", response_model=AuditVerifyResponse)
async def verify_event_endpoint(
    event_id: UUID,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_AUDIT_READ)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> AuditVerifyResponse:
    """Verify that an event's stored row_hash matches a recomputed HMAC."""
    verdict = await audit_repo.verify_event(session, ctx.project_id, event_id)
    return AuditVerifyResponse(**verdict)


# --- Anchors -----------------------------------------------------------------


@router.get("/anchors", response_model=AuditAnchorListResponse)
async def list_anchors_endpoint(
    since: Optional[datetime] = Query(
        default=None,
        description="ISO 8601. Return anchors at or after this time.",
    ),
    limit: int = Query(default=100, ge=1, le=MAX_LIMIT),
    ctx: auth_mod.ApiKeyContext = Depends(  # noqa: ARG001
        require_scope(auth_mod.SCOPE_AUDIT_READ)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> AuditAnchorListResponse:
    rows = await audit_repo.list_anchors(session, since=since, limit=limit)
    return AuditAnchorListResponse(
        data=[
            AuditAnchorRead(
                anchor_at=r["anchor_at"],
                last_sequence=r["last_sequence"],
                last_row_hash=bytes(r["last_row_hash"]).hex(),
                merkle_root=bytes(r["merkle_root"]).hex(),
                event_count=r["event_count"],
                signature=(
                    bytes(r["signature"]).hex() if r.get("signature") else None
                ),
                signing_key_id=r.get("signing_key_id"),
            )
            for r in rows
        ]
    )


# --- Export ------------------------------------------------------------------


@router.post("/export", status_code=status.HTTP_202_ACCEPTED)
async def export_events_endpoint(
    ctx: auth_mod.ApiKeyContext = Depends(  # noqa: ARG001
        require_scope(auth_mod.SCOPE_AUDIT_READ)
    ),
) -> dict[str, Any]:
    """Stage 1 stub for async NDJSON export.

    The real implementation lands the export as a dramatiq task and
    returns a signed download URL. Stage 1 returns a clear "not yet"
    response so SDK clients can detect the endpoint exists and is
    scope-protected. Stage 2 fills in the body.
    """
    return {
        "status": "not_implemented",
        "detail": (
            "Audit export will land in Stage 2. Use GET /v1/audit/events "
            "with cursor pagination for now."
        ),
    }


# --- Streams -----------------------------------------------------------------


@router.get("/streams", response_model=AuditStreamListResponse)
async def list_streams_endpoint(
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_AUDIT_READ)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> AuditStreamListResponse:
    streams = await audit_repo.list_streams(session, ctx.project_id)
    return AuditStreamListResponse(
        data=[AuditStreamRead.model_validate(s) for s in streams]
    )


@router.post(
    "/streams",
    status_code=status.HTTP_201_CREATED,
    response_model=AuditStreamRead,
)
async def create_stream_endpoint(
    payload: AuditStreamCreate,
    request: Request,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_AUDIT_WRITE)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> AuditStreamRead:
    stream = await audit_repo.create_stream(
        session,
        ctx.project_id,
        name=payload.name,
        url=payload.url,
        signing_key_id=payload.signing_key_id,
        categories=payload.categories,
    )
    await audit_repo.emit(
        session,
        build_audit_context(request, ctx),
        AUDIT_STREAM_CREATE,
        CATEGORY_AUDIT_STREAM,
        resource_type="audit_stream",
        resource_id=str(stream.id),
        after_state={
            "name": payload.name,
            "url": payload.url,
            "categories": payload.categories,
        },
    )
    return AuditStreamRead.model_validate(stream)


@router.delete("/streams/{stream_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_stream_endpoint(
    stream_id: UUID,
    request: Request,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_AUDIT_WRITE)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    deleted = await audit_repo.delete_stream(
        session, ctx.project_id, stream_id
    )
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"stream {stream_id} not found",
        )
    await audit_repo.emit(
        session,
        build_audit_context(request, ctx),
        AUDIT_STREAM_DELETE,
        CATEGORY_AUDIT_STREAM,
        resource_type="audit_stream",
        resource_id=str(stream_id),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
