"""HTTP-level tests for /v1/halts and /v1/intervention/sync.

Drives the real FastAPI app via TestClient. Each test cleans up its
own halt rows so the tests don't depend on global DB state.
"""

from __future__ import annotations

import os
import sys

import pytest

_RECEIVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _RECEIVER_DIR)


DEV_KEY = "stra_dev_local_default_project_do_not_use_in_production"
DEFAULT_PROJECT_ID = "00000000-0000-0000-0000-000000000001"


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


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _create_api_key(client, scopes: list[str]) -> str:
    r = client.post(
        "/v1/api_keys",
        headers=_auth(DEV_KEY),
        json={"name": f"halts_test_{','.join(scopes)}", "scopes": scopes},
    )
    assert r.status_code == 201, r.text
    return r.json()["key"]


def _delete_halt_rows(halt_ids: list[int]) -> None:
    if not halt_ids:
        return
    import psycopg
    db_url = os.environ["DATABASE_URL"]
    conn = psycopg.connect(db_url, autocommit=True)
    try:
        conn.execute(
            "DELETE FROM halt_state WHERE id = ANY(%s)",
            (halt_ids,),
        )
    finally:
        conn.close()


# ---- POST /v1/halts ---------------------------------------------------


def test_create_agent_halt_returns_201(client):
    r = client.post(
        "/v1/halts",
        headers=_auth(DEV_KEY),
        json={
            "scope": "agent",
            "scope_value": "agent-test-1",
            "reason": "create-agent test",
        },
    )
    assert r.status_code == 201, r.text
    halt = r.json()["halt"]
    assert halt["scope"] == "agent"
    assert halt["scope_value"] == "agent-test-1"
    assert halt["state"] == "halted"
    assert halt["cleared_at"] is None

    _delete_halt_rows([halt["id"]])


def test_create_project_halt_returns_201(client):
    r = client.post(
        "/v1/halts",
        headers=_auth(DEV_KEY),
        json={
            "scope": "project",
            "reason": "create-project test",
        },
    )
    assert r.status_code == 201, r.text
    halt = r.json()["halt"]
    assert halt["scope"] == "project"
    assert halt["scope_value"] is None

    _delete_halt_rows([halt["id"]])


def test_create_with_invalid_scope_returns_400(client):
    r = client.post(
        "/v1/halts",
        headers=_auth(DEV_KEY),
        json={"scope": "magic", "reason": "x"},
    )
    assert r.status_code == 400


def test_create_agent_without_value_returns_400(client):
    r = client.post(
        "/v1/halts",
        headers=_auth(DEV_KEY),
        json={"scope": "agent", "reason": "x"},
    )
    assert r.status_code == 400
    assert "scope_value" in r.json()["detail"]


def test_create_project_with_value_returns_400(client):
    r = client.post(
        "/v1/halts",
        headers=_auth(DEV_KEY),
        json={"scope": "project", "scope_value": "x", "reason": "x"},
    )
    assert r.status_code == 400


# ---- GET /v1/halts ----------------------------------------------------


def test_list_returns_created_halt(client):
    r1 = client.post(
        "/v1/halts",
        headers=_auth(DEV_KEY),
        json={"scope": "agent", "scope_value": "list-test-a",
              "reason": "list-test"},
    )
    halt_id = r1.json()["halt"]["id"]

    r2 = client.get("/v1/halts", headers=_auth(DEV_KEY))
    assert r2.status_code == 200
    halts = r2.json()["halts"]
    ids = [h["id"] for h in halts]
    assert halt_id in ids

    _delete_halt_rows([halt_id])


def test_list_excludes_cleared_by_default(client):
    r1 = client.post(
        "/v1/halts",
        headers=_auth(DEV_KEY),
        json={"scope": "agent", "scope_value": "exclude-test",
              "reason": "to be cleared"},
    )
    halt_id = r1.json()["halt"]["id"]
    client.delete(f"/v1/halts/{halt_id}", headers=_auth(DEV_KEY))

    r2 = client.get("/v1/halts", headers=_auth(DEV_KEY))
    ids = [h["id"] for h in r2.json()["halts"]]
    assert halt_id not in ids

    _delete_halt_rows([halt_id])


def test_list_include_cleared_shows_audit_trail(client):
    r1 = client.post(
        "/v1/halts",
        headers=_auth(DEV_KEY),
        json={"scope": "agent", "scope_value": "audit-test",
              "reason": "audit-test"},
    )
    halt_id = r1.json()["halt"]["id"]
    client.delete(f"/v1/halts/{halt_id}", headers=_auth(DEV_KEY))

    r2 = client.get(
        "/v1/halts?include_cleared=true",
        headers=_auth(DEV_KEY),
    )
    ids = [h["id"] for h in r2.json()["halts"]]
    assert halt_id in ids

    _delete_halt_rows([halt_id])


# ---- GET /v1/halts/{id} ----------------------------------------------


def test_get_single_halt_returns_dto(client):
    r1 = client.post(
        "/v1/halts",
        headers=_auth(DEV_KEY),
        json={"scope": "agent", "scope_value": "single-test",
              "reason": "single-test"},
    )
    halt_id = r1.json()["halt"]["id"]

    r2 = client.get(f"/v1/halts/{halt_id}", headers=_auth(DEV_KEY))
    assert r2.status_code == 200
    assert r2.json()["halt"]["id"] == halt_id

    _delete_halt_rows([halt_id])


