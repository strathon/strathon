"""HTTP-level tests for /v1/model_prices endpoints.

Covers POST (upsert), GET (list), DELETE, and scope enforcement.
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
        json={"name": f"prices_{uuid.uuid4().hex[:6]}", "scopes": scopes},
    )
    assert r.status_code == 201, r.text
    return r.json()["key"]


def _cleanup_overrides(model_names: list[str]) -> None:
    """Delete test-created override rows so tests stay isolated."""
    import psycopg
    conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
    try:
        for name in model_names:
            conn.execute(
                "DELETE FROM model_price_overrides WHERE model_name = %s",
                (name,),
            )
    finally:
        conn.close()


# ---- POST -----------------------------------------------------------


def test_upsert_creates(client):
    model = f"test-model-{uuid.uuid4().hex[:6]}"
    try:
        r = client.post(
            "/v1/model_prices",
            headers=_auth(DEV_KEY),
            json={
                "model_name": model,
                "input_cost_per_token": "0.000001",
                "output_cost_per_token": "0.000002",
            },
        )
        assert r.status_code == 200, r.text
        ov = r.json()["override"]
        from decimal import Decimal
        assert ov["model_name"] == model
        assert Decimal(ov["input_cost_per_token"]) == Decimal("0.000001")
    finally:
        _cleanup_overrides([model])


def test_upsert_is_idempotent(client):
    """POST twice with the same model → second updates, not 409."""
    model = f"test-model-{uuid.uuid4().hex[:6]}"
    try:
        r1 = client.post(
            "/v1/model_prices",
            headers=_auth(DEV_KEY),
            json={
                "model_name": model,
                "input_cost_per_token": "0.000001",
                "output_cost_per_token": "0.000001",
            },
        )
        assert r1.status_code == 200
        r2 = client.post(
            "/v1/model_prices",
            headers=_auth(DEV_KEY),
            json={
                "model_name": model,
                "input_cost_per_token": "0.000010",
                "output_cost_per_token": "0.000010",
            },
        )
        assert r2.status_code == 200
        from decimal import Decimal
        assert Decimal(r2.json()["override"]["input_cost_per_token"]) == Decimal("0.000010")
    finally:
        _cleanup_overrides([model])


def test_upsert_rejects_negative(client):
    model = f"test-model-{uuid.uuid4().hex[:6]}"
    r = client.post(
        "/v1/model_prices",
        headers=_auth(DEV_KEY),
        json={
            "model_name": model,
            "input_cost_per_token": "-0.001",
            "output_cost_per_token": "0.001",
        },
    )
    assert r.status_code == 400


def test_upsert_rejects_bad_decimal(client):
    r = client.post(
        "/v1/model_prices",
        headers=_auth(DEV_KEY),
        json={
            "model_name": "some-model",
            "input_cost_per_token": "not-a-number",
            "output_cost_per_token": "0.001",
        },
    )
    assert r.status_code == 400


# ---- GET ------------------------------------------------------------


def test_list_overrides(client):
    model = f"test-model-{uuid.uuid4().hex[:6]}"
    try:
        client.post(
            "/v1/model_prices",
            headers=_auth(DEV_KEY),
            json={
                "model_name": model,
                "input_cost_per_token": "0.000001",
                "output_cost_per_token": "0.000001",
            },
        )
        r = client.get("/v1/model_prices", headers=_auth(DEV_KEY))
        assert r.status_code == 200
        models = [o["model_name"] for o in r.json()["overrides"]]
        assert model in models
    finally:
        _cleanup_overrides([model])


# ---- DELETE ---------------------------------------------------------


def test_delete_existing(client):
    model = f"test-model-{uuid.uuid4().hex[:6]}"
    client.post(
        "/v1/model_prices",
        headers=_auth(DEV_KEY),
        json={
            "model_name": model,
            "input_cost_per_token": "0.000001",
            "output_cost_per_token": "0.000001",
        },
    )
    r = client.delete(f"/v1/model_prices/{model}", headers=_auth(DEV_KEY))
    assert r.status_code == 200


def test_delete_nonexistent_returns_404(client):
    r = client.delete(
        f"/v1/model_prices/never-existed-{uuid.uuid4().hex[:6]}",
        headers=_auth(DEV_KEY),
    )
    assert r.status_code == 404


# ---- Scope enforcement ---------------------------------------------


def test_read_scope_cannot_write(client):
    read_key = _create_api_key(client, ["model_prices:read"])
    r = client.post(
        "/v1/model_prices",
        headers=_auth(read_key),
        json={
            "model_name": "x",
            "input_cost_per_token": "0.001",
            "output_cost_per_token": "0.001",
        },
    )
    assert r.status_code == 403


def test_unrelated_scope_cannot_read(client):
    other_key = _create_api_key(client, ["traces:write"])
    r = client.get("/v1/model_prices", headers=_auth(other_key))
    assert r.status_code == 403
