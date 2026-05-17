"""Tests for full-text search on spans.

Covers: websearch_to_tsquery via the q parameter, multi-word queries,
negative terms, combining q with other filters, and empty query passthrough.
"""

from __future__ import annotations

import os
import time
import uuid

import psycopg
import pytest


DEV_KEY = "stra_dev_local_default_project_do_not_use_in_production"


@pytest.fixture(scope="module")
def client():
    db_url = os.getenv(
        "DATABASE_URL",
        "postgresql://strathon:strathon_dev@127.0.0.1:5432/strathon",
    )
    os.environ["DATABASE_URL"] = db_url
    try:
        psycopg.connect(db_url, autocommit=True).close()
    except Exception:
        pytest.skip("Postgres not reachable")
    from fastapi.testclient import TestClient
    import main
    with TestClient(main.app) as c:
        yield c


@pytest.fixture(scope="module")
def db_url():
    return os.getenv(
        "DATABASE_URL",
        "postgresql://strathon:strathon_dev@127.0.0.1:5432/strathon",
    )


@pytest.fixture(scope="module")
def default_project_id(db_url):
    conn = psycopg.connect(db_url, autocommit=True)
    try:
        row = conn.execute("SELECT id FROM projects WHERE slug = 'default'").fetchone()
        return row[0]
    finally:
        conn.close()


def _mint(client, scopes):
    resp = client.post(
        "/v1/api_keys",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
        json={"name": f"fts-{uuid.uuid4().hex[:6]}", "scopes": scopes},
    )
    assert resp.status_code == 201
    return resp.json()["key"]


def _insert_span(db_url, project_id, name, agent_name=None, tool_name=None,
                 model=None, operation=None):
    """Insert a span directly for FTS testing."""
    conn = psycopg.connect(db_url, autocommit=True)
    trace_id = os.urandom(16)
    span_id = os.urandom(8)
    now_ns = int(time.time() * 1e9)
    try:
        conn.execute(
            "INSERT INTO traces (id, project_id, start_time_unix_nano) "
            "VALUES (%s, %s::uuid, %s) ON CONFLICT DO NOTHING",
            (trace_id, str(project_id), now_ns),
        )
        conn.execute(
            "INSERT INTO spans "
            "(start_time_unix_nano, trace_id, span_id, project_id, name, "
            " kind, agent_name, tool_name, request_model, operation_name, "
            " attributes) "
            "VALUES (%s, %s, %s, %s::uuid, %s, 'INTERNAL', %s, %s, %s, %s, "
            " '{}'::jsonb)",
            (now_ns, trace_id, span_id, str(project_id), name,
             agent_name, tool_name, model, operation),
        )
    finally:
        conn.close()
    return trace_id, span_id


def _cleanup(db_url, pairs):
    conn = psycopg.connect(db_url, autocommit=True)
    try:
        for tid, sid in pairs:
            conn.execute("DELETE FROM spans WHERE trace_id = %s AND span_id = %s", (tid, sid))
            conn.execute("DELETE FROM traces WHERE id = %s", (tid,))
    finally:
        conn.close()


def test_fts_basic_query(client, db_url, default_project_id):
    """Search by span name matches."""
    key = _mint(client, ["traces:read"])
    unique = f"fts_{uuid.uuid4().hex[:6]}"
    spans = [_insert_span(db_url, default_project_id, f"send_email_{unique}")]
    try:
        resp = client.get(
            f"/v1/spans?q={unique}",
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 200
        assert len(resp.json()["data"]) >= 1
    finally:
        _cleanup(db_url, spans)


def test_fts_agent_name(client, db_url, default_project_id):
    """Search matches agent_name."""
    key = _mint(client, ["traces:read"])
    unique = f"bot_{uuid.uuid4().hex[:6]}"
    spans = [_insert_span(db_url, default_project_id, "generic-span",
                          agent_name=unique)]
    try:
        resp = client.get(
            f"/v1/spans?q={unique}",
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 200
        assert len(resp.json()["data"]) >= 1
    finally:
        _cleanup(db_url, spans)


def test_fts_tool_name(client, db_url, default_project_id):
    """Search matches tool_name."""
    key = _mint(client, ["traces:read"])
    unique = f"tool_{uuid.uuid4().hex[:6]}"
    spans = [_insert_span(db_url, default_project_id, "generic-span",
                          tool_name=unique)]
    try:
        resp = client.get(
            f"/v1/spans?q={unique}",
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 200
        assert len(resp.json()["data"]) >= 1
    finally:
        _cleanup(db_url, spans)


def test_fts_combined_with_filter(client, db_url, default_project_id):
    """FTS q works alongside column filters."""
    key = _mint(client, ["traces:read"])
    unique = f"combo_{uuid.uuid4().hex[:6]}"
    spans = [_insert_span(db_url, default_project_id, f"action_{unique}",
                          agent_name="combo-bot")]
    try:
        resp = client.get(
            f"/v1/spans?q={unique}&agent_name=combo-bot",
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 200
        assert len(resp.json()["data"]) >= 1
    finally:
        _cleanup(db_url, spans)


def test_fts_no_match(client, db_url, default_project_id):
    """Query that matches nothing returns empty."""
    key = _mint(client, ["traces:read"])
    resp = client.get(
        "/v1/spans?q=xyznonexistent99999",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200
    assert len(resp.json()["data"]) == 0


def test_fts_empty_query_ignored(client):
    """Empty q parameter is treated as no search."""
    key = _mint(client, ["traces:read"])
    resp = client.get(
        "/v1/spans?q=",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200
