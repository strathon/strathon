"""Operator-facing span search endpoints.

  GET /v1/spans                     search with filters + cursor pagination
  GET /v1/spans/{trace_id}/{span_id}  single span with events + links

Scope: traces:read

Filtering:

- Time range via ``start_after`` / ``start_before`` query params
  (nanosecond unix timestamps or ISO 8601 strings).
- Denormalized column equality via query params matching column names
  (``agent_name``, ``tool_name``, ``kind``, etc.).
- JSONB attribute containment via the ``attr.*`` query param prefix:
  ``?attr.gen_ai.tool.name=search`` compiles to
  ``attributes @> '{"gen_ai.tool.name": "search"}'::jsonb`` which
  hits the GIN index from migration 011.

Pagination: keyset cursor over
``(start_time_unix_nano DESC, trace_id, span_id)``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

import auth as auth_mod
import repositories.spans as spans_repo
from database import get_db_session
from schemas.spans import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    SpanDetailRead,
    SpanListResponse,
    row_to_span_read,
)

from ._deps import require_scope


logger = logging.getLogger("strathon.receiver.api.spans")


router = APIRouter(prefix="/v1/spans", tags=["spans"])


def _parse_timestamp(value: str, param_name: str) -> int:
    """Parse a timestamp string to nanosecond unix.

    Accepts either a bare integer (nanoseconds) or an ISO 8601 string.
    """
    # Try integer first.
    try:
        return int(value)
    except ValueError:
        pass
    # Try ISO 8601.
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1e9)
    except (ValueError, OverflowError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"{param_name} must be a nanosecond unix timestamp or "
                f"ISO 8601 string; got {value!r}: {exc}"
            ),
        ) from exc


def _extract_attr_filters(request: Request) -> dict[str, Any] | None:
    """Extract ``attr.*`` query params into a dict for containment.

    ``?attr.gen_ai.tool.name=search&attr.custom.flag=true`` becomes
    ``{"gen_ai.tool.name": "search", "custom.flag": "true"}``.

    Values are always strings (from query params). The GIN containment
    check matches string equality in the JSONB.
    """
    attrs: dict[str, Any] = {}
    for key, value in request.query_params.items():
        if key.startswith("attr."):
            attr_key = key[5:]  # strip "attr." prefix
            if not attr_key:
                continue
            attrs[attr_key] = value
    return attrs if attrs else None


@router.get("", response_model=SpanListResponse)
async def list_spans_endpoint(
    request: Request,
    response: Response,
    start_after: Optional[str] = Query(
        default=None,
        description=(
            "Return spans starting at or after this time. "
            "Nanosecond unix timestamp or ISO 8601."
        ),
    ),
    start_before: Optional[str] = Query(
        default=None,
        description=(
            "Return spans starting at or before this time. "
            "Nanosecond unix timestamp or ISO 8601."
        ),
    ),
    # Denormalized column filters as explicit query params.
    agent_name: Optional[str] = Query(default=None),
    agent_id: Optional[str] = Query(default=None),
    tool_name: Optional[str] = Query(default=None),
    operation_name: Optional[str] = Query(default=None),
    request_model: Optional[str] = Query(default=None),
    response_model: Optional[str] = Query(default=None, alias="response_model"),
    kind: Optional[str] = Query(default=None),
    status_code: Optional[str] = Query(default=None),
    intervention_state: Optional[str] = Query(default=None),
    workflow_name: Optional[str] = Query(default=None),
    conversation_id: Optional[str] = Query(default=None),
    provider_name: Optional[str] = Query(default=None),
    cursor: Optional[str] = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_TRACES_READ)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> SpanListResponse:
    """Search spans for the caller's project."""
    # Collect denormalized column filters.
    filters: dict[str, str] = {}
    for col_name, col_val in [
        ("agent_name", agent_name),
        ("agent_id", agent_id),
        ("tool_name", tool_name),
        ("operation_name", operation_name),
        ("request_model", request_model),
        ("response_model", response_model),
        ("kind", kind),
        ("status_code", status_code),
        ("intervention_state", intervention_state),
        ("workflow_name", workflow_name),
        ("conversation_id", conversation_id),
        ("provider_name", provider_name),
    ]:
        if col_val is not None:
            filters[col_name] = col_val

    # Parse time bounds.
    start_after_ns = (
        _parse_timestamp(start_after, "start_after")
        if start_after is not None
        else None
    )
    start_before_ns = (
        _parse_timestamp(start_before, "start_before")
        if start_before is not None
        else None
    )

    # Extract attr.* prefix filters.
    attr_contains = _extract_attr_filters(request)

    try:
        result = await spans_repo.list_spans(
            session,
            ctx.project_id,
            limit=limit,
            cursor=cursor,
            start_after=start_after_ns,
            start_before=start_before_ns,
            filters=filters or None,
            attr_contains=attr_contains,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    spans = [row_to_span_read(row) for row in result.spans]

    if result.next_cursor:
        response.headers["Link"] = (
            f'<{router.prefix}?cursor={result.next_cursor}>; rel="next"'
        )
    return SpanListResponse(data=spans, next_cursor=result.next_cursor)


@router.get("/{trace_id}/{span_id}", response_model=SpanDetailRead)
async def get_span_endpoint(
    trace_id: str,
    span_id: str,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_TRACES_READ)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> SpanDetailRead:
    """Fetch a single span by trace_id and span_id (hex-encoded).

    Returns the span plus its events and links.
    """
    row = await spans_repo.get_span(
        session, ctx.project_id, trace_id, span_id
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"span {trace_id}/{span_id} not found",
        )

    base = row_to_span_read(row)
    events = await spans_repo.get_span_events(
        session, row["trace_id"], row["span_id"]
    )
    links = await spans_repo.get_span_links(
        session, row["trace_id"], row["span_id"]
    )

    return SpanDetailRead(
        **base.model_dump(),
        events=events,
        links=links,
    )
