"""Tests for POST /v1/policies/simulate.

Covers: CEL validation, applies_to filtering, time windowing,
match counting, response shape, scope enforcement.
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
    conn = psycopg.connect(db_url, autocommit=True)
    try:
        row = conn.execute(
            "SELECT id FROM projects WHERE slug = 'default'"
        ).fetchone()
        assert row is not None
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


def _insert_spans(db_url, project_id, spans_data):
    """Insert test spans. spans_data is a list of dicts with
    name, attributes, and optionally agent_name/tool_name.
    Returns list of (trace_id, span_id) for cleanup."""
    conn = psycopg.connect(db_url, autocommit=True)
    inserted = []
    try:
        for i, s in enumerate(spans_data):
            trace_id = os.urandom(16)
            span_id = os.urandom(8)
            now_ns = int(time.time() * 1e9) - (len(spans_data) - i) * 100_000_000
            conn.execute(
                "INSERT INTO traces (id, project_id, start_time_unix_nano) "
                "VALUES (%s, %s::uuid, %s) ON CONFLICT DO NOTHING",
                (trace_id, str(project_id), now_ns),
            )
            conn.execute(
                "INSERT INTO spans "
                "(trace_id, span_id, project_id, name, kind, "
                " start_time_unix_nano, end_time_unix_nano, "
                " agent_name, tool_name, attributes) "
                "VALUES (%s, %s, %s::uuid, %s, 'INTERNAL', %s, %s, %s, %s, %s::jsonb)",
                (
                    trace_id, span_id, str(project_id),
                    s.get("name", "test-span"),
                    now_ns, now_ns + 50_000_000,
                    s.get("agent_name"),
                    s.get("tool_name"),
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


# --- Scope enforcement -------------------------------------------------------


def test_simulate_requires_policies_read(client):
    key = _mint(client, f"wr-{uuid.uuid4().hex[:6]}", ["traces:write"])
    resp = client.post(
        "/v1/policies/simulate",
        headers={"Authorization": f"Bearer {key}"},
        json={"match_expression": "true"},
    )
    assert resp.status_code == 403


def test_simulate_succeeds_with_policies_read(client):
    key = _mint(client, f"rd-{uuid.uuid4().hex[:6]}", ["policies:read"])
    resp = client.post(
        "/v1/policies/simulate",
        headers={"Authorization": f"Bearer {key}"},
        json={"match_expression": "true"},
    )
    assert resp.status_code == 200


# --- CEL validation ----------------------------------------------------------


def test_simulate_rejects_bad_expression(client):
    key = _mint(client, f"bad-{uuid.uuid4().hex[:6]}", ["policies:read"])
    resp = client.post(
        "/v1/policies/simulate",
        headers={"Authorization": f"Bearer {key}"},
        json={"match_expression": "this is not valid CEL !!!"},
    )
    assert resp.status_code == 400
    assert "match_expression" in resp.json()["detail"]


def test_simulate_rejects_empty_expression(client):
    key = _mint(client, f"emp-{uuid.uuid4().hex[:6]}", ["policies:read"])
    resp = client.post(
        "/v1/policies/simulate",
        headers={"Authorization": f"Bearer {key}"},
        json={"match_expression": ""},
    )
    assert resp.status_code == 400


# --- Matching ----------------------------------------------------------------


def test_simulate_true_matches_all_spans(client, db_url, default_project_id):
    key = _mint(client, f"all-{uuid.uuid4().hex[:6]}", ["policies:read"])
    spans = _insert_spans(db_url, default_project_id, [
        {"name": "a", "attributes": {}},
        {"name": "b", "attributes": {}},
        {"name": "c", "attributes": {}},
    ])
    try:
        resp = client.post(
            "/v1/policies/simulate",
            headers={"Authorization": f"Bearer {key}"},
            json={"match_expression": "true", "scan_limit": 100},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["summary"]["scanned"] >= 3
        assert body["summary"]["matched"] >= 3
        assert body["summary"]["match_rate"] > 0
        assert body["summary"]["elapsed_ms"] >= 0
        assert body["summary"]["uses_redacted_data"] is True
    finally:
        _cleanup(db_url, spans)


def test_simulate_false_matches_nothing(client, db_url, default_project_id):
    key = _mint(client, f"no-{uuid.uuid4().hex[:6]}", ["policies:read"])
    spans = _insert_spans(db_url, default_project_id, [
        {"name": "x", "attributes": {}},
    ])
    try:
        resp = client.post(
            "/v1/policies/simulate",
            headers={"Authorization": f"Bearer {key}"},
            json={"match_expression": "false", "scan_limit": 100},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["summary"]["matched"] == 0
        assert body["summary"]["match_rate"] == 0.0
        assert body["matches"] == []
    finally:
        _cleanup(db_url, spans)


def test_simulate_attr_match(client, db_url, default_project_id):
    """CEL expression matching on JSONB attributes."""
    key = _mint(client, f"attr-{uuid.uuid4().hex[:6]}", ["policies:read"])
    tag = f"tag-{uuid.uuid4().hex[:6]}"
    spans = _insert_spans(db_url, default_project_id, [
        {"name": "tool.search", "attributes": {"custom.label": tag}},
        {"name": "tool.other", "attributes": {"custom.label": "nope"}},
    ])
    try:
        resp = client.post(
            "/v1/policies/simulate",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "match_expression": f'attrs["custom.label"] == "{tag}"',
                "scan_limit": 100,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["summary"]["matched"] >= 1
        # The matching span should have our tag.
        match_attrs = [m["attributes"] for m in body["matches"]]
        assert any(a.get("custom.label") == tag for a in match_attrs)
    finally:
        _cleanup(db_url, spans)


def test_simulate_applies_to_filters(client, db_url, default_project_id):
    """applies_to restricts which spans are evaluated."""
    key = _mint(client, f"apt-{uuid.uuid4().hex[:6]}", ["policies:read"])
    spans = _insert_spans(db_url, default_project_id, [
        {"name": "langgraph.tool.search", "attributes": {}},
        {"name": "langgraph.llm.generate", "attributes": {}},
    ])
    try:
        # Only evaluate against tool spans.
        resp = client.post(
            "/v1/policies/simulate",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "match_expression": "true",
                "applies_to": ["tool"],
                "scan_limit": 100,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        # Both scanned but only tool.* matched.
        matched_names = [m["name"] for m in body["matches"]]
        assert any("tool" in n for n in matched_names)
        assert all("llm" not in n for n in matched_names)
    finally:
        _cleanup(db_url, spans)


# --- Time windowing ----------------------------------------------------------


def test_simulate_time_window(client, db_url, default_project_id):
    key = _mint(client, f"tw-{uuid.uuid4().hex[:6]}", ["policies:read"])
    # Default window is last 24h. A span from "now" should be found.
    spans = _insert_spans(db_url, default_project_id, [
        {"name": "recent", "attributes": {}},
    ])
    try:
        resp = client.post(
            "/v1/policies/simulate",
            headers={"Authorization": f"Bearer {key}"},
            json={"match_expression": "true"},
        )
        assert resp.status_code == 200
        assert resp.json()["summary"]["scanned"] >= 1
    finally:
        _cleanup(db_url, spans)


def test_simulate_rejects_inverted_window(client):
    key = _mint(client, f"inv-{uuid.uuid4().hex[:6]}", ["policies:read"])
    resp = client.post(
        "/v1/policies/simulate",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "match_expression": "true",
            "start_after": "2026-12-31T00:00:00Z",
            "start_before": "2026-01-01T00:00:00Z",
        },
    )
    assert resp.status_code == 400
    assert "start_after" in resp.json()["detail"]


# --- Response shape -----------------------------------------------------------


def test_simulate_response_shape(client, db_url, default_project_id):
    key = _mint(client, f"shape-{uuid.uuid4().hex[:6]}", ["policies:read"])
    spans = _insert_spans(db_url, default_project_id, [
        {"name": "test", "attributes": {"k": "v"}},
    ])
    try:
        resp = client.post(
            "/v1/policies/simulate",
            headers={"Authorization": f"Bearer {key}"},
            json={"match_expression": "true", "scan_limit": 10},
        )
        assert resp.status_code == 200
        body = resp.json()
        # Summary fields.
        s = body["summary"]
        assert isinstance(s["scanned"], int)
        assert isinstance(s["matched"], int)
        assert isinstance(s["match_rate"], float)
        assert isinstance(s["elapsed_ms"], int)
        assert isinstance(s["truncated"], bool)
        assert isinstance(s["uses_redacted_data"], bool)
        # Matches are SpanRead-shaped.
        if body["matches"]:
            m = body["matches"][0]
            assert "trace_id" in m
            assert "span_id" in m
            assert "start_time" in m
            assert "tokens" in m
            assert "cost" in m
    finally:
        _cleanup(db_url, spans)


def test_simulate_scan_limit_caps(client):
    """scan_limit > MAX_SCAN_LIMIT (10,000) is rejected by pydantic."""
    key = _mint(client, f"cap-{uuid.uuid4().hex[:6]}", ["policies:read"])
    resp = client.post(
        "/v1/policies/simulate",
        headers={"Authorization": f"Bearer {key}"},
        json={"match_expression": "true", "scan_limit": 99999},
    )
    assert resp.status_code == 422  # Pydantic validation error
