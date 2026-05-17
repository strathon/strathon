"""Span aggregation and trace tree queries.

Aggregation: group-by analytics over spans (count, cost, tokens)
with time bucketing. Powers operator dashboards and Grafana
integrations.

Trace tree: reconstructs the full parent-child span hierarchy for
a single trace. Powers flamegraph/waterfall visualizations.
"""

from __future__ import annotations

import logging
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("strathon.receiver.repositories.analytics")


# Columns that can be used as group_by dimensions.
VALID_GROUP_BY: frozenset[str] = frozenset({
    "agent_name",
    "tool_name",
    "operation_name",
    "request_model",
    "provider_name",
    "kind",
    "status_code",
    "intervention_state",
})

# Time bucket sizes mapped to Postgres interval expressions.
VALID_BUCKETS: dict[str, str] = {
    "1h": "3600",
    "6h": "21600",
    "1d": "86400",
    "7d": "604800",
    "30d": "2592000",
}


async def aggregate_spans(
    session: AsyncSession,
    project_id: UUID,
    *,
    group_by: str = "request_model",
    time_bucket: Optional[str] = None,
    start_after: Optional[int] = None,
    start_before: Optional[int] = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Aggregate spans by a dimension with optional time bucketing.

    Returns rows with: dimension value, count, total_cost_usd,
    total_input_tokens, total_output_tokens. When time_bucket is
    provided, results are further grouped by bucket.
    """
    if group_by not in VALID_GROUP_BY:
        raise ValueError(
            f"group_by must be one of {sorted(VALID_GROUP_BY)}, "
            f"got {group_by!r}"
        )

    params: dict[str, Any] = {"pid": project_id, "limit": limit}
    where_parts = ["project_id = :pid"]

    if start_after is not None:
        where_parts.append("start_time_unix_nano >= :start_after")
        params["start_after"] = start_after
    if start_before is not None:
        where_parts.append("start_time_unix_nano <= :start_before")
        params["start_before"] = start_before

    where = " AND ".join(where_parts)

    if time_bucket and time_bucket in VALID_BUCKETS:
        bucket_ns = int(VALID_BUCKETS[time_bucket]) * 1_000_000_000
        select_cols = (
            f"{group_by} AS dimension, "
            f"(start_time_unix_nano / {bucket_ns}) * {bucket_ns} AS bucket, "
            "COUNT(*) AS span_count, "
            "COALESCE(SUM(cost_usd), 0) AS total_cost_usd, "
            "COALESCE(SUM(input_tokens), 0) AS total_input_tokens, "
            "COALESCE(SUM(output_tokens), 0) AS total_output_tokens"
        )
        group_clause = f"GROUP BY {group_by}, bucket ORDER BY bucket DESC, span_count DESC"
    else:
        select_cols = (
            f"{group_by} AS dimension, "
            "COUNT(*) AS span_count, "
            "COALESCE(SUM(cost_usd), 0) AS total_cost_usd, "
            "COALESCE(SUM(input_tokens), 0) AS total_input_tokens, "
            "COALESCE(SUM(output_tokens), 0) AS total_output_tokens"
        )
        group_clause = f"GROUP BY {group_by} ORDER BY span_count DESC"

    await session.execute(
        text("SET LOCAL plan_cache_mode = 'force_custom_plan'")
    )
    sql = text(
        f"SELECT {select_cols} FROM spans "
        f"WHERE {where} "
        f"{group_clause} "
        f"LIMIT :limit"
    )
    result = await session.execute(sql, params)
    rows = []
    for r in result.mappings().all():
        row = dict(r)
        # Convert Decimal to string for JSON safety.
        if "total_cost_usd" in row:
            row["total_cost_usd"] = str(row["total_cost_usd"])
        rows.append(row)
    return rows


async def get_trace_tree(
    session: AsyncSession,
    project_id: UUID,
    trace_id_hex: str,
) -> Optional[dict[str, Any]]:
    """Reconstruct the full span tree for a trace.

    Returns the trace metadata plus a nested tree of spans. Each span
    node includes its children, timing, cost, and key attributes.
    Returns None if the trace is not found.
    """
    try:
        trace_id = bytes.fromhex(trace_id_hex)
    except ValueError:
        return None

    # Fetch trace metadata.
    trace_result = await session.execute(
        text(
            "SELECT * FROM traces "
            "WHERE id = :tid AND project_id = :pid "
            "LIMIT 1"
        ),
        {"tid": trace_id, "pid": project_id},
    )
    trace_row = trace_result.mappings().first()
    if trace_row is None:
        return None
    trace = dict(trace_row)

    # Fetch all spans for this trace.
    await session.execute(
        text("SET LOCAL plan_cache_mode = 'force_custom_plan'")
    )
    spans_result = await session.execute(
        text(
            "SELECT * FROM spans "
            "WHERE trace_id = :tid AND project_id = :pid "
            "ORDER BY start_time_unix_nano ASC"
        ),
        {"tid": trace_id, "pid": project_id},
    )
    spans = [dict(r) for r in spans_result.mappings().all()]

    if not spans:
        return {
            "trace": _serialize_trace(trace),
            "root": None,
            "span_count": 0,
        }

    # Build the tree by parent_span_id lookup.
    by_span_id: dict[bytes, dict[str, Any]] = {}
    for span in spans:
        node = _serialize_span(span)
        node["children"] = []
        by_span_id[bytes(span["span_id"])] = node

    roots: list[dict[str, Any]] = []
    for span in spans:
        node = by_span_id[bytes(span["span_id"])]
        parent_id = span.get("parent_span_id")
        if parent_id and bytes(parent_id) in by_span_id:
            by_span_id[bytes(parent_id)]["children"].append(node)
        else:
            roots.append(node)

    return {
        "trace": _serialize_trace(trace),
        "root": roots[0] if len(roots) == 1 else roots,
        "span_count": len(spans),
    }


def _serialize_trace(row: dict[str, Any]) -> dict[str, Any]:
    """Convert a trace row to JSON-safe dict."""
    return {
        "trace_id": bytes(row["id"]).hex(),
        "project_id": str(row["project_id"]),
        "start_time_unix_nano": row.get("start_time_unix_nano"),
        "end_time_unix_nano": row.get("end_time_unix_nano"),
        "agent_name": row.get("agent_name"),
        "workflow_name": row.get("workflow_name"),
        "span_count": row.get("span_count"),
        "total_cost_usd": str(row["total_cost_usd"]) if row.get("total_cost_usd") else None,
        "intervention_state": row.get("intervention_state"),
    }


def _serialize_span(row: dict[str, Any]) -> dict[str, Any]:
    """Convert a span row to a tree node dict."""
    from datetime import datetime, timezone
    start_ns = row.get("start_time_unix_nano")
    end_ns = row.get("end_time_unix_nano")
    duration_ms = None
    if start_ns and end_ns:
        duration_ms = round((end_ns - start_ns) / 1_000_000, 2)

    return {
        "span_id": bytes(row["span_id"]).hex(),
        "parent_span_id": bytes(row["parent_span_id"]).hex() if row.get("parent_span_id") else None,
        "name": row.get("name"),
        "kind": row.get("kind"),
        "start_time": (
            datetime.fromtimestamp(start_ns / 1e9, tz=timezone.utc).isoformat()
            if start_ns else None
        ),
        "end_time": (
            datetime.fromtimestamp(end_ns / 1e9, tz=timezone.utc).isoformat()
            if end_ns else None
        ),
        "duration_ms": duration_ms,
        "status_code": row.get("status_code"),
        "agent_name": row.get("agent_name"),
        "tool_name": row.get("tool_name"),
        "request_model": row.get("request_model"),
        "operation_name": row.get("operation_name"),
        "input_tokens": row.get("input_tokens"),
        "output_tokens": row.get("output_tokens"),
        "cost_usd": str(row["cost_usd"]) if row.get("cost_usd") else None,
        "intervention_state": row.get("intervention_state"),
    }


# ---- Trace list --------------------------------------------------------------


async def list_traces(
    session: AsyncSession,
    project_id: UUID,
    *,
    limit: int = 50,
    cursor: Optional[str] = None,
    start_after: Optional[int] = None,
    start_before: Optional[int] = None,
    agent_name: Optional[str] = None,
    intervention_state: Optional[str] = None,
) -> dict[str, Any]:
    """List traces for a project, newest first.

    Returns a page of traces plus next_cursor for pagination.
    """
    import base64
    import json

    limit = max(1, min(limit, 1000))
    params: dict[str, Any] = {"pid": project_id, "limit": limit + 1}
    clauses = ["project_id = :pid"]

    if start_after is not None:
        clauses.append("start_time_unix_nano >= :start_after")
        params["start_after"] = start_after
    if start_before is not None:
        clauses.append("start_time_unix_nano <= :start_before")
        params["start_before"] = start_before
    if agent_name is not None:
        clauses.append("agent_name = :agent_name")
        params["agent_name"] = agent_name
    if intervention_state is not None:
        clauses.append("intervention_state = :intervention_state")
        params["intervention_state"] = intervention_state

    if cursor:
        try:
            raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            obj = json.loads(raw)
            clauses.append(
                "(start_time_unix_nano, id) < (:cur_time, :cur_id)"
            )
            params["cur_time"] = int(obj["t"])
            params["cur_id"] = bytes.fromhex(obj["id"])
        except Exception as exc:
            raise ValueError(f"invalid cursor: {exc}") from exc

    where = " AND ".join(clauses)
    result = await session.execute(text(
        f"SELECT * FROM traces WHERE {where} "
        f"ORDER BY start_time_unix_nano DESC, id DESC "
        f"LIMIT :limit"
    ), params)
    rows = [dict(r) for r in result.mappings().all()]

    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = None
    if has_more and page:
        last = page[-1]
        payload = json.dumps({
            "t": last["start_time_unix_nano"],
            "id": bytes(last["id"]).hex(),
        }, separators=(",", ":"))
        next_cursor = (
            base64.urlsafe_b64encode(payload.encode())
            .decode().rstrip("=")
        )

    return {
        "data": [_serialize_trace(r) for r in page],
        "next_cursor": next_cursor,
    }
