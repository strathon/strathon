"""Tests for the agent topology map endpoint."""

from __future__ import annotations

import os
import sys

import pytest

_RECEIVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _RECEIVER_DIR)

DEV_KEY = "stra_dev_local_default_project_do_not_use_in_production"


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture(scope="module")
def client():
    db_url = os.getenv(
        "DATABASE_URL",
        "postgresql://strathon:strathon_dev@127.0.0.1:5432/strathon",
    )
    os.environ["DATABASE_URL"] = db_url
    try:
        import psycopg
        conn = psycopg.connect(db_url, autocommit=True)
        conn.close()
    except Exception:
        pytest.skip("Postgres not reachable")

    from fastapi.testclient import TestClient
    import main

    with TestClient(main.app) as c:
        yield c


def test_topology_returns_200(client):
    resp = client.get("/v1/topology", headers=_auth(DEV_KEY))
    assert resp.status_code == 200
    data = resp.json()
    assert "nodes" in data
    assert "edges" in data
    assert "node_count" in data
    assert "edge_count" in data
    assert isinstance(data["nodes"], list)
    assert isinstance(data["edges"], list)


def test_topology_accepts_time_filters(client):
    resp = client.get(
        "/v1/topology",
        headers=_auth(DEV_KEY),
        params={"start_after": 0, "start_before": 9999999999999999999},
    )
    assert resp.status_code == 200


def test_topology_node_shape(client):
    """Nodes have id, type, name, span_count, error_count."""
    resp = client.get("/v1/topology", headers=_auth(DEV_KEY))
    data = resp.json()
    for node in data["nodes"]:
        assert "id" in node
        assert "type" in node
        assert node["type"] in ("agent", "tool")
        assert "name" in node
        assert "span_count" in node
        assert "error_count" in node


def test_topology_edge_shape(client):
    """Edges have source, target, call_count, error_count, avg_duration_ms."""
    resp = client.get("/v1/topology", headers=_auth(DEV_KEY))
    data = resp.json()
    for edge in data["edges"]:
        assert "source" in edge
        assert "target" in edge
        assert edge["source"].startswith("agent:")
        assert edge["target"].startswith("tool:")
        assert "call_count" in edge
        assert "error_count" in edge
        assert "avg_duration_ms" in edge


def test_topology_requires_auth(client):
    resp = client.get("/v1/topology")
    assert resp.status_code == 401
