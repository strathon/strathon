"""Session-based tests for repositories/traces.py.

Covers the ingest write path: upsert_trace, upsert_span (including the
ON CONFLICT DO UPDATE branch with JSONB attribute merge), the startup
helpers ensure_default_project and is_dev_key_active.

A note on identity map / session behavior:
    SQLAlchemy's session caches ORM objects in its identity map. After
    an upsert that triggers ON CONFLICT DO UPDATE on the DB side, the
    in-memory ORM object is still the stale version from before the
    conflict. To read the post-conflict row, the test must either
    `session.expire()` the cached object or query via raw `select()`
    after a flush *and* an expire — or simpler: use `session.execute(text(...))`.

    In production ingest this is a non-issue because each request gets
    its own short-lived session that's discarded after the response.
    Tests that need to verify "what got written" after an UPSERT must
    explicitly bypass the identity map.
"""

from __future__ import annotations

import os
import uuid


# ---- upsert_trace -------------------------------------------------------


async def test_upsert_trace_first_call_inserts(session, isolated_project):
    from sqlalchemy import select
    from models import Trace
    from repositories.traces import upsert_trace

    trace_id = os.urandom(16)
    await upsert_trace(
        session,
        trace_id=trace_id,
        project_id=isolated_project,
        start_time_unix_nano=1000,
        agent_name="bot",
    )
    await session.flush()

    row = (
        await session.execute(select(Trace).where(Trace.id == trace_id))
    ).scalar_one()
    assert row.project_id == isolated_project
    assert row.start_time_unix_nano == 1000
    assert row.agent_name == "bot"


async def test_upsert_trace_second_call_is_noop(session, isolated_project):
    """Second upsert with different agent_name should NOT change the row.

    Trace upsert uses ON CONFLICT DO NOTHING — the first writer wins.
    """
    from repositories.traces import upsert_trace

    trace_id = os.urandom(16)
    await upsert_trace(
        session,
        trace_id=trace_id, project_id=isolated_project,
        start_time_unix_nano=1000, agent_name="first",
    )
    await session.flush()

    await upsert_trace(
        session,
        trace_id=trace_id, project_id=isolated_project,
        start_time_unix_nano=2000, agent_name="second",
    )
    await session.flush()

    # Bypass the identity map by using raw text query
    from sqlalchemy import text
    result = await session.execute(
        text("SELECT start_time_unix_nano, agent_name FROM traces WHERE id = :t"),
        {"t": trace_id},
    )
    row = result.first()
    assert row[0] == 1000, "start_time should be the first writer's"
    assert row[1] == "first", "agent_name should be the first writer's"


# ---- upsert_span --------------------------------------------------------


async def test_upsert_span_first_call_inserts(session, isolated_project):
    from sqlalchemy import select
    from models import Trace, Span
    from repositories.traces import upsert_span

    trace_id = os.urandom(16)
    span_id = os.urandom(8)

    # Trace must exist first (FK)
    session.add(Trace(
        id=trace_id, project_id=isolated_project, start_time_unix_nano=1000,
    ))
    await session.flush()

    await upsert_span(
        session,
        trace_id=trace_id, span_id=span_id,
        parent_span_id=None, project_id=isolated_project,
        name="my.span", kind="INTERNAL",
        start_time_unix_nano=2000, end_time_unix_nano=None,
        status_code="UNSET", status_message=None,
        operation_name=None, provider_name=None,
        request_model="gpt-5", response_model=None,
        agent_name="agent1", agent_id=None,
        tool_name=None, workflow_name=None, conversation_id=None,
        input_tokens=100, output_tokens=None,
        attributes={"k": "v"},
    )
    await session.flush()

    row = (
        await session.execute(
            select(Span).where(Span.span_id == span_id)
        )
    ).scalar_one()
    assert row.name == "my.span"
    assert row.kind == "INTERNAL"
    assert row.request_model == "gpt-5"
    assert row.agent_name == "agent1"
    assert row.input_tokens == 100
    assert row.attributes == {"k": "v"}


