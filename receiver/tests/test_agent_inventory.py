"""Tests for agent inventory endpoint."""

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


def test_agents_returns_200(client):
    resp = client.get("/v1/agents", headers=_auth(DEV_KEY))
    assert resp.status_code == 200
    data = resp.json()
    assert "agents" in data
    assert "count" in data
    assert "lookback_days" in data


def test_agents_accepts_days_param(client):
    resp = client.get(
        "/v1/agents", headers=_auth(DEV_KEY), params={"days": 1},
    )
    assert resp.status_code == 200
    assert resp.json()["lookback_days"] == 1


def test_agents_requires_auth(client):
    resp = client.get("/v1/agents")
    assert resp.status_code == 401


def test_agent_shape_when_present(client):
    resp = client.get("/v1/agents", headers=_auth(DEV_KEY))
    data = resp.json()
    for agent in data["agents"]:
        assert "agent_name" in agent
        assert "risk_score" in agent
        assert agent["risk_score"] in ("high", "medium", "low")
        assert "risk_factors" in agent
        assert isinstance(agent["risk_factors"], list)
        assert "tools_used" in agent
        assert "models_used" in agent
        assert "policies_covering" in agent
        assert "has_budget" in agent
