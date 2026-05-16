"""HTTP-level tests for /v1/budgets endpoints.

Drives the real FastAPI app via TestClient. Each test creates a budget,
exercises the endpoint, and deletes the budget at the end so global DB
state stays clean.

Coverage:
  * POST creates cost + iteration budgets, returns 201
  * POST validation errors → 400
  * GET list / single
  * GET /{id}/spend returns live aggregation
  * PATCH partial update; scope/duration NOT patchable
  * DELETE returns 200, then 404
  * scope enforcement: budgets:read vs budgets:write
"""

from __future__ import annotations

import os
import sys
import uuid

import pytest

_RECEIVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _RECEIVER_DIR)


DEV_KEY = "stra_dev_local_default_project_do_not_use_in_production"


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
        json={"name": f"bud_{uuid.uuid4().hex[:6]}", "scopes": scopes},
    )
    assert r.status_code == 201, r.text
    return r.json()["key"]


def _delete_budget_rows(budget_ids: list[str]) -> None:
    if not budget_ids:
        return
    import psycopg
    conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
    try:
        for bid in budget_ids:
            conn.execute("DELETE FROM budgets WHERE id = %s", (bid,))
    finally:
        conn.close()


# ---- POST /v1/budgets ------------------------------------------------


def test_create_cost_budget(client):
    r = client.post(
        "/v1/budgets",
        headers=_auth(DEV_KEY),
        json={
            "name": "monthly cap",
            "scope": "project",
            "max_spend_usd": "100",
            "budget_duration": "30d",
        },
    )
    assert r.status_code == 201, r.text
    b = r.json()["budget"]
    try:
        from decimal import Decimal
        assert b["name"] == "monthly cap"
        assert b["scope"] == "project"
        assert Decimal(b["max_spend_usd"]) == Decimal("100")
        assert b["budget_duration"] == "30d"
        assert b["budget_reset_at"] is not None
    finally:
        _delete_budget_rows([b["id"]])


def test_create_iteration_budget(client):
    r = client.post(
        "/v1/budgets",
        headers=_auth(DEV_KEY),
        json={
            "name": "loop guard",
            "scope": "agent",
            "scope_value": "agent-x",
            "max_repeated_calls": 50,
            "loop_window_seconds": "60",
        },
    )
    assert r.status_code == 201, r.text
    b = r.json()["budget"]
    try:
        from decimal import Decimal
        assert b["max_repeated_calls"] == 50
        assert Decimal(b["loop_window_seconds"]) == Decimal("60")
    finally:
        _delete_budget_rows([b["id"]])


def test_create_rejects_invalid_combos(client):
    # Both cost AND iteration
    r = client.post(
        "/v1/budgets",
        headers=_auth(DEV_KEY),
        json={
            "name": "bad",
            "scope": "project",
            "max_spend_usd": "10",
            "budget_duration": "1d",
            "max_repeated_calls": 5,
            "loop_window_seconds": "30",
        },
    )
    assert r.status_code == 400
    assert "cannot be both" in r.json()["detail"]


def test_create_rejects_bad_duration(client):
    r = client.post(
        "/v1/budgets",
        headers=_auth(DEV_KEY),
        json={
            "name": "bad",
            "scope": "project",
            "max_spend_usd": "10",
            "budget_duration": "2w",
        },
    )
    assert r.status_code == 400


# ---- GET endpoints --------------------------------------------------


def test_list_budgets(client):
    r1 = client.post(
        "/v1/budgets", headers=_auth(DEV_KEY),
        json={"name": "list-test", "scope": "project",
              "max_spend_usd": "10", "budget_duration": "1d"},
    )
    bid = r1.json()["budget"]["id"]
    try:
        r = client.get("/v1/budgets", headers=_auth(DEV_KEY))
        assert r.status_code == 200
        names = [b["name"] for b in r.json()["budgets"]]
        assert "list-test" in names
    finally:
        _delete_budget_rows([bid])


def test_get_single_budget(client):
    r1 = client.post(
        "/v1/budgets", headers=_auth(DEV_KEY),
        json={"name": "single-test", "scope": "project",
              "max_spend_usd": "10", "budget_duration": "1d"},
    )
    bid = r1.json()["budget"]["id"]
    try:
        r = client.get(f"/v1/budgets/{bid}", headers=_auth(DEV_KEY))
        assert r.status_code == 200
        assert r.json()["budget"]["id"] == bid
    finally:
        _delete_budget_rows([bid])


