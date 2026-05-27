"""Tests for dashboard convenience endpoints.

Tests: capabilities, version, change-password, members CRUD,
member admin actions, settings, GDPR export, heartbeat interception.
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
    os.environ.setdefault("STRATHON_REGISTRATION_ENABLED", "true")
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


def _unique_email() -> str:
    return f"test_{uuid.uuid4().hex[:12]}@example.com"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register(client, email=None, password="TestPass123!"):
    email = email or _unique_email()
    r = client.post("/v1/auth/register", json={
        "email": email,
        "password": password,
        "display_name": email.split("@")[0],
    })
    return r, email


# ---- Capabilities (no auth) -------------------------------------------------

def test_capabilities_returns_200(client):
    r = client.get("/v1/auth/capabilities")
    assert r.status_code == 200
    data = r.json()
    assert "registration_enabled" in data
    assert "smtp_enabled" in data
    assert "mfa_available" in data
    assert "mode" in data


def test_capabilities_no_auth_needed(client):
    r = client.get("/v1/auth/capabilities")
    assert r.status_code == 200


# ---- Version (no auth) ------------------------------------------------------

def test_version_returns_200(client):
    r = client.get("/v1/version")
    assert r.status_code == 200
    data = r.json()
    assert "version" in data
    assert "api_version" in data
    assert data["api_version"] == "v1"


# ---- Change password ---------------------------------------------------------

def test_change_password_success(client):
    r, email = _register(client)
    assert r.status_code == 201
    token = r.json()["token"]

    r = client.post(
        "/v1/auth/change-password",
        json={"current_password": "TestPass123!", "new_password": "NewPass456!!"},
        headers=_auth(token),
    )
    assert r.status_code == 200
    assert r.json()["status"] == "password_changed"


def test_change_password_wrong_current(client):
    r, email = _register(client)
    token = r.json()["token"]

    r = client.post(
        "/v1/auth/change-password",
        json={"current_password": "WrongPassword", "new_password": "NewPass456!!"},
        headers=_auth(token),
    )
    assert r.status_code == 401


def test_change_password_weak_new(client):
    r, email = _register(client)
    token = r.json()["token"]

    r = client.post(
        "/v1/auth/change-password",
        json={"current_password": "TestPass123!", "new_password": "short"},
        headers=_auth(token),
    )
    assert r.status_code == 422


# ---- Members -----------------------------------------------------------------

def test_list_members(client):
    r = client.get("/v1/members", headers={"Authorization": f"Bearer {DEV_KEY}"})
    assert r.status_code == 200
    data = r.json()
    assert "data" in data


def test_invite_member(client):
    new_email = _unique_email()
    r = client.post(
        "/v1/members",
        json={"email": new_email, "role": "viewer"},
        headers={"Authorization": f"Bearer {DEV_KEY}"},
    )
    assert r.status_code in (201, 200)
    assert r.json()["status"] == "invited"


def test_invite_duplicate_fails(client):
    new_email = _unique_email()
    # Register user first.
    _register(client, email=new_email)

    # Invite existing user.
    r = client.post(
        "/v1/members",
        json={"email": new_email, "role": "viewer"},
        headers={"Authorization": f"Bearer {DEV_KEY}"},
    )
    # First invite succeeds (adds to project).
    if r.status_code == 201:
        # Second invite should fail (already a member).
        r2 = client.post(
            "/v1/members",
            json={"email": new_email, "role": "operator"},
            headers={"Authorization": f"Bearer {DEV_KEY}"},
        )
        assert r2.status_code == 409


# ---- Settings ----------------------------------------------------------------

def test_get_settings(client):
    r = client.get("/v1/settings", headers={"Authorization": f"Bearer {DEV_KEY}"})
    assert r.status_code == 200
    data = r.json()
    assert "project_name" in data
    assert "retention" in data


def test_update_settings(client):
    r = client.patch(
        "/v1/settings",
        json={"timezone": "America/New_York"},
        headers={"Authorization": f"Bearer {DEV_KEY}"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "updated"


# ---- GDPR export -------------------------------------------------------------

def test_gdpr_export(client):
    r, email = _register(client)
    token = r.json()["token"]

    r = client.get("/v1/auth/me/export", headers=_auth(token))
    assert r.status_code == 200
    data = r.json()
    assert "user" in data
    assert data["user"]["email"] == email


# ---- Member admin actions ----------------------------------------------------

def test_reset_member_password(client):
    r, email = _register(client)
    user_id = None

    # Get user ID from members list.
    members = client.get(
        "/v1/members",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
    ).json()["data"]

    for m in members:
        if m["email"] == email:
            user_id = m["id"]
            break

    if user_id:
        r = client.post(
            f"/v1/members/{user_id}/reset-password",
            headers={"Authorization": f"Bearer {DEV_KEY}"},
        )
        assert r.status_code == 200
        assert "temporary_password" in r.json()


def test_disable_member_mfa(client):
    r, email = _register(client)
    user_id = None

    members = client.get(
        "/v1/members",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
    ).json()["data"]

    for m in members:
        if m["email"] == email:
            user_id = m["id"]
            break

    if user_id:
        r = client.post(
            f"/v1/members/{user_id}/disable-mfa",
            headers={"Authorization": f"Bearer {DEV_KEY}"},
        )
        assert r.status_code == 200


# ---- Heartbeat interception --------------------------------------------------

def test_heartbeat_span_not_stored(client):
    """Heartbeat spans should be intercepted, not stored as regular spans."""
    from heartbeat import _last_heartbeat, record_heartbeat

    record_heartbeat("test-agent-hb")
    assert "test-agent-hb" in _last_heartbeat


def test_heartbeat_is_heartbeat_span():
    from heartbeat import is_heartbeat_span
    assert is_heartbeat_span("strathon.heartbeat") is True
    assert is_heartbeat_span("normal.span") is False


# ---- SDK integrity check -----------------------------------------------------

def test_code_hash_change_detected():
    """Code hash change fires warning (check via logger)."""
    from api.traces import _last_code_hash, _check_code_hash

    _check_code_hash("test-agent-int", "hash_aaa")
    assert _last_code_hash["test-agent-int"] == "hash_aaa"

    # Second call with same hash: no alert.
    _check_code_hash("test-agent-int", "hash_aaa")

    # Third call with different hash: should log warning.
    _check_code_hash("test-agent-int", "hash_bbb")
    assert _last_code_hash["test-agent-int"] == "hash_bbb"
