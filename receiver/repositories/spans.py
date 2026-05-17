"""Span search query logic.

Read-only: all writes go through the traces ingest path. This module
provides filtered, paginated access to the ``spans`` table for the
operator-facing search API.

Filtering strategy:

- **Denormalized columns** (agent_name, tool_name, operation_name,
  request_model, response_model, agent_id, kind, status_code,
  intervention_state) — equality checks compiled into parameterized
  SQL. Each uses the partial B-tree indexes from migration 001.

- **Time range** — ``start_after`` / ``start_before`` as nanosecond
  unix timestamps; prune against the composite
  ``idx_spans_project_time`` index.

- **JSONB attributes** — containment queries via ``@>`` which hit
  the GIN ``idx_spans_attributes_gin`` index from migration 011.
  Expressed as a JSON dict; every key-value pair in the dict must
  match. Nested objects are supported.

- **Cursor pagination** — keyset pagination over
  ``(start_time_unix_nano DESC, trace_id, span_id)``. Stable across
  concurrent inserts; never skips or double-returns rows.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("strathon.receiver.repositories.spans")


# Columns the query API can filter on via simple equality.
_FILTERABLE_COLUMNS: frozenset[str] = frozenset({
    "agent_name",
    "agent_id",
    "tool_name",
    "operation_name",
    "request_model",
    "response_model",
    "kind",
    "status_code",
    "intervention_state",
    "workflow_name",
    "conversation_id",
    "provider_name",
})


@dataclass
class SpanListResult:
    """Page of spans plus pagination state."""

    spans: list[dict[str, Any]]
    next_cursor: Optional[str]


async def list_spans(
    session: AsyncSession,
    project_id: UUID,
    *,
    limit: int = 50,
    cursor: Optional[str] = None,
    start_after: Optional[int] = None,
    start_before: Optional[int] = None,
    filters: Optional[dict[str, str]] = None,
    attr_contains: Optional[dict[str, Any]] = None,
) -> SpanListResult:
    """Search spans for a project.

    ``filters`` is a dict of column_name → value for the denormalized
    columns. ``attr_contains`` is a dict passed to the ``@>`` operator
    against the JSONB ``attributes`` column.

    Returns a page of rows (as dicts) plus an opaque cursor for the
    next page, or None if no more.
    """
    limit = max(1, min(limit, 1000))
    params: dict[str, Any] = {"pid": project_id, "limit": limit + 1}
    clauses: list[str] = ["project_id = :pid"]

    # Time range.
    if start_after is not None:
        clauses.append("start_time_unix_nano >= :start_after")
        params["start_after"] = start_after
    if start_before is not None:
        clauses.append("start_time_unix_nano <= :start_before")
        params["start_before"] = start_before

    # Denormalized column filters.
    if filters:
        for col, val in filters.items():
            if col not in _FILTERABLE_COLUMNS:
                raise ValueError(
                    f"unknown filter column {col!r}; "
                    f"valid: {sorted(_FILTERABLE_COLUMNS)}"
                )
            param_name = f"f_{col}"
            clauses.append(f"{col} = :{param_name}")
            params[param_name] = val

    # JSONB attribute containment.
    if attr_contains:
        clauses.append("attributes @> CAST(:attr_json AS jsonb)")
        params["attr_json"] = json.dumps(attr_contains)

    # Cursor (keyset pagination).
    if cursor:
        try:
            cur_time, cur_trace, cur_span = _decode_cursor(cursor)
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError(f"invalid cursor: {exc}") from exc
        clauses.append(
            "(start_time_unix_nano, trace_id, span_id) < "
            "(:cur_time, :cur_trace, :cur_span)"
        )
        params["cur_time"] = cur_time
        params["cur_trace"] = cur_trace
        params["cur_span"] = cur_span

    where = " AND ".join(clauses)
    sql = text(
        f"SELECT * FROM spans "
        f"WHERE {where} "
        f"ORDER BY start_time_unix_nano DESC, trace_id DESC, span_id DESC "
        f"LIMIT :limit"
    )
    result = await session.execute(sql, params)
    rows = [dict(r) for r in result.mappings().all()]

    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor: Optional[str] = None
    if has_more and page:
        last = page[-1]
        next_cursor = _encode_cursor(
            last["start_time_unix_nano"],
            last["trace_id"],
            last["span_id"],
        )
    return SpanListResult(spans=page, next_cursor=next_cursor)


async def get_span(
    session: AsyncSession,
    project_id: UUID,
    trace_id_hex: str,
    span_id_hex: str,
) -> Optional[dict[str, Any]]:
    """Fetch a single span by trace_id + span_id (hex-encoded).

    Returns None if not found or not in the given project.
    """
    try:
        trace_id = bytes.fromhex(trace_id_hex)
        span_id = bytes.fromhex(span_id_hex)
    except ValueError:
        return None

    sql = text(
        "SELECT * FROM spans "
        "WHERE project_id = :pid AND trace_id = :tid AND span_id = :sid "
        "LIMIT 1"
    )
    result = await session.execute(
        sql, {"pid": project_id, "tid": trace_id, "sid": span_id}
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def get_span_events(
    session: AsyncSession,
    trace_id: bytes,
    span_id: bytes,
) -> list[dict[str, Any]]:
    """Fetch span events for a given span."""
    sql = text(
        "SELECT * FROM span_events "
        "WHERE trace_id = :tid AND span_id = :sid "
        "ORDER BY time_unix_nano ASC"
    )
    result = await session.execute(sql, {"tid": trace_id, "sid": span_id})
    return [dict(r) for r in result.mappings().all()]


async def get_span_links(
    session: AsyncSession,
    trace_id: bytes,
    span_id: bytes,
) -> list[dict[str, Any]]:
    """Fetch span links for a given span."""
    sql = text(
        "SELECT * FROM span_links "
        "WHERE trace_id = :tid AND span_id = :sid"
    )
    result = await session.execute(sql, {"tid": trace_id, "sid": span_id})
    return [dict(r) for r in result.mappings().all()]


# --- Cursor helpers -----------------------------------------------------------


def _encode_cursor(
    start_time: int, trace_id: bytes, span_id: bytes
) -> str:
    payload = json.dumps(
        {
            "t": start_time,
            "tr": trace_id.hex(),
            "sp": span_id.hex(),
        },
        separators=(",", ":"),
    )
    return (
        base64.urlsafe_b64encode(payload.encode("utf-8"))
        .decode("ascii")
        .rstrip("=")
    )


def _decode_cursor(cursor: str) -> tuple[int, bytes, bytes]:
    padding = "=" * (-len(cursor) % 4)
    raw = base64.urlsafe_b64decode(cursor + padding)
    obj = json.loads(raw.decode("utf-8"))
    return (
        int(obj["t"]),
        bytes.fromhex(obj["tr"]),
        bytes.fromhex(obj["sp"]),
    )
