"""Halt persistence operations.

The halt_state table is a write-ahead log — every state change appends a
new row. The "current state" of a halt is the most recent row for a
given scope (trace_id / agent_id / budget_id triple) within a project,
filtered to rows whose state is still active (``paused``, ``halted``)
and not cleared.

Append-only model
=================

We never UPDATE existing rows to change state. Clearing a halt inserts a
new row with state=``cleared`` (and writes cleared_at on the active row
so the active-row query stops returning it). This gives operators an
audit trail of every halt and clear, which is the right shape when the
question "did anyone halt this agent on Tuesday?" matters more than
storage efficiency.

Scopes
======

A halt's "scope" is which agent / trace / budget it applies to. The
DB CHECK constraint requires at least one of trace_id, agent_id, or
budget_id to be non-null. v1 surfaces two operator-facing scopes:

  * ``agent``: scoped to one agent_id; stops every trace from that agent
  * ``project``: scoped to no specific entity; stops every trace in the
                 project (the kill-switch). Modeled in the row as
                 agent_id='*' so the existing CHECK still passes.

The richer trace_id and budget_id scopes are reserved for future server-
side actors such as the budget monitor and loop detector.
For now those scope values aren't reachable through the operator-facing
endpoints; only the resurrected /v1/intervention/sync reads them so the
SDK's eventual budget-driven halts are visible.

Active-halt semantics
=====================

A halt is active iff:
  state IN ('paused', 'halted')
  AND cleared_at IS NULL
  AND (expires_at IS NULL OR expires_at > NOW())

We don't currently have expires_at as a column on halt_state — that's
deferred until programmatic halts that auto-clear after a
window. For v1 every halt is indefinite until an operator clears it.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.intervention import HaltState

logger = logging.getLogger("strathon.receiver.repositories.halts")


# ---- Scopes & states ----------------------------------------------------

# Operator-facing scopes. Maps the user's choice onto the underlying
# halt_state row's nullable columns.
SCOPE_AGENT = "agent"
SCOPE_PROJECT = "project"
VALID_OPERATOR_SCOPES = {SCOPE_AGENT, SCOPE_PROJECT}

# halt_state.state values that are "active" — the SDK and operator
# views both filter to these.
ACTIVE_STATES = ("paused", "halted")

# The single value we stuff into agent_id for a project-scoped halt
# so the CHECK constraint (at least one of trace_id/agent_id/budget_id
# is non-null) still passes. Operators never see this; the API
# translates back to scope="project" on response.
PROJECT_WILDCARD_AGENT_ID = "*"


# ---- DTO ---------------------------------------------------------------


@dataclass(frozen=True)
class HaltRow:
    """Operator-facing view of a halt_state row.

    The DB-level shape is "append-only event log"; this DTO presents
    the synthesized "active halt" view that operators care about.
    """
    id: int
    project_id: uuid.UUID
    scope: str           # "agent" or "project"
    scope_value: Optional[str]   # the agent_id, or None for project-scope
    state: str           # "paused" or "halted"
    reason: str
    actor: str
    set_at: datetime
    cleared_at: Optional[datetime]

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": str(self.project_id),
            "scope": self.scope,
            "scope_value": self.scope_value,
            "state": self.state,
            "reason": self.reason,
            "actor": self.actor,
            "set_at": self.set_at.isoformat(),
            "cleared_at": self.cleared_at.isoformat() if self.cleared_at else None,
        }


def _row_to_dto(row: HaltState) -> HaltRow:
    """Map an ORM HaltState row to the operator-facing HaltRow.

    Reconstructs the scope: a row with agent_id == '*' was created from
    a project-scoped operator request; anything else is agent-scoped.
    """
    if row.agent_id == PROJECT_WILDCARD_AGENT_ID:
        scope = SCOPE_PROJECT
        scope_value = None
    elif row.agent_id is not None:
        scope = SCOPE_AGENT
        scope_value = row.agent_id
    elif row.trace_id is not None:
        # Reserved for future trace-scoped halts. Surface as a generic
        # "trace" scope for now so operators can still see the row.
        scope = "trace"
        scope_value = row.trace_id.hex()
    elif row.budget_id is not None:
        scope = "budget"
        scope_value = str(row.budget_id)
    else:
        # Shouldn't happen — the DB CHECK requires one of the three to
        # be set. Defensive default.
        scope = "unknown"
        scope_value = None

    return HaltRow(
        id=row.id,
        project_id=row.project_id,
        scope=scope,
        scope_value=scope_value,
        state=row.state,
        reason=row.reason,
        actor=row.actor,
        set_at=row.set_at,
        cleared_at=row.cleared_at,
    )


# ---- create_halt -------------------------------------------------------


async def create_halt(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    scope: str,
    scope_value: Optional[str],
    reason: str,
    actor: str = "user",
    state: str = "halted",
) -> HaltRow:
    """Insert a new halt row.

    Raises ValueError if scope/scope_value/state/actor are invalid.

    scope_value must be non-empty for scope="agent" and must be None
    (or empty) for scope="project". The DB doesn't enforce this; the
    repo does, because the agent_id='*' mapping for project scope is
    an internal detail operators shouldn't have to know about.
    """
    if scope not in VALID_OPERATOR_SCOPES:
        raise ValueError(
            f"invalid scope {scope!r}. Valid: {sorted(VALID_OPERATOR_SCOPES)}"
        )
    if state not in ACTIVE_STATES:
        raise ValueError(
            f"invalid state {state!r}. Valid for new halts: {list(ACTIVE_STATES)}"
        )
    if not reason or not reason.strip():
        raise ValueError("reason is required")

    if scope == SCOPE_AGENT:
        if not scope_value:
            raise ValueError("scope=agent requires a non-empty scope_value")
        agent_id = scope_value
    else:  # project
        if scope_value:
            raise ValueError(
                "scope=project must not have a scope_value "
                "(project halts apply to all agents)"
            )
        agent_id = PROJECT_WILDCARD_AGENT_ID

    result = await session.execute(
        insert(HaltState)
        .values(
            project_id=project_id,
            agent_id=agent_id,
            state=state,
            reason=reason,
            actor=actor,
        )
        .returning(HaltState)
    )
    row = result.scalar_one()
    logger.info(
        "Created %s halt for project %s scope=%s value=%s (id=%d)",
        state, project_id, scope, scope_value or "(all)", row.id,
    )
    return _row_to_dto(row)


# ---- list_active_halts (operator view) ---------------------------------


async def list_active_halts(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    include_cleared: bool = False,
    limit: int = 100,
) -> list[HaltRow]:
    """List halts for the project, newest first.

    By default returns only currently-active halts (state in paused/halted
    AND cleared_at IS NULL). include_cleared=True returns the full audit
    trail including previously-cleared halts.

    Hard cap at 200 to keep response sizes bounded; default 100. The
    sync endpoint that the SDK polls uses a higher default since it
    needs the complete active set.
    """
    if limit < 1:
        limit = 1
    if limit > 200:
        limit = 200

    stmt = select(HaltState).where(HaltState.project_id == project_id)
    if not include_cleared:
        stmt = stmt.where(
            HaltState.state.in_(ACTIVE_STATES),
            HaltState.cleared_at.is_(None),
        )
    # Tiebreaker on id since multiple halts inserted in the same
    # transaction share a set_at value (NOW() is statement-stable in
    # a transaction). Without the tiebreaker, ordering across two
    # rows with identical timestamps is undefined.
    stmt = stmt.order_by(
        HaltState.set_at.desc(), HaltState.id.desc(),
    ).limit(limit)

    result = await session.scalars(stmt)
    return [_row_to_dto(r) for r in result.all()]


# ---- get_halt ----------------------------------------------------------


async def get_halt(
    session: AsyncSession,
    halt_id: int,
    project_id: uuid.UUID,
) -> Optional[HaltRow]:
    """Fetch a single halt scoped to the project.

    Returns None if no such halt exists in this project — the API layer
    converts to 404 without leaking cross-project existence info.
    """
    row = await session.scalar(
        select(HaltState).where(
            HaltState.id == halt_id,
            HaltState.project_id == project_id,
        )
    )
    return _row_to_dto(row) if row else None


# ---- clear_halt --------------------------------------------------------


async def clear_halt(
    session: AsyncSession,
    halt_id: int,
    project_id: uuid.UUID,
    *,
    cleared_by_user_id: Optional[uuid.UUID] = None,
) -> Optional[HaltRow]:
    """Mark a halt as cleared.

    Returns the updated row, or None if no such halt exists in this
    project. Raises ValueError if the halt exists but is already
    cleared (the API layer turns this into a 409 with a descriptive
    message).

    Implementation: we UPDATE the existing row's cleared_at column
    rather than insert a new "cleared" event row. The schema supports
    both shapes; the column-update form keeps the active-row query
    simple (one row per halt, not "latest event").
    """
    row = await session.scalar(
        select(HaltState).where(
            HaltState.id == halt_id,
            HaltState.project_id == project_id,
        )
    )
    if row is None:
        return None
    if row.cleared_at is not None:
        raise ValueError(
            f"halt {halt_id} is already cleared (cleared_at={row.cleared_at})"
        )

    # Mark the row cleared. The column is TIMESTAMP WITH TIME ZONE;
    # func.now() resolves to the DB clock so the value is consistent
    # with set_at's default and survives clock-skew between app and DB.
    from sqlalchemy import func as sa_func
    await session.execute(
        update(HaltState)
        .where(HaltState.id == halt_id)
        .values(
            cleared_at=sa_func.now(),
            cleared_by_user_id=cleared_by_user_id,
        )
    )
    await session.flush()
    refreshed = await session.scalar(
        select(HaltState).where(HaltState.id == halt_id)
    )
    assert refreshed is not None, (
        f"halt {halt_id} vanished mid-transaction"
    )
    logger.info(
        "Cleared halt %d for project %s (was state=%s reason=%r)",
        halt_id, project_id, row.state, row.reason,
    )
    return _row_to_dto(refreshed)


# ---- SDK sync: get_active_halts_for_sync ------------------------------


async def get_active_halts_for_sync(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> list[dict[str, Any]]:
    """Compact payload of active halts for the SDK's sync endpoint.

    The SDK is polling on a hot path; we keep the response shape minimal:
    no created_at human formatting, no audit metadata, just the fields
    the SDK needs to decide "halt this agent right now":

        {
          "id": 42,
          "scope": "agent",
          "scope_value": "agent-7",   # or None for project-scope
          "state": "halted",
          "reason": "operator killswitch"
        }

    The SDK uses scope+scope_value to match the current call's agent_id
    against the halt list. A project-scope halt (scope_value=None)
    matches every agent.

    Returns a list, possibly empty. Stable order by set_at DESC.
    """
    stmt = (
        select(HaltState)
        .where(
            HaltState.project_id == project_id,
            HaltState.state.in_(ACTIVE_STATES),
            HaltState.cleared_at.is_(None),
        )
        .order_by(HaltState.set_at.desc(), HaltState.id.desc())
        .limit(500)  # protective cap; thousands-of-halts is pathological
    )
    rows = (await session.scalars(stmt)).all()

    out: list[dict[str, Any]] = []
    for r in rows:
        dto = _row_to_dto(r)
        # Skip non-operator scopes (trace / budget / unknown) from the
        # sync payload until we have SDK code that actually handles them.
        # A future revision will emit them once the SDK handles them.
        if dto.scope not in (SCOPE_AGENT, SCOPE_PROJECT):
            continue
        out.append({
            "id": dto.id,
            "scope": dto.scope,
            "scope_value": dto.scope_value,
            "state": dto.state,
            "reason": dto.reason,
        })
    return out


__all__ = [
    "ACTIVE_STATES",
    "HaltRow",
    "PROJECT_WILDCARD_AGENT_ID",
    "SCOPE_AGENT",
    "SCOPE_PROJECT",
    "VALID_OPERATOR_SCOPES",
    "clear_halt",
    "create_halt",
    "get_active_halts_for_sync",
    "get_halt",
    "list_active_halts",
]
