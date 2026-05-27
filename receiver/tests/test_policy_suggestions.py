"""Tests for automated policy suggestions endpoint."""

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


def test_suggest_returns_200(client):
    resp = client.get("/v1/policies/suggest", headers=_auth(DEV_KEY))
    assert resp.status_code == 200
    data = resp.json()
    assert "suggestions" in data
    assert "count" in data
    assert "lookback_days" in data
    assert "agents_analyzed" in data
    assert isinstance(data["suggestions"], list)


def test_suggest_accepts_days_param(client):
    resp = client.get(
        "/v1/policies/suggest",
        headers=_auth(DEV_KEY),
        params={"days": 1},
    )
    assert resp.status_code == 200
    assert resp.json()["lookback_days"] == 1


def test_suggest_requires_auth(client):
    resp = client.get("/v1/policies/suggest")
    assert resp.status_code == 401


def test_suggestion_shape_when_present(client):
    """If suggestions exist, each has the required fields."""
    resp = client.get("/v1/policies/suggest", headers=_auth(DEV_KEY))
    data = resp.json()
    for s in data["suggestions"]:
        assert "risk_level" in s
        assert s["risk_level"] in ("high", "medium", "low")
        assert "owasp_ref" in s
        assert "description" in s
        assert "recommendation" in s
        # policy can be None (for budget recommendations).
        if s["policy"] is not None:
            assert "name" in s["policy"]
            assert "match_expression" in s["policy"]
            assert "action" in s["policy"]


def test_suggestions_sorted_by_risk(client):
    """Suggestions are sorted high > medium > low."""
    resp = client.get("/v1/policies/suggest", headers=_auth(DEV_KEY))
    data = resp.json()
    levels = [s["risk_level"] for s in data["suggestions"]]
    priority = {"high": 0, "medium": 1, "low": 2}
    priorities = [priority[level] for level in levels]
    assert priorities == sorted(priorities)
