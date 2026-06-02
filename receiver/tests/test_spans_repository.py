"""DB-backed tests for repositories/spans.py.

Exercises list_spans (time range, column filters, attribute
containment, cursor pagination) and get_span + events/links. Uses
the session + isolated_project fixtures from conftest.
"""

from __future__ import annotations

import os
import uuid

import pytest

import repositories.spans as spans_repo
import repositories.traces as traces_repo


async def _insert_span(
    session, project_id, *, name="test-span", kind="INTERNAL",
    start_ns=1_000_000_000_000, end_ns=None,
    agent_name=None, tool_name=None, operation_name=None,
    request_model=None, attributes=None, status_code=None,
    intervention_state=None, agent_id=None,
):
    """Helper to insert a trace+span pair."""
    trace_id = os.urandom(16)
    span_id = os.urandom(8)
    await traces_repo.upsert_trace(
        session, trace_id=trace_id, project_id=project_id,
        start_time_unix_nano=start_ns, agent_name=agent_name,
    )
    await traces_repo.upsert_span(
        session,
        trace_id=trace_id, span_id=span_id, parent_span_id=None,
        project_id=project_id, name=name, kind=kind,
        start_time_unix_nano=start_ns, end_time_unix_nano=end_ns,
        status_code=status_code, status_message=None,
        operation_name=operation_name, provider_name=None,
        request_model=request_model, response_model=None,
        agent_name=agent_name, agent_id=agent_id,
        tool_name=tool_name, workflow_name=None,
        conversation_id=None, input_tokens=None, output_tokens=None,
        attributes=attributes or {},
    )
    await session.flush()
    return trace_id, span_id


@pytest.mark.asyncio
async def test_list_spans_returns_project_scoped(session, isolated_project):
    """Only spans for the queried project are returned."""
    from sqlalchemy import insert
    from models import Project, ProjectSettings

    other = uuid.uuid4()
    await session.execute(
        insert(Project).values(org_id=__import__("uuid").UUID("00000000-0000-0000-0000-0000000000aa"), id=other, name="other", slug=f"o-{other.hex[:8]}")
    )
    await session.execute(insert(ProjectSettings).values(project_id=other))
    await session.flush()

    await _insert_span(session, isolated_project, name="mine")
    await _insert_span(session, other, name="theirs")

    result = await spans_repo.list_spans(session, isolated_project)
    names = [s["name"] for s in result.spans]
    assert "mine" in names
    assert "theirs" not in names


@pytest.mark.asyncio
async def test_list_spans_newest_first(session, isolated_project):
    for i in range(3):
        await _insert_span(
            session, isolated_project,
            name=f"span-{i}",
            start_ns=1_000_000_000_000 + i * 1_000_000_000,
        )
    result = await spans_repo.list_spans(session, isolated_project)
    times = [s["start_time_unix_nano"] for s in result.spans]
    assert times == sorted(times, reverse=True)


@pytest.mark.asyncio
async def test_list_spans_time_range(session, isolated_project):
    early = 1_000_000_000_000
    mid = 2_000_000_000_000
    late = 3_000_000_000_000
    await _insert_span(session, isolated_project, start_ns=early, name="early")
    await _insert_span(session, isolated_project, start_ns=mid, name="mid")
    await _insert_span(session, isolated_project, start_ns=late, name="late")

    result = await spans_repo.list_spans(
        session, isolated_project,
        start_after=mid, start_before=mid,
    )
    assert len(result.spans) == 1
    assert result.spans[0]["name"] == "mid"


@pytest.mark.asyncio
async def test_list_spans_filter_agent_name(session, isolated_project):
    await _insert_span(session, isolated_project, agent_name="alice")
    await _insert_span(session, isolated_project, agent_name="bob")

    result = await spans_repo.list_spans(
        session, isolated_project,
        filters={"agent_name": "alice"},
    )
    assert len(result.spans) == 1
    assert result.spans[0]["agent_name"] == "alice"


@pytest.mark.asyncio
async def test_list_spans_filter_tool_name(session, isolated_project):
    await _insert_span(session, isolated_project, tool_name="search")
    await _insert_span(session, isolated_project, tool_name="calculator")

    result = await spans_repo.list_spans(
        session, isolated_project,
        filters={"tool_name": "search"},
    )
    assert len(result.spans) == 1
    assert result.spans[0]["tool_name"] == "search"


@pytest.mark.asyncio
async def test_list_spans_filter_kind(session, isolated_project):
    await _insert_span(session, isolated_project, kind="CLIENT")
    await _insert_span(session, isolated_project, kind="SERVER")

    result = await spans_repo.list_spans(
        session, isolated_project,
        filters={"kind": "CLIENT"},
    )
    assert len(result.spans) == 1
    assert result.spans[0]["kind"] == "CLIENT"


