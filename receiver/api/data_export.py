"""General data export.

POST /v1/export produces an on-demand, manual export of a project's own
data (policies, traces, spans, approvals, agents, audit, budgets, and the
compliance evidence package) as either a single JSON document or a ZIP of
per-dataset CSV files.

This is deliberately the *manual download* form of export: the operator
pulls their own data on demand. Automated, scheduled, or SIEM-destination
export (streaming to Splunk/Datadog/S3 on a schedule) is a separate,
integration-grade capability and is not part of this endpoint.

Scope: audit:read (read-only over the project's data).
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

import auth as auth_mod
import repositories.analytics as analytics_repo
import repositories.approvals as approvals_repo
import repositories.audit as audit_repo
import repositories.budgets as budgets_repo
import repositories.policies as policies_repo
import repositories.spans as spans_repo
from database import get_db_session

from ._deps import require_scope

router = APIRouter(prefix="/v1", tags=["export"])

# Datasets this endpoint can export. "compliance" is handled specially
# (it's a generated package, not a row set).
VALID_DATASETS: frozenset[str] = frozenset(
    {
        "policies",
        "traces",
        "spans",
        "approvals",
        "agents",
        "audit",
        "budgets",
        "compliance",
    }
)

VALID_FORMATS: frozenset[str] = frozenset({"json", "csv"})

# time_range token -> number of days. None means "all time".
_RANGE_DAYS: dict[str, Optional[int]] = {
    "24h": 1,
    "7d": 7,
    "30d": 30,
    "90d": 90,
    "1y": 365,
    "all": None,
}

# Per-dataset row cap. Manual export is not a bulk pipeline; cap keeps a
# single request bounded in memory and time. Streaming/scheduled export is
# the path for very large pulls.
_MAX_ROWS_PER_DATASET = 10_000


def _range_bounds(time_range: str) -> tuple[Optional[int], Optional[datetime]]:
    """Return (start_unix_nanos, start_datetime) for a time_range token.

    Some repos filter on unix-nano span time, others on a datetime; we
    return both forms so each dataset can use what it needs. None means
    no lower bound (all time).
    """
    days = _RANGE_DAYS.get(time_range, 7)
    if days is None:
        return None, None
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return int(cutoff.timestamp() * 1_000_000_000), cutoff


def _jsonable(value: Any) -> Any:
    """Coerce a value into something JSON/CSV serializable."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (dict, list, str, int, float, bool)) or value is None:
        return value
    return str(value)


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Normalize a repo result row (Pydantic model, ORM object, or dict)
    into a flat dict of JSON-serializable values."""
    if isinstance(row, dict):
        raw = row
    elif hasattr(row, "model_dump"):
        raw = row.model_dump(mode="python")
    elif hasattr(row, "__dict__"):
        raw = {k: v for k, v in vars(row).items() if not k.startswith("_")}
    else:
        return {"value": _jsonable(row)}
    return {k: _jsonable(v) for k, v in raw.items()}


async def _gather_dataset(
    name: str,
    session: AsyncSession,
    project_id: Any,
    start_nanos: Optional[int],
    start_dt: Optional[datetime],
    request: Request,
    ctx: Any,
    _days_for_agents: int,
) -> list[dict[str, Any]]:
    """Fetch one dataset as a list of plain dicts. Unknown/empty datasets
    return an empty list rather than raising, so a partial selection still
    produces a usable export."""
    cap = _MAX_ROWS_PER_DATASET
    if name == "policies":
        rows = await policies_repo.list_policies(session, project_id)
        return [_row_to_dict(r) for r in rows]
    if name == "traces":
        page = await analytics_repo.list_traces(
            session, project_id, limit=cap, start_after=start_nanos
        )
        return [_row_to_dict(r) for r in page.get("data", page.get("traces", []))]
    if name == "spans":
        page = await spans_repo.list_spans(
            session, project_id, limit=cap, start_after=start_nanos
        )
        data = page.get("data", []) if isinstance(page, dict) else getattr(page, "data", [])
        return [_row_to_dict(r) for r in data]
    if name == "approvals":
        rows = await approvals_repo.list_approvals(session, project_id, limit=cap)
        return [_row_to_dict(r) for r in rows]
    if name == "budgets":
        rows = await budgets_repo.list_budgets(
            session, project_id, include_inactive=True, limit=500
        )
        return [_row_to_dict(r) for r in rows]
    if name == "audit":
        result = await audit_repo.list_events(session, project_id, limit=cap)
        events = getattr(result, "events", None)
        if events is None and isinstance(result, dict):
            events = result.get("events", [])
        return [_row_to_dict(r) for r in (events or [])]
    if name == "agents":
        from api.agent_inventory import list_agents  # local import avoids cycle

        try:
            data = await list_agents(
                request=request, days=_days_for_agents, ctx=ctx, session=session
            )
            rows = data.get("agents", []) if isinstance(data, dict) else []
            return [_row_to_dict(r) for r in rows]
        except Exception:
            return []
    return []


def _csv_bytes(rows: list[dict[str, Any]]) -> bytes:
    """Serialize a list of dicts to CSV bytes. The header is the union of
    all keys (stable order: first-seen). Nested values are JSON-encoded so
    a cell never breaks the row structure."""
    if not rows:
        return b""
    columns: list[str] = []
    seen: set[str] = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                columns.append(k)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        flat = {
            k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
            for k, v in r.items()
        }
        writer.writerow(flat)
    return buf.getvalue().encode("utf-8")


@router.post("/export")
async def export_data(
    request: Request,
    body: dict[str, Any] | None = None,
    ctx: auth_mod.ApiKeyContext = Depends(require_scope(auth_mod.SCOPE_AUDIT_READ)),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    """Export selected datasets for the caller's project.

    Body: ``{"datasets": [...], "time_range": "7d", "format": "json"}``.
    JSON returns one document keyed by dataset. CSV returns a ZIP with one
    ``<dataset>.csv`` per selected dataset (CSV is single-table by nature).
    """
    body = body or {}
    project_id = ctx.project_id

    requested = body.get("datasets") or []
    if not isinstance(requested, list) or not requested:
        return JSONResponse(
            status_code=400,
            content={"detail": "Provide a non-empty 'datasets' list."},
        )
    unknown = [d for d in requested if d not in VALID_DATASETS]
    if unknown:
        return JSONResponse(
            status_code=400,
            content={
                "detail": f"Unknown dataset(s): {', '.join(map(str, unknown))}. "
                f"Valid: {', '.join(sorted(VALID_DATASETS))}."
            },
        )

    fmt = str(body.get("format", "json")).lower()
    if fmt not in VALID_FORMATS:
        return JSONResponse(
            status_code=400,
            content={"detail": f"Unsupported format '{fmt}'. Use 'json' or 'csv'."},
        )

    time_range = str(body.get("time_range", "7d"))
    start_nanos, start_dt = _range_bounds(time_range)

    # Gather every requested dataset.
    datasets: dict[str, list[dict[str, Any]]] = {}
    compliance_pkg: Optional[dict[str, Any]] = None
    for name in requested:
        if name == "compliance":
            from api.compliance_export import export_compliance

            pkg = await export_compliance(
                request=request, body={}, ctx=ctx, session=session
            )
            # export_compliance returns a dict for JSON format.
            compliance_pkg = pkg if isinstance(pkg, dict) else None
            continue
        datasets[name] = await _gather_dataset(
            name, session, project_id, start_nanos, start_dt,
            request, ctx, (_RANGE_DAYS.get(time_range) or 365),
        )

    generated_at = datetime.now(timezone.utc).isoformat()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    if fmt == "json":
        doc: dict[str, Any] = {
            "generated_at": generated_at,
            "project_id": str(project_id),
            "time_range": time_range,
            "datasets": datasets,
        }
        if compliance_pkg is not None:
            doc["compliance"] = compliance_pkg
        return Response(
            content=json.dumps(doc, indent=2, default=str),
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="strathon-export-{stamp}.json"'
            },
        )

    # CSV -> ZIP of per-dataset CSVs (+ compliance as JSON inside the zip,
    # since the evidence package is not tabular).
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, rows in datasets.items():
            zf.writestr(f"{name}.csv", _csv_bytes(rows))
        if compliance_pkg is not None:
            zf.writestr("compliance.json", json.dumps(compliance_pkg, indent=2, default=str))
        zf.writestr(
            "manifest.json",
            json.dumps(
                {
                    "generated_at": generated_at,
                    "project_id": str(project_id),
                    "time_range": time_range,
                    "datasets": list(datasets.keys())
                    + (["compliance"] if compliance_pkg is not None else []),
                    "row_counts": {k: len(v) for k, v in datasets.items()},
                },
                indent=2,
            ),
        )
    zip_buf.seek(0)
    return Response(
        content=zip_buf.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="strathon-export-{stamp}.zip"'
        },
    )
