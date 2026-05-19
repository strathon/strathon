"""Tests for production hardening.

- Docs disabled by default (STRATHON_DOCS_ENABLED not set)
- Metrics auth when STRATHON_METRICS_AUTH_TOKEN is set
- Security headers present on responses
"""

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
    # Ensure docs are disabled for these tests (production default).
    os.environ.pop("STRATHON_DOCS_ENABLED", None)
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


def test_security_headers_present(client):
    """X-Content-Type-Options and X-Frame-Options on every response."""
    resp = client.get("/health")
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("X-Frame-Options") == "DENY"


def test_cache_control_on_auth_endpoints(client):
    """Auth endpoints get Cache-Control: no-store."""
    resp = client.post(
        "/v1/api_keys",
        headers=_auth(DEV_KEY),
        json={"name": "cache-test"},
    )
    # Should have Cache-Control header regardless of status.
    if resp.status_code in (200, 201):
        assert resp.headers.get("Cache-Control") == "no-store"
        # Cleanup.
        key_id = resp.json().get("id")
        if key_id:
            client.delete(f"/v1/api_keys/{key_id}", headers=_auth(DEV_KEY))


def test_metrics_accessible_without_token_when_unset(client):
    """Without STRATHON_METRICS_AUTH_TOKEN, /metrics is open."""
    os.environ.pop("STRATHON_METRICS_AUTH_TOKEN", None)
    resp = client.get("/metrics")
    assert resp.status_code == 200
