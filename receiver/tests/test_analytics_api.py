"""Tests for span aggregation and trace tree endpoints."""

from __future__ import annotations

import json
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


def _mint(client, name, scopes):
    resp = client.post(
        "/v1/api_keys",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
        json={"name": name, "scopes": scopes},
    )
    assert resp.status_code == 201
    return resp.json()["key"]


def _insert_spans(db_url, project_id, spans_data):
    """Insert test spans. Returns list of (trace_id, span_id)."""
    conn = psycopg.connect(db_url, autocommit=True)
    inserted = []
    try:
        for i, s in enumerate(spans_data):
            trace_id = s.get("trace_id") or os.urandom(16)
            span_id = os.urandom(8)
            parent = s.get("parent_span_id")
            now_ns = int(time.time() * 1e9) - (len(spans_data) - i) * 100_000_000
            conn.execute(
                "INSERT INTO traces (id, project_id, start_time_unix_nano) "
                "VALUES (%s, %s::uuid, %s) ON CONFLICT DO NOTHING",
                (trace_id, str(project_id), now_ns),
            )
            conn.execute(
                "INSERT INTO spans "
                "(start_time_unix_nano, trace_id, span_id, parent_span_id, "
                " project_id, name, kind, end_time_unix_nano, "
                " agent_name, tool_name, request_model, cost_usd, "
                " input_tokens, output_tokens, attributes) "
                "VALUES (%s, %s, %s, %s, %s::uuid, %s, 'INTERNAL', %s, "
                " %s, %s, %s, %s, %s, %s, %s::jsonb)",
                (
                    now_ns, trace_id, span_id, parent,
                    str(project_id),
                    s.get("name", f"span-{i}"),
                    now_ns + 50_000_000,
                    s.get("agent_name"),
                    s.get("tool_name"),
                    s.get("request_model"),
                    s.get("cost_usd"),
                    s.get("input_tokens"),
                    s.get("output_tokens"),
                    json.dumps(s.get("attributes", {})),
                ),
            )
            inserted.append((trace_id, span_id))
    finally:
        conn.close()
    return inserted


def _cleanup(db_url, spans):
    conn = psycopg.connect(db_url, autocommit=True)
    try:
        for tid, sid in spans:
            conn.execute("DELETE FROM spans WHERE trace_id = %s AND span_id = %s", (tid, sid))
            conn.execute("DELETE FROM traces WHERE id = %s", (tid,))
    finally:
        conn.close()


# --- Aggregation tests --------------------------------------------------------