def test_get_unknown_halt_returns_404(client):
    r = client.get("/v1/halts/999999999", headers=_auth(DEV_KEY))
    assert r.status_code == 404


# ---- DELETE /v1/halts/{id} -------------------------------------------


def test_delete_halt_marks_cleared(client):
    r1 = client.post(
        "/v1/halts",
        headers=_auth(DEV_KEY),
        json={"scope": "agent", "scope_value": "delete-test",
              "reason": "delete-test"},
    )
    halt_id = r1.json()["halt"]["id"]

    r2 = client.delete(f"/v1/halts/{halt_id}", headers=_auth(DEV_KEY))
    assert r2.status_code == 200
    assert r2.json()["halt"]["cleared_at"] is not None

    _delete_halt_rows([halt_id])


def test_delete_already_cleared_returns_409(client):
    r1 = client.post(
        "/v1/halts",
        headers=_auth(DEV_KEY),
        json={"scope": "agent", "scope_value": "double-delete",
              "reason": "double-delete"},
    )
    halt_id = r1.json()["halt"]["id"]
    client.delete(f"/v1/halts/{halt_id}", headers=_auth(DEV_KEY))

    r2 = client.delete(f"/v1/halts/{halt_id}", headers=_auth(DEV_KEY))
    assert r2.status_code == 409
    assert "already cleared" in r2.json()["detail"]

    _delete_halt_rows([halt_id])


def test_delete_unknown_halt_returns_404(client):
    r = client.delete("/v1/halts/999999999", headers=_auth(DEV_KEY))
    assert r.status_code == 404


# ---- POST /v1/intervention/sync --------------------------------------


def test_sync_returns_active_halts(client):
    r1 = client.post(
        "/v1/halts",
        headers=_auth(DEV_KEY),
        json={"scope": "agent", "scope_value": "sync-test",
              "reason": "sync-test"},
    )
    halt_id = r1.json()["halt"]["id"]

    r2 = client.post(
        "/v1/intervention/sync",
        headers=_auth(DEV_KEY),
        json={},
    )
    assert r2.status_code == 200
    body = r2.json()
    assert "halts" in body
    assert "budgets" in body
    assert "synced_at_unix_nano" in body
    assert isinstance(body["budgets"], list)
    halt_ids = [h["id"] for h in body["halts"]]
    assert halt_id in halt_ids

    # Compact SDK shape — only the 5 fields the SDK needs
    matching = [h for h in body["halts"] if h["id"] == halt_id][0]
    assert set(matching.keys()) == {"id", "scope", "scope_value", "state", "reason"}

    _delete_halt_rows([halt_id])


def test_sync_returns_empty_when_no_halts(client):
    """Baseline sanity: a sync against a project with no active halts
    returns an empty halts list (not an error)."""
    # Clear any leftover halts in the default project first
    import psycopg
    db_url = os.environ["DATABASE_URL"]
    conn = psycopg.connect(db_url, autocommit=True)
    try:
        conn.execute(
            "DELETE FROM halt_state WHERE project_id = %s::uuid",
            (DEFAULT_PROJECT_ID,),
        )
    finally:
        conn.close()

    r = client.post(
        "/v1/intervention/sync",
        headers=_auth(DEV_KEY),
        json={},
    )
    assert r.status_code == 200
    assert r.json()["halts"] == []


# ---- Scope enforcement -----------------------------------------------


def test_list_requires_read_scope(client):
    narrow = _create_api_key(client, ["traces:write"])
    r = client.get("/v1/halts", headers=_auth(narrow))
    assert r.status_code == 403


def test_list_works_with_halts_read(client):
    narrow = _create_api_key(client, ["halts:read"])
    r = client.get("/v1/halts", headers=_auth(narrow))
    assert r.status_code == 200


def test_create_requires_write_scope(client):
    narrow = _create_api_key(client, ["halts:read"])
    r = client.post(
        "/v1/halts",
        headers=_auth(narrow),
        json={"scope": "agent", "scope_value": "x", "reason": "x"},
    )
    assert r.status_code == 403


def test_create_works_with_write_scope(client):
    narrow = _create_api_key(client, ["halts:read", "halts:write"])
    r = client.post(
        "/v1/halts",
        headers=_auth(narrow),
        json={"scope": "agent", "scope_value": "scope-write",
              "reason": "scope-write test"},
    )
    assert r.status_code == 201
    _delete_halt_rows([r.json()["halt"]["id"]])


def test_sync_requires_read_scope(client):
    """The SDK polls sync; it needs halts:read (NOT traces:write)."""
    narrow = _create_api_key(client, ["traces:write"])
    r = client.post(
        "/v1/intervention/sync",
        headers=_auth(narrow),
        json={},
    )
    assert r.status_code == 403


def test_sync_works_with_halts_read(client):
    narrow = _create_api_key(client, ["halts:read"])
    r = client.post(
        "/v1/intervention/sync",
        headers=_auth(narrow),
        json={},
    )
    assert r.status_code == 200


def test_no_authorization_returns_401(client):
    r = client.get("/v1/halts")
    assert r.status_code == 401
