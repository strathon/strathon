"""HTTP-level tests for RBAC: auth endpoints + membership management.

Tests the full flow: register → login → /me → invite member → list →
change role → remove. Drives the real FastAPI app via TestClient.
Requires a live Postgres; skips cleanly when none is reachable.
"""

from __future__ import annotations

import os
import sys
import uuid

import pytest

_RECEIVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _RECEIVER_DIR)


DEV_KEY = "stra_dev_local_default_project_do_not_use_in_production"
DEFAULT_PROJECT_SLUG = "default"


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


def _unique_email() -> str:
    """Generate a unique test email to avoid collisions across test runs."""
    return f"test_{uuid.uuid4().hex[:12]}@example.com"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _session_headers(token: str, project_id: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "X-Project-Id": project_id,
    }


# ---- Cleanup helpers ------------------------------------------------------

def _cleanup_user(email: str) -> None:
    """Remove a test user and their memberships directly via SQL."""
    import psycopg
    db_url = os.environ["DATABASE_URL"]
    conn = psycopg.connect(db_url, autocommit=True)
    try:
        conn.execute(
            "DELETE FROM sessions WHERE user_id IN "
            "(SELECT id FROM users WHERE LOWER(email) = LOWER(%s))",
            (email,),
        )
        conn.execute(
            "UPDATE project_members SET invited_by = NULL WHERE invited_by IN "
            "(SELECT id FROM users WHERE LOWER(email) = LOWER(%s))",
            (email,),
        )
        conn.execute(
            "DELETE FROM project_members WHERE user_id IN "
            "(SELECT id FROM users WHERE LOWER(email) = LOWER(%s))",
            (email,),
        )
        conn.execute(
            "DELETE FROM users WHERE LOWER(email) = LOWER(%s)",
            (email,),
        )
    finally:
        conn.close()


def _ensure_project_owner(email: str, project_id: str) -> None:
    """Add user to the default project as owner via direct SQL.

    Registration only auto-assigns ownership to the very first user.
    In CI the first-user slot is taken by earlier tests, so subsequent
    registrations leave the user with no project membership. This helper
    ensures the test user is an owner so membership management tests work
    regardless of test ordering.
    """
    import psycopg
    db_url = os.environ["DATABASE_URL"]
    conn = psycopg.connect(db_url, autocommit=True)
    try:
        conn.execute(
            "INSERT INTO project_members (project_id, user_id, role) "
            "SELECT %s, id, 'owner' FROM users WHERE LOWER(email) = LOWER(%s) "
            "ON CONFLICT (project_id, user_id) DO UPDATE SET role = 'owner'",
            (project_id, email),
        )
    finally:
        conn.close()


# ---- Registration tests ---------------------------------------------------