async def test_upsert_span_on_conflict_updates_end_time_and_merges_attrs(
    session, isolated_project
):
    """The critical streaming-semantics test.

    First insert: span starts. end_time=None, attrs A.
    Second insert: span ends. end_time set, status set, attrs B.
    Result: end_time/status updated, attributes are the JSONB || merge of A and B.
    """
    from sqlalchemy import text
    from models import Trace
    from repositories.traces import upsert_span

    trace_id = os.urandom(16)
    span_id = os.urandom(8)

    session.add(Trace(
        id=trace_id, project_id=isolated_project, start_time_unix_nano=1000,
    ))
    await session.flush()

    await upsert_span(
        session,
        trace_id=trace_id, span_id=span_id,
        parent_span_id=None, project_id=isolated_project,
        name="step1", kind="INTERNAL",
        start_time_unix_nano=2000, end_time_unix_nano=None,
        status_code="UNSET", status_message=None,
        operation_name=None, provider_name=None,
        request_model=None, response_model=None,
        agent_name=None, agent_id=None,
        tool_name=None, workflow_name=None, conversation_id=None,
        input_tokens=None, output_tokens=None,
        attributes={"first_key": "v1", "shared": "old"},
    )
    await session.flush()

    # Second upsert — same trace+span, new end_time, new attrs
    await upsert_span(
        session,
        trace_id=trace_id, span_id=span_id,
        parent_span_id=None, project_id=isolated_project,
        name="step1", kind="INTERNAL",
        start_time_unix_nano=2000, end_time_unix_nano=3000,
        status_code="OK", status_message="all good",
        operation_name=None, provider_name=None,
        request_model=None, response_model=None,
        agent_name=None, agent_id=None,
        tool_name=None, workflow_name=None, conversation_id=None,
        input_tokens=None, output_tokens=None,
        attributes={"second_key": "v2", "shared": "new"},
    )
    await session.flush()

    # Verify via raw SQL so we bypass the SQLAlchemy identity map
    # (which still holds the pre-conflict in-memory Span object).
    result = await session.execute(
        text("""
            SELECT end_time_unix_nano, status_code, status_message, attributes
            FROM spans WHERE span_id = :s
        """),
        {"s": span_id},
    )
    row = result.first()
    assert row[0] == 3000, "end_time should be updated by the second call"
    assert row[1] == "OK", "status_code should be updated"
    assert row[2] == "all good", "status_message should be updated"
    attrs = row[3]
    assert attrs["first_key"] == "v1", "first call's keys must survive"
    assert attrs["second_key"] == "v2", "second call's keys must be added"
    assert attrs["shared"] == "new", "shared key must take EXCLUDED's value (Postgres || semantics)"


# ---- ensure_default_project ---------------------------------------------


async def test_ensure_default_project_creates_when_missing(session):
    from sqlalchemy import select
    from models import Project
    from repositories.traces import ensure_default_project

    slug = f"fresh-{uuid.uuid4().hex[:8]}"
    project_id = await ensure_default_project(session, slug)
    await session.flush()

    row = (
        await session.execute(select(Project).where(Project.slug == slug))
    ).scalar_one()
    assert row.id == project_id
    assert row.name == "Default"


async def test_ensure_default_project_idempotent(session):
    """Calling twice returns the same project_id."""
    from repositories.traces import ensure_default_project

    slug = f"idempotent-{uuid.uuid4().hex[:8]}"
    first = await ensure_default_project(session, slug)
    await session.flush()
    second = await ensure_default_project(session, slug)
    await session.flush()
    assert first == second


async def test_ensure_default_project_creates_settings_row(session):
    """The function also seeds the project_settings row."""
    from sqlalchemy import select
    from models import ProjectSettings
    from repositories.traces import ensure_default_project

    slug = f"settings-{uuid.uuid4().hex[:8]}"
    project_id = await ensure_default_project(session, slug)
    await session.flush()

    settings = (
        await session.execute(
            select(ProjectSettings).where(ProjectSettings.project_id == project_id)
        )
    ).scalar_one()
    assert settings is not None
    assert settings.trace_retention_days == 30  # the schema default


# ---- is_dev_key_active --------------------------------------------------


async def test_is_dev_key_active_returns_true_for_unrevoked(session, isolated_project):
    from repositories.auth import create_api_key
    from repositories.traces import is_dev_key_active

    created = await create_api_key(session, isolated_project, name="dev")
    await session.flush()

    assert await is_dev_key_active(session, created.api_key.id) is True


async def test_is_dev_key_active_returns_false_for_revoked(session, isolated_project):
    from repositories.auth import create_api_key, revoke_api_key
    from repositories.traces import is_dev_key_active

    created = await create_api_key(session, isolated_project, name="dev")
    await session.flush()
    await revoke_api_key(session, created.api_key.id)
    await session.flush()

    assert await is_dev_key_active(session, created.api_key.id) is False


async def test_is_dev_key_active_returns_false_for_unknown(session):
    from repositories.traces import is_dev_key_active

    assert await is_dev_key_active(session, uuid.uuid4()) is False
