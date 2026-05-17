"""End-to-end tests for /v1/spans endpoints.

Covers scope enforcement, basic search, filtering, pagination, and
the single-span detail endpoint. Test spans are inserted via raw SQL
(bypassing OTLP ingest) to keep the test focused on the search
surface, not the ingest path which is tested separately.
"""

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
    """The seeded default project's UUID."""
    conn = psycopg.connect(db_url, autocommit=True)
    try:
        row = conn.execute(
            "SELECT id FROM projects WHERE slug = 'default'"
        ).fetchone()
        assert row is not None, "default project not found"
        return row[0]
    finally:
        conn.close()


def _mint(client, name: str, scopes: list[str]) -> str:
    resp = client.post(
        "/v1/api_keys",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
        json={"name": name, "scopes": scopes},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["key"]


def _insert_test_spans(db_url, project_id, count=3, **overrides):
    """Insert test trace+span rows directly into the DB."""
    conn = psycopg.connect(db_url, autocommit=True)
    inserted = []
    try:
        for i in range(count):
            trace_id = os.urandom(16)
            span_id = os.urandom(8)
            now_ns = int(time.time() * 1e9) - (count - i) * 1_000_000_000
            conn.execute(
                "INSERT INTO traces (id, project_id, start_time_unix_nano) "
                "VALUES (%s, %s::uuid, %s) ON CONFLICT DO NOTHING",
                (trace_id, str(project_id), now_ns),
            )
            conn.execute(
                "INSERT INTO spans "
                "(trace_id, span_id, project_id, name, kind, "
                " start_time_unix_nano, end_time_unix_nano, "
                " agent_name, tool_name, operation_name, attributes) "
                "VALUES (%s, %s, %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
                (
                    trace_id,
                    span_id,
                    str(project_id),
                    overrides.get("name", f"test-span-{i}"),
                    overrides.get("kind", "INTERNAL"),
                    now_ns,
                    now_ns + 500_000_000,
                    overrides.get("agent_name"),
                    overrides.get("tool_name"),
                    overrides.get("operation_name"),
                    json.dumps(overrides.get("attributes", {})),
                ),
            )
            inserted.append((trace_id, span_id))
    finally:
        conn.close()
    return inserted


def _cleanup_spans(db_url, spans):
    """Remove test spans (and cascading events/links)."""
    conn = psycopg.connect(db_url, autocommit=True)
    try:
        for trace_id, span_id in spans:
            conn.execute(
                "DELETE FROM spans WHERE trace_id = %s AND span_id = %s",
                (trace_id, span_id),
            )
            conn.execute(
                "DELETE FROM traces WHERE id = %s",
                (trace_id,),
            )
    finally:
        conn.close()


# --- Scope enforcement -------------------------------------------------------


def test_list_spans_requires_traces_read(client):
    key = _mint(client, f"wr-{uuid.uuid4().hex[:6]}", ["traces:write"])
    resp = client.get(
        "/v1/spans",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 403


def test_list_spans_succeeds_with_traces_read(client):
    key = _mint(client, f"rd-{uuid.uuid4().hex[:6]}", ["traces:read"])
    resp = client.get(
        "/v1/spans",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "data" in body
    assert isinstance(body["data"], list)


def test_get_span_requires_traces_read(client):
    key = _mint(client, f"wr-{uuid.uuid4().hex[:6]}", ["traces:write"])
    resp = client.get(
        f"/v1/spans/{'00' * 16}/{'00' * 8}",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 403


# --- Basic search + filtering ------------------------------------------------


def test_list_spans_returns_data(client, db_url, default_project_id):
    key = _mint(client, f"sr-{uuid.uuid4().hex[:6]}", ["traces:read"])
    spans = _insert_test_spans(db_url, default_project_id, count=2)
    try:
        resp = client.get(
            "/v1/spans?limit=10",
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["data"]) >= 2
        # Check response shape.
        first = body["data"][0]
        assert "trace_id" in first
        assert "span_id" in first
        assert "start_time" in first
        assert "tokens" in first
        assert "cost" in first
    finally:
        _cleanup_spans(db_url, spans)


def test_list_spans_filter_by_agent_name(client, db_url, default_project_id):
    key = _mint(client, f"fa-{uuid.uuid4().hex[:6]}", ["traces:read"])
    unique = f"agent-{uuid.uuid4().hex[:6]}"
    spans = _insert_test_spans(
        db_url, default_project_id, count=1, agent_name=unique,
    )
    _insert_test_spans(
        db_url, default_project_id, count=1, agent_name="other-agent",
    )
    try:
        resp = client.get(
            f"/v1/spans?agent_name={unique}",
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data) >= 1
        assert all(s["agent_name"] == unique for s in data)
    finally:
        _cleanup_spans(db_url, spans)


def test_list_spans_filter_by_attr(client, db_url, default_project_id):
    key = _mint(client, f"fa-{uuid.uuid4().hex[:6]}", ["traces:read"])
    tag = f"tag-{uuid.uuid4().hex[:6]}"
    spans = _insert_test_spans(
        db_url, default_project_id, count=1,
        attributes={"custom.label": tag},
    )
    try:
        resp = client.get(
            f"/v1/spans?attr.custom.label={tag}",
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data) >= 1
        assert data[0]["attributes"]["custom.label"] == tag
    finally:
        _cleanup_spans(db_url, spans)


def test_list_spans_time_range(client, db_url, default_project_id):
    key = _mint(client, f"tr-{uuid.uuid4().hex[:6]}", ["traces:read"])
    # Insert a span with a known timestamp.
    now_ns = int(time.time() * 1e9)
    spans = _insert_test_spans(
        db_url, default_project_id, count=1, name="in-window",
    )
    try:
        # Search with a tight window that includes "now".
        resp = client.get(
            f"/v1/spans?start_after={now_ns - 30_000_000_000}"
            f"&start_before={now_ns + 30_000_000_000}",
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 200
    finally:
        _cleanup_spans(db_url, spans)


def test_list_spans_time_range_iso(client, db_url, default_project_id):
    """ISO 8601 timestamps work too."""
    key = _mint(client, f"ti-{uuid.uuid4().hex[:6]}", ["traces:read"])
    resp = client.get(
        "/v1/spans?start_after=2020-01-01T00:00:00Z"
        "&start_before=2099-12-31T23:59:59Z",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200


def test_list_spans_bad_timestamp_returns_400(client):
    key = _mint(client, f"bt-{uuid.uuid4().hex[:6]}", ["traces:read"])
    resp = client.get(
        "/v1/spans?start_after=not-a-timestamp",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 400


# --- Pagination ---------------------------------------------------------------


def test_list_spans_cursor_pagination(client, db_url, default_project_id):
    key = _mint(client, f"pg-{uuid.uuid4().hex[:6]}", ["traces:read"])
    unique_name = f"paginate-{uuid.uuid4().hex[:6]}"
    spans = _insert_test_spans(
        db_url, default_project_id, count=5, name=unique_name,
    )
    try:
        resp1 = client.get(
            "/v1/spans?limit=2&agent_name=&operation_name=",
            headers={"Authorization": f"Bearer {key}"},
            params={"limit": 2},
        )
        assert resp1.status_code == 200
        body1 = resp1.json()
        assert body1["next_cursor"] is not None

        resp2 = client.get(
            f"/v1/spans?limit=2&cursor={body1['next_cursor']}",
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp2.status_code == 200
        body2 = resp2.json()
        # No overlapping span_ids between pages.
        ids1 = {s["span_id"] for s in body1["data"]}
        ids2 = {s["span_id"] for s in body2["data"]}
        assert ids1.isdisjoint(ids2)
    finally:
        _cleanup_spans(db_url, spans)


# --- Single span detail -------------------------------------------------------


def test_get_span_detail(client, db_url, default_project_id):
    key = _mint(client, f"gd-{uuid.uuid4().hex[:6]}", ["traces:read"])
    spans = _insert_test_spans(
        db_url, default_project_id, count=1, name="detail-test",
    )
    tid_hex = spans[0][0].hex()
    sid_hex = spans[0][1].hex()
    try:
        resp = client.get(
            f"/v1/spans/{tid_hex}/{sid_hex}",
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "detail-test"
        assert body["trace_id"] == tid_hex
        assert body["span_id"] == sid_hex
        assert "events" in body
        assert "links" in body
    finally:
        _cleanup_spans(db_url, spans)


def test_get_span_not_found(client):
    key = _mint(client, f"nf-{uuid.uuid4().hex[:6]}", ["traces:read"])
    resp = client.get(
        f"/v1/spans/{'00' * 16}/{'00' * 8}",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 404


def test_get_span_invalid_hex(client):
    key = _mint(client, f"ih-{uuid.uuid4().hex[:6]}", ["traces:read"])
    resp = client.get(
        "/v1/spans/not-hex/also-not",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 404
