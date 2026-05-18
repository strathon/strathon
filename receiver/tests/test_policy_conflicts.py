"""Tests for policy conflict detection."""

from __future__ import annotations

import os
import uuid

import pytest

from policy_conflicts import detect_conflicts


def _policy(name, expr, action, pid=None):
    return {
        "id": pid or str(uuid.uuid4()),
        "name": name,
        "match_expression": expr,
        "action": action,
        "enabled": True,
    }


def test_no_conflicts():
    policies = [
        _policy("a", 'attrs["gen_ai.tool.name"] == "foo"', "block"),
        _policy("b", 'attrs["gen_ai.tool.name"] == "bar"', "log"),
    ]
    assert detect_conflicts(policies) == []


def test_exact_contradiction():
    policies = [
        _policy("a", "true", "block"),
        _policy("b", "true", "allow"),
    ]
    result = detect_conflicts(policies)
    assert len(result) == 1
    assert result[0]["type"] == "contradiction"


def test_exact_redundancy():
    policies = [
        _policy("a", "true", "log"),
        _policy("b", "true", "log"),
    ]
    result = detect_conflicts(policies)
    assert len(result) == 1
    assert result[0]["type"] == "redundancy"


def test_tool_name_contradiction():
    policies = [
        _policy("a", 'attrs["gen_ai.tool.name"] == "send_email"', "block"),
        _policy("b", 'attrs["gen_ai.tool.name"] == "send_email" && attrs["x"] == "y"', "allow"),
    ]
    result = detect_conflicts(policies)
    assert len(result) == 1
    assert result[0]["type"] == "contradiction"
    assert "send_email" in result[0]["reason"]


def test_disabled_policies_ignored():
    policies = [
        _policy("a", "true", "block"),
        {"id": str(uuid.uuid4()), "name": "b", "match_expression": "true",
         "action": "allow", "enabled": False},
    ]
    assert detect_conflicts(policies) == []


def test_different_tools_no_conflict():
    policies = [
        _policy("a", 'attrs["gen_ai.tool.name"] == "foo"', "block"),
        _policy("b", 'attrs["gen_ai.tool.name"] == "bar"', "allow"),
    ]
    assert detect_conflicts(policies) == []


# ---- API test ----------------------------------------------------------------

DEV_KEY = "stra_dev_local_default_project_do_not_use_in_production"


@pytest.fixture(scope="module")
def client():
    db_url = os.getenv(
        "DATABASE_URL",
        "postgresql://strathon:strathon_dev@127.0.0.1:5432/strathon",
    )
    os.environ["DATABASE_URL"] = db_url
    import psycopg
    try:
        psycopg.connect(db_url, autocommit=True).close()
    except Exception:
        pytest.skip("Postgres not reachable")
    from fastapi.testclient import TestClient
    import main
    with TestClient(main.app) as c:
        yield c


def test_conflicts_endpoint(client):
    # Create two contradicting policies.
    ids = []
    for action in ("block", "allow"):
        r = client.post(
            "/v1/policies",
            headers={"Authorization": f"Bearer {DEV_KEY}"},
            json={
                "name": f"conflict-{action}-{uuid.uuid4().hex[:6]}",
                "match_expression": 'attrs["gen_ai.tool.name"] == "conflict_test_tool"',
                "action": action,
            },
        )
        assert r.status_code == 201
        ids.append(r.json()["id"])

    resp = client.get(
        "/v1/policies/conflicts",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["policies_analyzed"] >= 2
    # The two policies have the same match_expression but different actions.
    found = any(
        c["type"] == "contradiction"
        for c in body["conflicts"]
    )
    assert found, f"expected contradiction, got: {body['conflicts']}"

    for pid in ids:
        client.delete(f"/v1/policies/{pid}", headers={"Authorization": f"Bearer {DEV_KEY}"})