def test_get_nonexistent_returns_404(client):
    fake = str(uuid.uuid4())
    r = client.get(f"/v1/budgets/{fake}", headers=_auth(DEV_KEY))
    assert r.status_code == 404


def test_get_spend_returns_live_aggregation(client):
    r1 = client.post(
        "/v1/budgets", headers=_auth(DEV_KEY),
        json={"name": "spend-test", "scope": "project",
              "max_spend_usd": "100", "budget_duration": "1d"},
    )
    bid = r1.json()["budget"]["id"]
    try:
        from decimal import Decimal
        r = client.get(f"/v1/budgets/{bid}/spend", headers=_auth(DEV_KEY))
        assert r.status_code == 200
        body = r.json()
        assert body["kind"] == "cost"
        assert Decimal(body["spent_usd"]) == Decimal("0")  # no spans yet
        assert Decimal(body["max_spend_usd"]) == Decimal("100")
        assert "window_start" in body
    finally:
        _delete_budget_rows([bid])


def test_get_iteration_budget_spend(client):
    r1 = client.post(
        "/v1/budgets", headers=_auth(DEV_KEY),
        json={"name": "iter-spend", "scope": "agent",
              "scope_value": "a",
              "max_repeated_calls": 10, "loop_window_seconds": "60"},
    )
    bid = r1.json()["budget"]["id"]
    try:
        r = client.get(f"/v1/budgets/{bid}/spend", headers=_auth(DEV_KEY))
        assert r.status_code == 200
        body = r.json()
        assert body["kind"] == "iteration"
        assert body["count"] == 0
        assert body["max_repeated_calls"] == 10
    finally:
        _delete_budget_rows([bid])


# ---- PATCH ----------------------------------------------------------


def test_patch_budget_name(client):
    r1 = client.post(
        "/v1/budgets", headers=_auth(DEV_KEY),
        json={"name": "before", "scope": "project",
              "max_spend_usd": "10", "budget_duration": "1d"},
    )
    bid = r1.json()["budget"]["id"]
    try:
        r = client.patch(
            f"/v1/budgets/{bid}",
            headers=_auth(DEV_KEY),
            json={"name": "after"},
        )
        assert r.status_code == 200
        assert r.json()["budget"]["name"] == "after"
    finally:
        _delete_budget_rows([bid])


def test_patch_cannot_set_iteration_on_cost_budget(client):
    r1 = client.post(
        "/v1/budgets", headers=_auth(DEV_KEY),
        json={"name": "cost", "scope": "project",
              "max_spend_usd": "10", "budget_duration": "1d"},
    )
    bid = r1.json()["budget"]["id"]
    try:
        r = client.patch(
            f"/v1/budgets/{bid}",
            headers=_auth(DEV_KEY),
            json={"max_repeated_calls": 5},
        )
        assert r.status_code == 400
    finally:
        _delete_budget_rows([bid])


# ---- DELETE ---------------------------------------------------------


def test_delete_budget(client):
    r1 = client.post(
        "/v1/budgets", headers=_auth(DEV_KEY),
        json={"name": "to-delete", "scope": "project",
              "max_spend_usd": "10", "budget_duration": "1d"},
    )
    bid = r1.json()["budget"]["id"]

    r = client.delete(f"/v1/budgets/{bid}", headers=_auth(DEV_KEY))
    assert r.status_code == 200

    # Second delete is 404
    r2 = client.delete(f"/v1/budgets/{bid}", headers=_auth(DEV_KEY))
    assert r2.status_code == 404


# ---- Scope enforcement ----------------------------------------------


def test_read_scope_cannot_create(client):
    read_key = _create_api_key(client, ["budgets:read"])
    r = client.post(
        "/v1/budgets",
        headers=_auth(read_key),
        json={"name": "x", "scope": "project",
              "max_spend_usd": "10", "budget_duration": "1d"},
    )
    assert r.status_code == 403


def test_write_scope_can_create(client):
    write_key = _create_api_key(client, ["budgets:write"])
    r = client.post(
        "/v1/budgets",
        headers=_auth(write_key),
        json={"name": "scope-write-test", "scope": "project",
              "max_spend_usd": "10", "budget_duration": "1d"},
    )
    assert r.status_code == 201
    _delete_budget_rows([r.json()["budget"]["id"]])


def test_no_scope_cannot_read(client):
    other_key = _create_api_key(client, ["traces:write"])  # unrelated scope
    r = client.get("/v1/budgets", headers=_auth(other_key))
    assert r.status_code == 403
