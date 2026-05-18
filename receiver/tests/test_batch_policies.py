"""Tests for batch policy operations."""

from __future__ import annotations

import os
import uuid

import pytest


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


def _create_policy(client, name=None):
    resp = client.post(
        "/v1/policies",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
        json={
            "name": name or f"batch-{uuid.uuid4().hex[:8]}",
            "match_expression": "true",
            "action": "log",
        },
    )
    assert resp.status_code == 201
    return resp.json()["id"]


def test_batch_disable(client):
    ids = [_create_policy(client) for _ in range(3)]
    resp = client.post(
        "/v1/policies/batch",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
        json={"action": "disable", "policy_ids": ids},
    )
    assert resp.status_code == 200
    assert resp.json()["affected"] == 3
    # Verify disabled.
    for pid in ids:
        r = client.get(f"/v1/policies/{pid}", headers={"Authorization": f"Bearer {DEV_KEY}"})
        assert r.json()["enabled"] is False
    # Cleanup.
    for pid in ids:
        client.delete(f"/v1/policies/{pid}", headers={"Authorization": f"Bearer {DEV_KEY}"})


def test_batch_enable(client):
    ids = [_create_policy(client) for _ in range(2)]
    # Disable first.
    client.post(
        "/v1/policies/batch",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
        json={"action": "disable", "policy_ids": ids},
    )
    # Re-enable.
    resp = client.post(
        "/v1/policies/batch",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
        json={"action": "enable", "policy_ids": ids},
    )
    assert resp.status_code == 200
    assert resp.json()["affected"] == 2
    for pid in ids:
        client.delete(f"/v1/policies/{pid}", headers={"Authorization": f"Bearer {DEV_KEY}"})


def test_batch_delete(client):
    ids = [_create_policy(client) for _ in range(3)]
    resp = client.post(
        "/v1/policies/batch",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
        json={"action": "delete", "policy_ids": ids},
    )
    assert resp.status_code == 200
    assert resp.json()["affected"] == 3
    # Verify gone.
    for pid in ids:
        r = client.get(f"/v1/policies/{pid}", headers={"Authorization": f"Bearer {DEV_KEY}"})
        assert r.status_code == 404


def test_batch_invalid_action(client):
    resp = client.post(
        "/v1/policies/batch",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
        json={"action": "explode", "policy_ids": [str(uuid.uuid4())]},
    )
    assert resp.status_code == 400


def test_batch_invalid_id(client):
    resp = client.post(
        "/v1/policies/batch",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
        json={"action": "disable", "policy_ids": ["not-a-uuid"]},
    )
    assert resp.status_code == 400


def test_batch_requires_write_scope(client):
    # Mint read-only key.
    r = client.post(
        "/v1/api_keys",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
        json={"name": f"ro-{uuid.uuid4().hex[:6]}", "scopes": ["policies:read"]},
    )
    key = r.json()["key"]
    resp = client.post(
        "/v1/policies/batch",
        headers={"Authorization": f"Bearer {key}"},
        json={"action": "disable", "policy_ids": [str(uuid.uuid4())]},
    )
    assert resp.status_code == 403
