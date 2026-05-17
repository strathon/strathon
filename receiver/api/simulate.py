"""Policy dry-run / simulation endpoint.

  POST /v1/policies/simulate

Takes a policy definition (match_expression, optional applies_to) and
a time window, evaluates the CEL expression against historical spans,
and returns which spans would have matched.

This lets operators test a policy before enabling it in production.
The answer to "what would this policy have caught in the last 7 days?"
is a list of matching spans plus aggregate counts.

Scope: policies:read (simulation is read-only; it doesn't persist
anything).

Known limitation: simulation runs against stored (redacted) span
attributes. At real-time ingest, policy evaluation happens BEFORE
redaction, so a policy matching raw PII would fire at ingest but
not in simulation. This is documented in the response metadata and
in docs/simulation.md.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import auth as auth_mod
from database import get_db_session
from policies import _span_matches_applies_to
from policies_eval import PolicyExpressionError, evaluate, validate_expression
from schemas.spans import SpanRead, row_to_span_read

from ._deps import require_scope


logger = logging.getLogger("strathon.receiver.api.simulate")


router = APIRouter(prefix="/v1/policies", tags=["policies"])


# Hard limits. Scanning is CPU-bound (CEL eval per span); these
# caps keep a single simulate call from monopolizing the receiver.
MAX_SCAN_LIMIT: int = 10_000
DEFAULT_SCAN_LIMIT: int = 1_000
MAX_RETURNED_MATCHES: int = 100


class SimulateRequest(BaseModel):
    """Body for POST /v1/policies/simulate."""

    match_expression: str = Field(
        ...,
        description="CEL expression to evaluate against each span.",
    )
    applies_to: Optional[list[str]] = Field(
        default=None,
        description=(
            "Span name path filter. Empty or null means all spans. "
            "Same semantics as the applies_to field on a policy."
        ),
    )
    start_after: Optional[str] = Field(
        default=None,
        description=(
            "Scan spans starting at or after this time. "
            "ISO 8601 or nanosecond unix timestamp. "
            "Defaults to 24 hours ago."
        ),
    )
    start_before: Optional[str] = Field(
        default=None,
        description=(
            "Scan spans starting at or before this time. "
            "ISO 8601 or nanosecond unix timestamp. "
            "Defaults to now."
        ),
    )
    scan_limit: int = Field(
        default=DEFAULT_SCAN_LIMIT,
        ge=1,
        le=MAX_SCAN_LIMIT,
        description=(
            "Maximum number of spans to evaluate. Higher values give "
            "more accurate match rates but take longer. Max 10,000."
        ),
    )


class SimulateMatchSummary(BaseModel):
    """Aggregate results from a simulation run."""

    scanned: int = Field(description="Total spans evaluated.")
    matched: int = Field(description="Spans that matched the expression.")
    match_rate: float = Field(
        description="matched / scanned. 0.0 if scanned == 0."
    )
    elapsed_ms: int = Field(description="Wall-clock time for the simulation.")
    truncated: bool = Field(
        description=(
            "True if more matching spans exist than are returned in "
            "the matches array."
        ),
    )
    uses_redacted_data: bool = Field(
        default=True,
        description=(
            "Simulation evaluates against stored (redacted) attributes. "
            "Real-time policy evaluation at ingest sees pre-redaction "
            "data, so match rates may differ for PII-sensitive patterns."
        ),
    )


class SimulateResponse(BaseModel):
    """Full response from POST /v1/policies/simulate."""

    summary: SimulateMatchSummary
    matches: list[SpanRead]


def _parse_timestamp(value: str | None, param_name: str, default_ns: int) -> int:
    """Parse a timestamp, falling back to a nanosecond default."""
    if value is None:
        return default_ns
    try:
        return int(value)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1e9)
    except (ValueError, OverflowError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{param_name} must be nanosecond unix or ISO 8601: {exc}",
        ) from exc


@router.post("/simulate", response_model=SimulateResponse)
async def simulate_policy_endpoint(
    body: SimulateRequest,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_POLICIES_READ)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> SimulateResponse:
    """Dry-run a policy expression against historical spans.

    Returns the spans that would have matched, plus aggregate counts.
    Does not persist anything or modify any state.
    """
    # Validate the CEL expression up front so a typo gets a clear 400
    # rather than "0 matches."
    try:
        validate_expression(body.match_expression)
    except PolicyExpressionError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid match_expression: {exc}",
        ) from exc

    now_ns = int(time.time() * 1e9)
    twenty_four_hours_ns = 24 * 60 * 60 * 1_000_000_000

    start_after_ns = _parse_timestamp(
        body.start_after, "start_after",
        default_ns=now_ns - twenty_four_hours_ns,
    )
    start_before_ns = _parse_timestamp(
        body.start_before, "start_before",
        default_ns=now_ns,
    )

    if start_after_ns > start_before_ns:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="start_after must be <= start_before",
        )

    # Fetch spans in the time window. We fetch scan_limit rows from the
    # DB and evaluate each in-process. The query uses the composite
    # idx_spans_project_time index for efficient time-bounded scans.
    sql = text(
        "SELECT * FROM spans "
        "WHERE project_id = :pid "
        "AND start_time_unix_nano >= :start_after "
        "AND start_time_unix_nano <= :start_before "
        "ORDER BY start_time_unix_nano DESC "
        "LIMIT :scan_limit"
    )
    result = await session.execute(sql, {
        "pid": ctx.project_id,
        "start_after": start_after_ns,
        "start_before": start_before_ns,
        "scan_limit": body.scan_limit,
    })
    rows = [dict(r) for r in result.mappings().all()]

    # Evaluate each span.
    t0 = time.monotonic()
    applies_to = body.applies_to or []
    matches: list[dict[str, Any]] = []
    scanned = 0

    for row in rows:
        scanned += 1
        span_name = row.get("name") or ""

        # Check applies_to filter.
        if not _span_matches_applies_to(span_name, applies_to):
            continue

        # Build the CEL context. The attributes JSONB column
        # contains the full (redacted) attribute dict.
        attrs = row.get("attributes") or {}
        matched = evaluate(body.match_expression, {"name": span_name, "attrs": attrs})
        if matched:
            matches.append(row)

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    truncated = len(matches) > MAX_RETURNED_MATCHES
    returned_matches = matches[:MAX_RETURNED_MATCHES]

    return SimulateResponse(
        summary=SimulateMatchSummary(
            scanned=scanned,
            matched=len(matches),
            match_rate=round(len(matches) / scanned, 6) if scanned > 0 else 0.0,
            elapsed_ms=elapsed_ms,
            truncated=truncated,
        ),
        matches=[row_to_span_read(row) for row in returned_matches],
    )
