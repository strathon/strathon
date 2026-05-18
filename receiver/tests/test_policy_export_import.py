"""HTTP-level tests for policy export and import.

Drives the real FastAPI app via TestClient. Follows the same pattern
as test_halts_api.py and test_api_key_scopes.py.

Export/import is the staging -> prod promotion workflow:
  1. GET /v1/policies/export on staging -> JSON array
  2. POST /v1/policies/import on prod -> bulk create
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


def _create_policy(client, name=None, expression="true", action="log"):
    payload = {
        "name": name or f"pol-{uuid.uuid4().hex[:8]}",
        "match_expression": expression,
        "action": action,
    }
    resp = client.post("/v1/policies", headers=_auth(DEV_KEY), json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _cleanup_policy(client, policy_id):
    client.delete(f"/v1/policies/{policy_id}", headers=_auth(DEV_KEY))


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def test_export_returns_policies_array(client):
    p = _create_policy(client, name=f"export-{uuid.uuid4().hex[:8]}")
    try:
        resp = client.get("/v1/policies/export", headers=_auth(DEV_KEY))
        assert resp.status_code == 200
        data = resp.json()
        assert "policies" in data
        assert "count" in data
        assert isinstance(data["policies"], list)
        assert data["count"] == len(data["policies"])
        assert data["count"] >= 1
    finally:
        _cleanup_policy(client, p["id"])


def test_export_excludes_id_and_project_id(client):
    p = _create_policy(client, name=f"export-no-id-{uuid.uuid4().hex[:8]}")
    try:
        resp = client.get("/v1/policies/export", headers=_auth(DEV_KEY))
        data = resp.json()
        for item in data["policies"]:
            assert "id" not in item
            assert "project_id" not in item
            assert "created_at" not in item
            assert "updated_at" not in item
    finally:
        _cleanup_policy(client, p["id"])


def test_export_contains_portable_fields(client):
    name = f"export-fields-{uuid.uuid4().hex[:8]}"
    p = _create_policy(client, name=name, expression='span.tool.name == "test"')
    try:
        resp = client.get("/v1/policies/export", headers=_auth(DEV_KEY))
        data = resp.json()
        exported = next((i for i in data["policies"] if i["name"] == name), None)
        assert exported is not None
        assert exported["match_expression"] == 'span.tool.name == "test"'
        assert exported["action"] == "log"
        assert "enabled" in exported
        assert "priority" in exported
    finally:
        _cleanup_policy(client, p["id"])


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def test_import_creates_policies(client):
    name = f"import-{uuid.uuid4().hex[:8]}"
    payload = {
        "policies": [
            {
                "name": name,
                "match_expression": "true",
                "action": "log",
            }
        ]
    }
    resp = client.post(
        "/v1/policies/import", headers=_auth(DEV_KEY), json=payload,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["created"] == 1
    assert data["skipped"] == 0
    assert data["errors"] == []

    # Cleanup.
    policies = client.get("/v1/policies", headers=_auth(DEV_KEY)).json()
    for p in policies["policies"]:
        if p["name"] == name:
            _cleanup_policy(client, p["id"])


def test_import_skips_duplicates(client):
    name = f"dup-{uuid.uuid4().hex[:8]}"
    p = _create_policy(client, name=name, expression="true")
    try:
        payload = {
            "policies": [
                {"name": name, "match_expression": "true", "action": "log"}
            ]
        }
        resp = client.post(
            "/v1/policies/import", headers=_auth(DEV_KEY), json=payload,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["created"] == 0
        assert data["skipped"] == 1
    finally:
        _cleanup_policy(client, p["id"])


def test_import_reports_validation_errors(client):
    payload = {
        "policies": [
            {"name": "", "match_expression": "true", "action": "log"},
        ]
    }
    resp = client.post(
        "/v1/policies/import", headers=_auth(DEV_KEY), json=payload,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["created"] == 0
    assert len(data["errors"]) == 1


def test_import_reports_invalid_cel(client):
    payload = {
        "policies": [
            {
                "name": f"badcel-{uuid.uuid4().hex[:8]}",
                "match_expression": "this is not valid CEL !!!",
                "action": "block",
            }
        ]
    }
    resp = client.post(
        "/v1/policies/import", headers=_auth(DEV_KEY), json=payload,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["created"] == 0
    assert len(data["errors"]) == 1


def test_import_rejects_missing_policies_array(client):
    resp = client.post(
        "/v1/policies/import", headers=_auth(DEV_KEY), json={"wrong": []},
    )
    assert resp.status_code == 400


def test_export_import_round_trip(client):
    """Export from source, import to same project. Second import skips all."""
    name = f"roundtrip-{uuid.uuid4().hex[:8]}"
    p = _create_policy(client, name=name, expression='span.agent.name == "x"')
    try:
        # Export.
        export_resp = client.get("/v1/policies/export", headers=_auth(DEV_KEY))
        assert export_resp.status_code == 200
        export_data = export_resp.json()

        # Import same data.
        import_resp = client.post(
            "/v1/policies/import",
            headers=_auth(DEV_KEY),
            json=export_data,
        )
        assert import_resp.status_code == 200
        data = import_resp.json()
        # All should be skipped since they already exist.
        assert data["skipped"] >= 1
        assert data["created"] == 0
    finally:
        _cleanup_policy(client, p["id"])


def test_import_multiple_policies(client):
    names = [f"multi-{uuid.uuid4().hex[:8]}" for _ in range(3)]
    payload = {
        "policies": [
            {"name": n, "match_expression": "true", "action": "log"}
            for n in names
        ]
    }
    resp = client.post(
        "/v1/policies/import", headers=_auth(DEV_KEY), json=payload,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["created"] == 3

    # Cleanup.
    policies = client.get("/v1/policies", headers=_auth(DEV_KEY)).json()
    for p in policies["policies"]:
        if p["name"] in names:
            _cleanup_policy(client, p["id"])