def test_register_returns_201_with_token(client):
    email = _unique_email()
    r = client.post(
        "/v1/auth/register",
        json={"email": email, "password": "test-password-123", "display_name": "Test User"},
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert "token" in data
    assert data["user"]["email"] == email.lower()
    assert data["user"]["display_name"] == "Test User"
    _cleanup_user(email)


def test_register_duplicate_email_returns_409(client):
    email = _unique_email()
    r1 = client.post(
        "/v1/auth/register",
        json={"email": email, "password": "test-password-123"},
    )
    assert r1.status_code == 201, r1.text

    r2 = client.post(
        "/v1/auth/register",
        json={"email": email, "password": "different-password"},
    )
    assert r2.status_code == 409
    _cleanup_user(email)


def test_register_short_password_returns_422(client):
    r = client.post(
        "/v1/auth/register",
        json={"email": _unique_email(), "password": "short"},
    )
    assert r.status_code == 422


def test_register_bad_email_returns_400(client):
    r = client.post(
        "/v1/auth/register",
        json={"email": "not-an-email", "password": "test-password-123"},
    )
    assert r.status_code == 400


# ---- Login tests ----------------------------------------------------------


def test_login_returns_token(client):
    email = _unique_email()
    client.post(
        "/v1/auth/register",
        json={"email": email, "password": "test-password-123"},
    )

    r = client.post(
        "/v1/auth/login",
        json={"email": email, "password": "test-password-123"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert "token" in data
    assert data["user"]["email"] == email.lower()
    assert data["message"] == "logged in"
    _cleanup_user(email)


def test_login_wrong_password_returns_401(client):
    email = _unique_email()
    client.post(
        "/v1/auth/register",
        json={"email": email, "password": "test-password-123"},
    )

    r = client.post(
        "/v1/auth/login",
        json={"email": email, "password": "wrong-password"},
    )
    assert r.status_code == 401
    _cleanup_user(email)


def test_login_nonexistent_email_returns_401(client):
    r = client.post(
        "/v1/auth/login",
        json={"email": "doesnotexist@example.com", "password": "anything"},
    )
    assert r.status_code == 401


# ---- /me tests ------------------------------------------------------------


def test_me_returns_user_profile(client):
    email = _unique_email()
    reg = client.post(
        "/v1/auth/register",
        json={"email": email, "password": "test-password-123", "display_name": "Me Test"},
    )
    token = reg.json()["token"]

    r = client.get("/v1/auth/me", headers=_auth(token))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["user"]["email"] == email.lower()
    assert data["user"]["display_name"] == "Me Test"
    assert isinstance(data["projects"], list)
    _cleanup_user(email)


def test_me_rejects_api_key(client):
    r = client.get("/v1/auth/me", headers=_auth(DEV_KEY))
    assert r.status_code == 400


def test_me_rejects_expired_token(client):
    r = client.get("/v1/auth/me", headers=_auth("some-invalid-session-token"))
    assert r.status_code == 401


# ---- Logout tests ---------------------------------------------------------


def test_logout_invalidates_session(client):
    email = _unique_email()
    reg = client.post(
        "/v1/auth/register",
        json={"email": email, "password": "test-password-123"},
    )
    token = reg.json()["token"]

    # Verify session works
    r = client.get("/v1/auth/me", headers=_auth(token))
    assert r.status_code == 200

    # Logout
    r = client.post("/v1/auth/logout", headers=_auth(token))
    assert r.status_code == 204

    # Session should now be invalid
    r = client.get("/v1/auth/me", headers=_auth(token))
    assert r.status_code == 401
    _cleanup_user(email)


def test_logout_rejects_api_key(client):
    r = client.post("/v1/auth/logout", headers=_auth(DEV_KEY))
    assert r.status_code == 400


# ---- Membership management tests ------------------------------------------


def _get_default_project_id(client) -> str:
    """Get the default project UUID via the projects API."""
    r = client.get("/v1/projects/default", headers=_auth(DEV_KEY))
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_list_members_via_api_key(client):
    """API key with sufficient scope can list members."""
    r = client.get(
        f"/v1/projects/{DEFAULT_PROJECT_SLUG}/members",
        headers=_auth(DEV_KEY),
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert "members" in data
    assert isinstance(data["members"], list)


def test_add_and_remove_member(client):
    """Full membership lifecycle: add → list → remove."""
    project_id = _get_default_project_id(client)

    # Register owner (first user)
    owner_email = _unique_email()
    reg = client.post(
        "/v1/auth/register",
        json={"email": owner_email, "password": "owner-password-123"},
    )
    assert reg.status_code == 201, reg.text
    owner_token = reg.json()["token"]
    _ensure_project_owner(owner_email, project_id)

    # Register a second user to invite
    member_email = _unique_email()
    reg2 = client.post(
        "/v1/auth/register",
        json={"email": member_email, "password": "member-password-123"},
    )
    assert reg2.status_code == 201, reg2.text
    member_user_id = reg2.json()["user"]["id"]

    # Owner adds member as operator
    r = client.post(
        f"/v1/projects/{DEFAULT_PROJECT_SLUG}/members",
        headers=_session_headers(owner_token, project_id),
        json={"email": member_email, "role": "operator"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["role"] == "operator"

    # List members — should include the new member
    r = client.get(
        f"/v1/projects/{DEFAULT_PROJECT_SLUG}/members",
        headers=_session_headers(owner_token, project_id),
    )
    assert r.status_code == 200, r.text
    members = r.json()["members"]
    member_emails = [m["email"] for m in members]
    assert member_email.lower() in member_emails

    # Remove the member
    r = client.delete(
        f"/v1/projects/{DEFAULT_PROJECT_SLUG}/members/{member_user_id}",
        headers=_session_headers(owner_token, project_id),
    )
    assert r.status_code == 204

    # Verify removal
    r = client.get(
        f"/v1/projects/{DEFAULT_PROJECT_SLUG}/members",
        headers=_session_headers(owner_token, project_id),
    )
    member_emails = [m["email"] for m in r.json()["members"]]
    assert member_email.lower() not in member_emails

    _cleanup_user(owner_email)
    _cleanup_user(member_email)


def test_change_member_role(client):
    project_id = _get_default_project_id(client)

    owner_email = _unique_email()
    reg = client.post(
        "/v1/auth/register",
        json={"email": owner_email, "password": "owner-password-123"},
    )
    owner_token = reg.json()["token"]
    _ensure_project_owner(owner_email, project_id)

    member_email = _unique_email()
    reg2 = client.post(
        "/v1/auth/register",
        json={"email": member_email, "password": "member-password-123"},
    )
    member_user_id = reg2.json()["user"]["id"]

    # Add as operator
    client.post(
        f"/v1/projects/{DEFAULT_PROJECT_SLUG}/members",
        headers=_session_headers(owner_token, project_id),
        json={"email": member_email, "role": "operator"},
    )

    # Change to viewer
    r = client.patch(
        f"/v1/projects/{DEFAULT_PROJECT_SLUG}/members/{member_user_id}",
        headers=_session_headers(owner_token, project_id),
        json={"role": "viewer"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "viewer"

    _cleanup_user(owner_email)
    _cleanup_user(member_email)


def test_viewer_cannot_add_members(client):
    """Viewers should get 403 when trying to add members."""
    project_id = _get_default_project_id(client)

    # Register owner
    owner_email = _unique_email()
    reg = client.post(
        "/v1/auth/register",
        json={"email": owner_email, "password": "owner-password-123"},
    )
    owner_token = reg.json()["token"]
    _ensure_project_owner(owner_email, project_id)

    # Register viewer
    viewer_email = _unique_email()
    reg2 = client.post(
        "/v1/auth/register",
        json={"email": viewer_email, "password": "viewer-password-123"},
    )
    viewer_token = reg2.json()["token"]

    # Owner adds viewer
    client.post(
        f"/v1/projects/{DEFAULT_PROJECT_SLUG}/members",
        headers=_session_headers(owner_token, project_id),
        json={"email": viewer_email, "role": "viewer"},
    )

    # Register a third user
    third_email = _unique_email()
    client.post(
        "/v1/auth/register",
        json={"email": third_email, "password": "third-password-123"},
    )

    # Viewer tries to add the third user — should fail
    r = client.post(
        f"/v1/projects/{DEFAULT_PROJECT_SLUG}/members",
        headers=_session_headers(viewer_token, project_id),
        json={"email": third_email, "role": "viewer"},
    )
    assert r.status_code == 403

    _cleanup_user(owner_email)
    _cleanup_user(viewer_email)
    _cleanup_user(third_email)


def test_cannot_assign_owner_role(client):
    project_id = _get_default_project_id(client)

    owner_email = _unique_email()
    reg = client.post(
        "/v1/auth/register",
        json={"email": owner_email, "password": "owner-password-123"},
    )
    owner_token = reg.json()["token"]
    _ensure_project_owner(owner_email, project_id)

    target_email = _unique_email()
    client.post(
        "/v1/auth/register",
        json={"email": target_email, "password": "target-password-123"},
    )

    r = client.post(
        f"/v1/projects/{DEFAULT_PROJECT_SLUG}/members",
        headers=_session_headers(owner_token, project_id),
        json={"email": target_email, "role": "owner"},
    )
    assert r.status_code == 403

    _cleanup_user(owner_email)
    _cleanup_user(target_email)


def test_duplicate_member_add_returns_409(client):
    # Ensure a first user exists so subsequent registrations don't auto-join
    _throwaway = _unique_email()
    client.post(
        "/v1/auth/register",
        json={"email": _throwaway, "password": "throwaway-password-123"},
    )

    member_email = _unique_email()
    client.post(
        "/v1/auth/register",
        json={"email": member_email, "password": "member-password-123"},
    )

    # Add once using DEV_KEY (wildcard scope, always works)
    r = client.post(
        f"/v1/projects/{DEFAULT_PROJECT_SLUG}/members",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
        json={"email": member_email, "role": "operator"},
    )
    assert r.status_code == 201, f"first add failed: {r.status_code} {r.text}"

    # Add again — should conflict
    r = client.post(
        f"/v1/projects/{DEFAULT_PROJECT_SLUG}/members",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
        json={"email": member_email, "role": "viewer"},
    )
    assert r.status_code == 409

    _cleanup_user(member_email)
    _cleanup_user(_throwaway)


# ---- Login rate limiting tests --------------------------------------------


def test_login_rate_limiting(client):
    """After exhausting the login rate limit, further attempts get 429."""
    email = _unique_email()
    # Don't register — all attempts will fail with 401, which still
    # consumes rate limit tokens.
    responses = []
    for _ in range(10):
        r = client.post(
            "/v1/auth/login",
            json={"email": email, "password": "wrong"},
        )
        responses.append(r.status_code)

    # At least one should be 429 (rate limited)
    assert 429 in responses, f"Expected 429 in responses but got: {set(responses)}"
    _cleanup_user(email)  # no-op but safe
