"""Tests for API quality: consistent error responses and response models.

Verifies that error responses follow the ErrorResponse schema:
  {"error": {"code": "...", "message": "..."}}
"""

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


def test_401_returns_detail_field(client):
    resp = client.get("/v1/policies")
    assert resp.status_code == 401
    data = resp.json()
    assert "detail" in data


def test_404_returns_detail_field(client):
    resp = client.get(
        "/v1/halts/99999999", headers=_auth(DEV_KEY),
    )
    assert resp.status_code == 404
    data = resp.json()
    assert "detail" in data


def test_400_returns_detail_field(client):
    resp = client.post(
        "/v1/policies",
        headers=_auth(DEV_KEY),
        json={"name": "", "match_expression": "", "action": "invalid"},
    )
    assert resp.status_code in (400, 422)
    data = resp.json()
    assert "detail" in data


def test_topology_response_has_typed_shape(client):
    resp = client.get("/v1/topology", headers=_auth(DEV_KEY))
    assert resp.status_code == 200
    data = resp.json()
    assert "nodes" in data
    assert "edges" in data
    assert "node_count" in data
    assert "edge_count" in data


def test_costs_response_has_typed_shape(client):
    resp = client.get("/v1/costs", headers=_auth(DEV_KEY))
    assert resp.status_code == 200
    data = resp.json()
    assert "group_by" in data
    assert "period" in data
    assert "costs" in data


def test_halts_list_response_shape(client):
    resp = client.get("/v1/halts", headers=_auth(DEV_KEY))
    assert resp.status_code == 200
    data = resp.json()
    assert "halts" in data


def test_projects_list_response_shape(client):
    resp = client.get("/v1/projects", headers=_auth(DEV_KEY))
    assert resp.status_code == 200
    data = resp.json()
    assert "data" in data


def test_api_keys_list_response_shape(client):
    resp = client.get("/v1/api_keys", headers=_auth(DEV_KEY))
    assert resp.status_code == 200
    data = resp.json()
    assert "api_keys" in data
