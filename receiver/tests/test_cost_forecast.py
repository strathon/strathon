"""Tests for cost forecasting endpoint."""

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


def test_forecast_returns_200(client):
    resp = client.get("/v1/costs/forecast", headers=_auth(DEV_KEY))
    assert resp.status_code == 200
    data = resp.json()
    assert "burn_rate_usd_per_hour" in data
    assert "projected_daily_cost" in data
    assert "projected_weekly_cost" in data
    assert "projected_monthly_cost" in data
    assert "budget_forecasts" in data
    assert "budget_alerts" in data


def test_forecast_burn_rate_non_negative(client):
    resp = client.get("/v1/costs/forecast", headers=_auth(DEV_KEY))
    data = resp.json()
    assert data["burn_rate_usd_per_hour"] >= 0
    assert data["projected_daily_cost"] >= 0


def test_forecast_accepts_threshold_param(client):
    resp = client.get(
        "/v1/costs/forecast",
        headers=_auth(DEV_KEY),
        params={"alert_threshold_hours": 48},
    )
    assert resp.status_code == 200
    assert resp.json()["alert_threshold_hours"] == 48


def test_forecast_requires_auth(client):
    resp = client.get("/v1/costs/forecast")
    assert resp.status_code == 401


def test_forecast_budget_forecast_shape(client):
    """Budget forecasts have the expected fields."""
    resp = client.get("/v1/costs/forecast", headers=_auth(DEV_KEY))
    data = resp.json()
    for bf in data["budget_forecasts"]:
        assert "budget_id" in bf
        assert "budget_name" in bf
        assert "max_spend_usd" in bf
        assert "current_spend_usd" in bf
        assert "remaining_usd" in bf
        assert "budget_alert" in bf
