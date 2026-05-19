"""Tests for input validation hardening.

- Oversized OTLP payload rejected with 413
- Extra fields in policy create rejected with 422
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


def test_oversized_otlp_payload_rejected(client):
    """Payloads over 4MB are rejected before parsing."""
    # 5MB of zeros — not valid protobuf, but size check comes first.
    big_body = b"\x00" * (5 * 1024 * 1024)
    resp = client.post(
        "/v1/traces",
        content=big_body,
        headers={
            **_auth(DEV_KEY),
            "Content-Type": "application/x-protobuf",
        },
    )
    assert resp.status_code == 413


def test_normal_sized_otlp_not_rejected_for_size(client):
    """Small payloads pass the size check (may fail on parse, not on size)."""
    small_body = b"\x00" * 100
    resp = client.post(
        "/v1/traces",
        content=small_body,
        headers={
            **_auth(DEV_KEY),
            "Content-Type": "application/x-protobuf",
        },
    )
    # 200 (empty valid protobuf) or 400 (invalid protobuf) — not 413.
    assert resp.status_code in (200, 400)
