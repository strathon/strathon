"""Trace and span persistence for the ingest path.

This module is the ORM replacement for the raw asyncpg INSERT statements
that previously lived inline in main.py's `ingest_traces` endpoint. It
also holds the two startup helpers (`ensure_default_project` and
`is_dev_key_active`) that the lifespan handler needs, so all DB work
exits main.py.

Transaction model:
    Repository functions never commit. Ingest endpoints get one session
    per request via `Depends(get_db_session)`; that dependency's
    on-success commit handles the whole batch atomically. The startup
    helpers run inside `async with async_session_maker()` blocks in
    main.py's lifespan and commit explicitly.

On the ON CONFLICT semantics:
    - Trace upsert: ON CONFLICT DO NOTHING — the first span in a trace
      creates the trace row, subsequent spans in the same trace are
      no-ops at the trace level.
    - Span upsert: ON CONFLICT DO UPDATE end_time + status + attribute
      merge. The `attributes || EXCLUDED.attributes` JSONB concatenation
      preserves earlier attributes while letting later updates overwrite
      colliding keys. This matches the streaming-span semantics where
      a span gets ingested twice (once on start, once on end) and the
      later record completes the earlier one.
"""

from __future__ import annotations

import logging
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from models import ApiKey, Project, ProjectSettings, Span, Trace

logger = logging.getLogger("strathon.receiver.repositories.traces")


# ---- Startup helpers ----------------------------------------------------


async def ensure_default_project(session: AsyncSession, slug: str) -> UUID:
    """Idempotent: get or create the default project, return its id.

    Called once at startup so a fresh deployment has somewhere to send
    traces before any user creates a real project. The conflict target
    is `slug` since name isn't unique.
    """
    stmt = (
        pg_insert(Project)
        .values(name="Default", slug=slug)
        .on_conflict_do_update(
            index_elements=[Project.slug],
            set_={"updated_at": pg_insert(Project).excluded.updated_at},
        )
        .returning(Project.id)
    )
    # The on_conflict_do_update is a no-op in terms of data (we set
    # updated_at to its excluded value, which is the same value), but it
    # ensures RETURNING produces a row even when the project already
    # exists. ON CONFLICT DO NOTHING would skip the RETURNING clause
    # for an existing row.
    result = await session.execute(stmt)
    project_id = result.scalar_one()

    # Also ensure the project_settings row exists. The migration seeds
    # this for the default project, but a redeployment against a fresh
    # DB without the seed would skip it.
    settings_stmt = (
        pg_insert(ProjectSettings)
        .values(project_id=project_id)
        .on_conflict_do_nothing(index_elements=[ProjectSettings.project_id])
    )
    await session.execute(settings_stmt)

    return project_id


async def is_dev_key_active(session: AsyncSession, key_id: UUID) -> bool:
    """Used by the quickstart banner — returns True iff the key exists and is unrevoked."""
    stmt = select(ApiKey.revoked_at).where(ApiKey.id == key_id)
    result = await session.execute(stmt)
    row = result.first()
    if row is None:
        return False
    return row[0] is None


# ---- Ingest hot path ----------------------------------------------------


async def upsert_trace(
    session: AsyncSession,
    trace_id: bytes,
    project_id: UUID,
    start_time_unix_nano: int,
    agent_name: Optional[str],
) -> None:
    """First-time-seen trace row creation. ON CONFLICT DO NOTHING.

    Multiple spans in the same trace will all call this; only the first
    actually inserts. Cheap once the row exists.
    """
    stmt = (
        pg_insert(Trace)
        .values(
            id=trace_id,
            project_id=project_id,
            start_time_unix_nano=start_time_unix_nano,
            agent_name=agent_name,
        )
        .on_conflict_do_nothing(index_elements=[Trace.id])
    )
    await session.execute(stmt)


async def upsert_span(
    session: AsyncSession,
    *,
    trace_id: bytes,
    span_id: bytes,
    parent_span_id: Optional[bytes],
    project_id: UUID,
    name: str,
    kind: str,
    start_time_unix_nano: int,
    end_time_unix_nano: Optional[int],
    status_code: Optional[str],
    status_message: Optional[str],
    operation_name: Optional[str],
    provider_name: Optional[str],
    request_model: Optional[str],
    response_model: Optional[str],
    agent_name: Optional[str],
    agent_id: Optional[str],
    tool_name: Optional[str],
    workflow_name: Optional[str],
    conversation_id: Optional[str],
    input_tokens: Optional[int],
    output_tokens: Optional[int],
    cost_usd: Optional[Any] = None,
    attributes: dict[str, Any],
) -> None:
    """Insert-or-merge a span row.

    Streaming semantics: a span can be ingested twice — first when it
    starts (no end_time, no status), then again when it ends (with both).
    The ON CONFLICT clause completes the earlier row by:
      - filling in end_time / status from the new record
      - merging attributes via JSONB concatenation: `||` keeps prior keys
        and lets new keys overwrite them where they collide

    This matches the production semantics of the previous asyncpg INSERT.
    """
    # Build the INSERT values; we'll pass these through pg_insert so they
    # can be referenced from EXCLUDED inside the on-conflict clause.
    insert_values = {
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": parent_span_id,
        "project_id": project_id,
        "name": name,
        "kind": kind,
        "start_time_unix_nano": start_time_unix_nano,
        "end_time_unix_nano": end_time_unix_nano,
        "status_code": status_code,
        "status_message": status_message,
        "operation_name": operation_name,
        "provider_name": provider_name,
        "request_model": request_model,
        "response_model": response_model,
        "agent_name": agent_name,
        "agent_id": agent_id,
        "tool_name": tool_name,
        "workflow_name": workflow_name,
        "conversation_id": conversation_id,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost_usd,
        "attributes": attributes,
    }

    stmt = pg_insert(Span).values(**insert_values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Span.start_time_unix_nano, Span.trace_id, Span.span_id],
        set_={
            "end_time_unix_nano": stmt.excluded.end_time_unix_nano,
            "status_code": stmt.excluded.status_code,
            "status_message": stmt.excluded.status_message,
            # cost_usd typically arrives with the end-time update (the
            # tokens aren't known until the call completes). Only
            # overwrite if the incoming value is non-null, so a
            # streaming-start record with cost_usd=NULL doesn't blow
            # away a previously-recorded cost.
            "cost_usd": func.coalesce(stmt.excluded.cost_usd, Span.cost_usd),
            # JSONB concat: existing keys win for collisions where EXCLUDED
            # is undefined, but the standard `||` operator has EXCLUDED win.
            # We want EXCLUDED to overwrite (the new ingest is fresher).
            "attributes": Span.attributes.op("||")(stmt.excluded.attributes),
        },
    )
    await session.execute(stmt)
