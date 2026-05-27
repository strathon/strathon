"""HTTP-level tests for key rotation and expiration.

Drives the real FastAPI app via TestClient, matching the exact patterns
from test_halts_api.py and test_api_key_scopes.py.

API-level tests:
  - POST /v1/api_keys with expires_at stores the expiry
  - POST /v1/api_keys/{id}/rotate creates replacement + deprecates old
  - Rotation of already-deprecated key returns 404
  - Rotation of revoked key returns 404
  - PATCH /v1/api_keys/{id} updates name and expires_at
  - PATCH with empty body returns 400
  - PATCH on revoked key returns 404

Repository-level tests:
  - rotate_api_key returns new key + marks old deprecated
  - reap_expired_keys revokes expired keys
  - find_keys_expiring_soon returns keys within warning window
  - verify_token_and_touch rejects expired keys
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

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


def _create_key(client, name=None, expires_at=None):
    """Create a key via the API using the dev key. Returns full response dict."""
    payload = {"name": name or f"test-{uuid.uuid4().hex[:8]}"}
    if expires_at is not None:
        payload["expires_at"] = expires_at
    resp = client.post("/v1/api_keys", headers=_auth(DEV_KEY), json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Rotation (API-level)
# ---------------------------------------------------------------------------


def test_rotate_creates_new_key_and_deprecates_old(client):
    original = _create_key(client)
    original_id = original["id"]

    resp = client.post(
        f"/v1/api_keys/{original_id}/rotate",
        json={"grace_period_hours": 48},
        headers=_auth(DEV_KEY),
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()

    # New key returned with raw value.
    assert "key" in data
    assert data["rotated_from_id"] == original_id
    assert "(rotated)" in data["name"]

    # Old key should now be deprecated.
    resp2 = client.get(
        "/v1/api_keys",
        headers=_auth(DEV_KEY),
        params={"include_revoked": "true"},
    )
    keys = resp2.json()["api_keys"]
    old = next((k for k in keys if k["id"] == original_id), None)
    assert old is not None
    assert old["deprecated_at"] is not None
    assert old["expires_at"] is not None


def test_rotate_already_deprecated_returns_404(client):
    original = _create_key(client)
    original_id = original["id"]

    resp = client.post(
        f"/v1/api_keys/{original_id}/rotate",
        json={},
        headers=_auth(DEV_KEY),
    )
    assert resp.status_code == 201

    resp2 = client.post(
        f"/v1/api_keys/{original_id}/rotate",
        json={},
        headers=_auth(DEV_KEY),
    )
    assert resp2.status_code == 404


def test_rotate_revoked_returns_404(client):
    original = _create_key(client)
    original_id = original["id"]

    client.delete(f"/v1/api_keys/{original_id}", headers=_auth(DEV_KEY))

    resp = client.post(
        f"/v1/api_keys/{original_id}/rotate",
        json={},
        headers=_auth(DEV_KEY),
    )
    assert resp.status_code == 404


def test_rotate_nonexistent_returns_404(client):
    fake_id = str(uuid.uuid4())
    resp = client.post(
        f"/v1/api_keys/{fake_id}/rotate",
        json={},
        headers=_auth(DEV_KEY),
    )
    assert resp.status_code == 404


def test_rotate_invalid_id_returns_400(client):
    resp = client.post(
        "/v1/api_keys/not-a-uuid/rotate",
        json={},
        headers=_auth(DEV_KEY),
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Expiration (API-level)
# ---------------------------------------------------------------------------


def test_create_key_with_expires_at(client):
    future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    data = _create_key(client, expires_at=future)
    assert data["expires_at"] is not None


def test_create_key_without_expires_at_has_none(client):
    data = _create_key(client)
    assert data.get("expires_at") is None


# ---------------------------------------------------------------------------
# PATCH (API-level)
# ---------------------------------------------------------------------------


def test_patch_updates_name_and_expires_at(client):
    original = _create_key(client)
    original_id = original["id"]

    future = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    resp = client.patch(
        f"/v1/api_keys/{original_id}",
        json={"name": "new-name", "expires_at": future},
        headers=_auth(DEV_KEY),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["name"] == "new-name"
    assert data["expires_at"] is not None


def test_patch_revoked_key_returns_404(client):
    original = _create_key(client)
    client.delete(f"/v1/api_keys/{original['id']}", headers=_auth(DEV_KEY))

    resp = client.patch(
        f"/v1/api_keys/{original['id']}",
        json={"name": "nope"},
        headers=_auth(DEV_KEY),
    )
    assert resp.status_code == 404


def test_patch_empty_body_returns_400(client):
    original = _create_key(client)
    resp = client.patch(
        f"/v1/api_keys/{original['id']}",
        json={},
        headers=_auth(DEV_KEY),
    )
    assert resp.status_code == 400


def test_patch_nonexistent_returns_404(client):
    fake_id = str(uuid.uuid4())
    resp = client.patch(
        f"/v1/api_keys/{fake_id}",
        json={"name": "nope"},
        headers=_auth(DEV_KEY),
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Repository-level: rotation
# ---------------------------------------------------------------------------


async def test_rotate_api_key_returns_new_key(session, isolated_project):
    from repositories.auth import create_api_key, rotate_api_key

    original = await create_api_key(session, isolated_project, name="orig")

    result = await rotate_api_key(session, original.api_key.id, grace_period_hours=24)
    assert result is not None
    assert result.raw_key.startswith("stra_")
    assert result.api_key.rotated_from_id == original.api_key.id
    assert result.api_key.name == "orig (rotated)"


async def test_rotate_sets_deprecated_and_expires(session, isolated_project):
    from repositories.auth import create_api_key, rotate_api_key
    from models import ApiKey
    from sqlalchemy import select

    original = await create_api_key(session, isolated_project, name="dep")
    await rotate_api_key(session, original.api_key.id, grace_period_hours=48)

    stmt = select(ApiKey).where(ApiKey.id == original.api_key.id)
    row = (await session.execute(stmt)).scalar_one()
    assert row.deprecated_at is not None
    assert row.expires_at is not None
    # expires_at should be roughly 48h from now.
    delta = row.expires_at - row.deprecated_at
    assert timedelta(hours=47) < delta < timedelta(hours=49)


async def test_rotate_deprecated_key_returns_none(session, isolated_project):
    from repositories.auth import create_api_key, rotate_api_key

    original = await create_api_key(session, isolated_project, name="dep2")
    await rotate_api_key(session, original.api_key.id)

    result = await rotate_api_key(session, original.api_key.id)
    assert result is None


# ---------------------------------------------------------------------------
# Repository-level: expiration + reaper
# ---------------------------------------------------------------------------


async def test_verify_rejects_expired_key(session, isolated_project):
    from repositories.auth import create_api_key, verify_token_and_touch

    past = datetime.now(timezone.utc) - timedelta(hours=1)
    resp = await create_api_key(
        session, isolated_project, name="expired", expires_at=past,
    )
    # The raw key should NOT authenticate because it's already expired.
    result = await verify_token_and_touch(session, resp.raw_key)
    assert result is None


async def test_verify_accepts_non_expired_key(session, isolated_project):
    from repositories.auth import create_api_key, verify_token_and_touch

    future = datetime.now(timezone.utc) + timedelta(hours=24)
    resp = await create_api_key(
        session, isolated_project, name="valid", expires_at=future,
    )
    result = await verify_token_and_touch(session, resp.raw_key)
    assert result is not None


async def test_reap_expired_keys_revokes(session, isolated_project):
    from repositories.auth import create_api_key, reap_expired_keys
    from models import ApiKey
    from sqlalchemy import select

    past = datetime.now(timezone.utc) - timedelta(hours=1)
    resp = await create_api_key(
        session, isolated_project, name="reap-me", expires_at=past,
    )
    count = await reap_expired_keys(session)
    assert count >= 1

    stmt = select(ApiKey).where(ApiKey.id == resp.api_key.id)
    row = (await session.execute(stmt)).scalar_one()
    assert row.revoked_at is not None


async def test_find_keys_expiring_soon_returns_within_window(
    session, isolated_project,
):
    from repositories.auth import create_api_key, find_keys_expiring_soon

    # Key expiring in 12 hours (within 24h window).
    soon = datetime.now(timezone.utc) + timedelta(hours=12)
    await create_api_key(
        session, isolated_project, name="soon", expires_at=soon,
    )

    # Key expiring in 48 hours (outside 24h window).
    later = datetime.now(timezone.utc) + timedelta(hours=48)
    await create_api_key(
        session, isolated_project, name="later", expires_at=later,
    )

    results = await find_keys_expiring_soon(session, within_hours=24)
    names = [r.name for r in results]
    assert "soon" in names
    assert "later" not in names
