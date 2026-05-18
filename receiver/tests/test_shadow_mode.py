"""Tests for shadow policy mode."""

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


def _create_policy(client, name=None, shadow=False, action="block"):
    payload = {
        "name": name or f"pol-{uuid.uuid4().hex[:8]}",
        "match_expression": "true",
        "action": action,
        "shadow": shadow,
    }
    resp = client.post("/v1/policies", headers=_auth(DEV_KEY), json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _cleanup(client, policy_id):
    client.delete(f"/v1/policies/{policy_id}", headers=_auth(DEV_KEY))


def test_create_policy_with_shadow_true(client):
    p = _create_policy(client, shadow=True)
    try:
        assert p["shadow"] is True
    finally:
        _cleanup(client, p["id"])


def test_create_policy_shadow_defaults_false(client):
    p = _create_policy(client, shadow=False)
    try:
        assert p["shadow"] is False
    finally:
        _cleanup(client, p["id"])


def test_patch_policy_shadow(client):
    p = _create_policy(client, shadow=False)
    try:
        resp = client.patch(
            f"/v1/policies/{p['id']}",
            headers=_auth(DEV_KEY),
            json={"shadow": True},
        )
        assert resp.status_code == 200
        assert resp.json()["shadow"] is True
    finally:
        _cleanup(client, p["id"])


def test_shadow_stats_endpoint(client):
    p = _create_policy(client, shadow=True)
    try:
        resp = client.get(
            f"/v1/policies/{p['id']}/shadow-stats",
            headers=_auth(DEV_KEY),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["shadow"] is True
        assert data["match_count"] == 0
        assert data["last_matched_at"] is None
        assert data["policy_id"] == p["id"]
    finally:
        _cleanup(client, p["id"])


def test_shadow_stats_nonshadow_policy(client):
    p = _create_policy(client, shadow=False)
    try:
        resp = client.get(
            f"/v1/policies/{p['id']}/shadow-stats",
            headers=_auth(DEV_KEY),
        )
        assert resp.status_code == 200
        assert resp.json()["shadow"] is False
    finally:
        _cleanup(client, p["id"])


def test_shadow_stats_404(client):
    fake = str(uuid.uuid4())
    resp = client.get(
        f"/v1/policies/{fake}/shadow-stats",
        headers=_auth(DEV_KEY),
    )
    assert resp.status_code == 404


def test_export_includes_shadow_field(client):
    p = _create_policy(client, shadow=True)
    try:
        resp = client.get("/v1/policies/export", headers=_auth(DEV_KEY))
        assert resp.status_code == 200
        exported = next(
            (i for i in resp.json()["policies"] if i["name"] == p["name"]),
            None,
        )
        assert exported is not None
        assert "shadow" in exported
        assert exported["shadow"] is True
    finally:
        _cleanup(client, p["id"])