def test_aggregate_requires_traces_read(client):
    key = _mint(client, f"ag-{uuid.uuid4().hex[:6]}", ["policies:read"])
    resp = client.get(
        "/v1/spans/aggregate",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 403


def test_aggregate_by_model(client, db_url, default_project_id):
    key = _mint(client, f"ag-{uuid.uuid4().hex[:6]}", ["traces:read"])
    spans = _insert_spans(db_url, default_project_id, [
        {"request_model": "gpt-4o", "cost_usd": 0.01, "input_tokens": 100, "output_tokens": 50},
        {"request_model": "gpt-4o", "cost_usd": 0.02, "input_tokens": 200, "output_tokens": 100},
        {"request_model": "claude-3", "cost_usd": 0.05, "input_tokens": 500, "output_tokens": 200},
    ])
    try:
        resp = client.get(
            "/v1/spans/aggregate?group_by=request_model",
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data) >= 2
        assert resp.json()["group_by"] == "request_model"
    finally:
        _cleanup(db_url, spans)


def test_aggregate_by_agent(client, db_url, default_project_id):
    key = _mint(client, f"ag-{uuid.uuid4().hex[:6]}", ["traces:read"])
    spans = _insert_spans(db_url, default_project_id, [
        {"agent_name": "bot-a"},
        {"agent_name": "bot-a"},
        {"agent_name": "bot-b"},
    ])
    try:
        resp = client.get(
            "/v1/spans/aggregate?group_by=agent_name",
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert any(d["dimension"] == "bot-a" for d in data)
    finally:
        _cleanup(db_url, spans)


def test_aggregate_with_time_bucket(client, db_url, default_project_id):
    key = _mint(client, f"ag-{uuid.uuid4().hex[:6]}", ["traces:read"])
    spans = _insert_spans(db_url, default_project_id, [
        {"request_model": "gpt-4o"},
    ])
    try:
        resp = client.get(
            "/v1/spans/aggregate?group_by=request_model&time_bucket=1d",
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        if data:
            assert "bucket" in data[0]
    finally:
        _cleanup(db_url, spans)


def test_aggregate_invalid_group_by(client):
    key = _mint(client, f"ag-{uuid.uuid4().hex[:6]}", ["traces:read"])
    resp = client.get(
        "/v1/spans/aggregate?group_by=nonexistent",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 400


def test_aggregate_time_range(client, db_url, default_project_id):
    key = _mint(client, f"ag-{uuid.uuid4().hex[:6]}", ["traces:read"])
    resp = client.get(
        "/v1/spans/aggregate?start_after=2020-01-01T00:00:00Z"
        "&start_before=2099-12-31T23:59:59Z",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200


# --- Trace tree tests ---------------------------------------------------------


def test_trace_tree_requires_traces_read(client):
    key = _mint(client, f"tt-{uuid.uuid4().hex[:6]}", ["policies:read"])
    resp = client.get(
        f"/v1/traces/{'00' * 16}/tree",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 403


def test_trace_tree_not_found(client):
    key = _mint(client, f"tt-{uuid.uuid4().hex[:6]}", ["traces:read"])
    resp = client.get(
        f"/v1/traces/{'00' * 16}/tree",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 404


def test_trace_tree_builds_hierarchy(client, db_url, default_project_id):
    key = _mint(client, f"tt-{uuid.uuid4().hex[:6]}", ["traces:read"])
    trace_id = os.urandom(16)
    child_span = os.urandom(8)

    spans = _insert_spans(db_url, default_project_id, [
        {"trace_id": trace_id, "name": "root", "agent_name": "bot"},
    ])
    # Insert child manually with parent_span_id.
    conn = psycopg.connect(db_url, autocommit=True)
    now_ns = int(time.time() * 1e9)
    try:
        conn.execute(
            "INSERT INTO spans "
            "(start_time_unix_nano, trace_id, span_id, parent_span_id, "
            " project_id, name, kind, attributes) "
            "VALUES (%s, %s, %s, %s, %s::uuid, 'child-tool', 'CLIENT', '{}'::jsonb)",
            (now_ns, trace_id, child_span, spans[0][1], str(default_project_id)),
        )
    finally:
        conn.close()

    try:
        resp = client.get(
            f"/v1/traces/{trace_id.hex()}/tree",
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["span_count"] >= 2
        assert body["trace"]["trace_id"] == trace_id.hex()
        # Root should have children.
        root = body["root"]
        if isinstance(root, list):
            root = root[0]
        assert root["name"] == "root"
        assert len(root["children"]) >= 1
        assert root["children"][0]["name"] == "child-tool"
    finally:
        conn = psycopg.connect(db_url, autocommit=True)
        try:
            conn.execute("DELETE FROM spans WHERE trace_id = %s", (trace_id,))
            conn.execute("DELETE FROM traces WHERE id = %s", (trace_id,))
        finally:
            conn.close()


def test_trace_tree_invalid_hex(client):
    key = _mint(client, f"tt-{uuid.uuid4().hex[:6]}", ["traces:read"])
    resp = client.get(
        "/v1/traces/not-hex/tree",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 404


# --- Trace list tests ---------------------------------------------------------


def test_trace_list_requires_traces_read(client):
    key = _mint(client, f"tl-{uuid.uuid4().hex[:6]}", ["policies:read"])
    resp = client.get(
        "/v1/traces",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 403


def test_trace_list_returns_data(client, db_url, default_project_id):
    key = _mint(client, f"tl-{uuid.uuid4().hex[:6]}", ["traces:read"])
    spans = _insert_spans(db_url, default_project_id, [
        {"agent_name": "bot-list"},
    ])
    try:
        resp = client.get(
            "/v1/traces",
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body
        assert isinstance(body["data"], list)
        if body["data"]:
            t = body["data"][0]
            assert "trace_id" in t
            assert "project_id" in t
            assert "start_time_unix_nano" in t
    finally:
        _cleanup(db_url, spans)


def test_trace_list_filter_agent(client, db_url, default_project_id):
    key = _mint(client, f"tl-{uuid.uuid4().hex[:6]}", ["traces:read"])
    unique = f"agent-{uuid.uuid4().hex[:6]}"
    spans = _insert_spans(db_url, default_project_id, [
        {"agent_name": unique},
    ])
    # Also update the trace's agent_name (insert_spans doesn't set it on traces)
    conn = psycopg.connect(db_url, autocommit=True)
    try:
        conn.execute(
            "UPDATE traces SET agent_name = %s WHERE id = %s",
            (unique, spans[0][0]),
        )
    finally:
        conn.close()
    try:
        resp = client.get(
            f"/v1/traces?agent_name={unique}",
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data) >= 1
        assert all(t.get("agent_name") == unique for t in data)
    finally:
        _cleanup(db_url, spans)


def test_trace_list_pagination(client, db_url, default_project_id):
    key = _mint(client, f"tl-{uuid.uuid4().hex[:6]}", ["traces:read"])
    # Insert multiple traces.
    all_spans = []
    for _ in range(5):
        spans = _insert_spans(db_url, default_project_id, [{"name": "pg-test"}])
        all_spans.extend(spans)
    try:
        resp1 = client.get(
            "/v1/traces?limit=2",
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp1.status_code == 200
        body1 = resp1.json()
        assert body1["next_cursor"] is not None

        resp2 = client.get(
            f"/v1/traces?limit=2&cursor={body1['next_cursor']}",
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp2.status_code == 200
        ids1 = {t["trace_id"] for t in body1["data"]}
        ids2 = {t["trace_id"] for t in resp2.json()["data"]}
        assert ids1.isdisjoint(ids2)
    finally:
        _cleanup(db_url, all_spans)