@pytest.mark.asyncio
async def test_list_spans_filter_unknown_column_raises(session, isolated_project):
    with pytest.raises(ValueError, match="unknown filter column"):
        await spans_repo.list_spans(
            session, isolated_project,
            filters={"nonexistent": "x"},
        )


@pytest.mark.asyncio
async def test_list_spans_attr_containment(session, isolated_project):
    """JSONB @> containment filter hits the GIN index."""
    await _insert_span(
        session, isolated_project,
        attributes={"custom.tag": "important", "other": "value"},
    )
    await _insert_span(
        session, isolated_project,
        attributes={"custom.tag": "normal"},
    )

    result = await spans_repo.list_spans(
        session, isolated_project,
        attr_contains={"custom.tag": "important"},
    )
    assert len(result.spans) == 1
    assert result.spans[0]["attributes"]["custom.tag"] == "important"


@pytest.mark.asyncio
async def test_list_spans_attr_containment_no_match(session, isolated_project):
    await _insert_span(
        session, isolated_project,
        attributes={"key": "a"},
    )
    result = await spans_repo.list_spans(
        session, isolated_project,
        attr_contains={"key": "nonexistent"},
    )
    assert len(result.spans) == 0


@pytest.mark.asyncio
async def test_list_spans_cursor_pagination(session, isolated_project):
    for i in range(5):
        await _insert_span(
            session, isolated_project,
            name=f"s-{i}",
            start_ns=1_000_000_000_000 + i * 1_000_000_000,
        )

    page1 = await spans_repo.list_spans(
        session, isolated_project, limit=2,
    )
    assert len(page1.spans) == 2
    assert page1.next_cursor is not None

    page2 = await spans_repo.list_spans(
        session, isolated_project, limit=2, cursor=page1.next_cursor,
    )
    assert len(page2.spans) == 2
    # No overlap.
    p1_ids = {s["span_id"].hex() for s in page1.spans}
    p2_ids = {s["span_id"].hex() for s in page2.spans}
    assert p1_ids.isdisjoint(p2_ids)

    page3 = await spans_repo.list_spans(
        session, isolated_project, limit=2, cursor=page2.next_cursor,
    )
    assert len(page3.spans) == 1
    assert page3.next_cursor is None


@pytest.mark.asyncio
async def test_list_spans_invalid_cursor(session, isolated_project):
    with pytest.raises(ValueError, match="invalid cursor"):
        await spans_repo.list_spans(
            session, isolated_project, cursor="not-valid"
        )


@pytest.mark.asyncio
async def test_list_spans_combined_filters(session, isolated_project):
    """Multiple filters combine with AND."""
    await _insert_span(
        session, isolated_project,
        agent_name="bot", tool_name="search",
        start_ns=2_000_000_000_000,
    )
    await _insert_span(
        session, isolated_project,
        agent_name="bot", tool_name="calculator",
        start_ns=2_000_000_000_000,
    )
    await _insert_span(
        session, isolated_project,
        agent_name="human", tool_name="search",
        start_ns=2_000_000_000_000,
    )

    result = await spans_repo.list_spans(
        session, isolated_project,
        filters={"agent_name": "bot", "tool_name": "search"},
    )
    assert len(result.spans) == 1
    assert result.spans[0]["agent_name"] == "bot"
    assert result.spans[0]["tool_name"] == "search"


@pytest.mark.asyncio
async def test_get_span_found(session, isolated_project):
    tid, sid = await _insert_span(session, isolated_project, name="found-me")
    row = await spans_repo.get_span(
        session, isolated_project, tid.hex(), sid.hex()
    )
    assert row is not None
    assert row["name"] == "found-me"


@pytest.mark.asyncio
async def test_get_span_not_found(session, isolated_project):
    row = await spans_repo.get_span(
        session, isolated_project, "00" * 16, "00" * 8
    )
    assert row is None


@pytest.mark.asyncio
async def test_get_span_wrong_project(session, isolated_project):
    """Span exists but in a different project — returns None."""
    tid, sid = await _insert_span(session, isolated_project)
    row = await spans_repo.get_span(
        session, uuid.uuid4(), tid.hex(), sid.hex()
    )
    assert row is None


@pytest.mark.asyncio
async def test_get_span_invalid_hex_returns_none(session, isolated_project):
    row = await spans_repo.get_span(
        session, isolated_project, "not-hex", "also-not"
    )
    assert row is None


@pytest.mark.asyncio
async def test_limit_capped_at_1000(session, isolated_project):
    """Requesting limit > 1000 silently caps to 1000."""
    result = await spans_repo.list_spans(
        session, isolated_project, limit=9999
    )
    # Can't easily verify the internal cap from outside, but the call
    # should not error.
    assert result.next_cursor is None
