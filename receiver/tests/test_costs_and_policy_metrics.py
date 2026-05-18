"""Tests for cost attribution and policy evaluation metrics.

API-level (TestClient):
- GET /v1/costs returns cost rollups
- GET /v1/costs validates group_by and period params
- Policy match_count and last_matched_at visible in policy read

Repository-level (async, session fixtures):
- record_match increments match_count on policy
- record_match sets last_matched_at on policy
"""

from __future__ import annotations

import os
import sys
import uuid

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


# ---------------------------------------------------------------------------
# Cost attribution (API-level)
# ---------------------------------------------------------------------------


def test_costs_endpoint_returns_200(client):
    resp = client.get("/v1/costs", headers=_auth(DEV_KEY))
    assert resp.status_code == 200
    data = resp.json()
    assert "costs" in data
    assert "group_by" in data
    assert "period" in data


def test_costs_group_by_agent(client):
    resp = client.get(
        "/v1/costs", headers=_auth(DEV_KEY), params={"group_by": "agent"},
    )
    assert resp.status_code == 200
    assert resp.json()["group_by"] == "agent"


def test_costs_group_by_agent_model(client):
    resp = client.get(
        "/v1/costs",
        headers=_auth(DEV_KEY),
        params={"group_by": "agent_model"},
    )
    assert resp.status_code == 200
    assert resp.json()["group_by"] == "agent_model"


def test_costs_period_week(client):
    resp = client.get(
        "/v1/costs", headers=_auth(DEV_KEY), params={"period": "week"},
    )
    assert resp.status_code == 200
    assert resp.json()["period"] == "week"


def test_costs_invalid_group_by_returns_400(client):
    resp = client.get(
        "/v1/costs",
        headers=_auth(DEV_KEY),
        params={"group_by": "invalid"},
    )
    assert resp.status_code == 400


def test_costs_invalid_period_returns_400(client):
    resp = client.get(
        "/v1/costs",
        headers=_auth(DEV_KEY),
        params={"period": "invalid"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Policy evaluation metrics (API-level)
# ---------------------------------------------------------------------------


def test_policy_read_includes_match_count(client):
    """PolicyRead now includes match_count and last_matched_at."""
    # Create a policy.
    resp = client.post(
        "/v1/policies",
        headers=_auth(DEV_KEY),
        json={
            "name": f"metrics-{uuid.uuid4().hex[:8]}",
            "match_expression": "true",
            "action": "log",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    policy_id = data["id"]

    try:
        assert "match_count" in data
        assert data["match_count"] == 0
        assert "last_matched_at" in data
        assert data["last_matched_at"] is None
    finally:
        client.delete(f"/v1/policies/{policy_id}", headers=_auth(DEV_KEY))


# ---------------------------------------------------------------------------
# Policy metrics (repository-level)
# ---------------------------------------------------------------------------


async def test_record_match_increments_match_count(session, isolated_project):
    """record_match atomically increments match_count on the policy row."""
    from repositories.policies import create_policy, record_match
    from models.policies import Policy
    from sqlalchemy import select

    policy = await create_policy(
        session,
        isolated_project,
        name="counter-test",
        match_expression="true",
        action="log",
    )
    assert policy.match_count == 0

    # Record two matches.
    await record_match(
        session, policy.id, isolated_project,
        b"\x01" * 16, b"\x01" * 8, "log", "matched",
    )
    await record_match(
        session, policy.id, isolated_project,
        b"\x02" * 16, b"\x02" * 8, "log", "matched",
    )
    await session.flush()

    stmt = select(Policy).where(Policy.id == policy.id)
    row = (await session.execute(stmt)).scalar_one()
    assert row.match_count == 2
    assert row.last_matched_at is not None
