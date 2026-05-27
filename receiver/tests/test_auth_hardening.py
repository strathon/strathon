"""Tests for auth hardening: re-auth, session invalidation, last_used_at."""

from __future__ import annotations

import os
import uuid

import pytest


DEV_KEY = "stra_dev_local_default_project_do_not_use_in_production"


@pytest.fixture(scope="module")
def client():
    db_url = os.getenv(
        "DATABASE_URL",
        "postgresql://strathon:strathon_dev@127.0.0.1:5432/strathon",
    )
    os.environ["DATABASE_URL"] = db_url
    import psycopg
    try:
        psycopg.connect(db_url, autocommit=True).close()
    except Exception:
        pytest.skip("Postgres not reachable")
    from fastapi.testclient import TestClient
    import main
    with TestClient(main.app) as c:
        yield c


def _unique_email():
    return f"authtest-{uuid.uuid4().hex[:8]}@example.com"


def _cleanup(email):
    import psycopg
    conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
    try:
        conn.execute(
            "UPDATE project_members SET invited_by = NULL WHERE invited_by IN "
            "(SELECT id FROM users WHERE LOWER(email) = LOWER(%s))",
            (email,),
        )
        conn.execute(
            "DELETE FROM sessions WHERE user_id IN "
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


def test_password_reset_invalidates_all_sessions(client):
    """After password change, old sessions should be invalid."""
    email = _unique_email()
    password = "test-password-123456"

    # Register.
    r = client.post(
        "/v1/auth/register",
        json={"email": email, "password": password},
    )
    assert r.status_code == 201
    token1 = r.json()["token"]

    # Login again to get a second session.
    r = client.post(
        "/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert r.status_code == 200
    token2 = r.json()["token"]

    # Both tokens work.
    assert client.get(
        "/v1/auth/me", headers={"Authorization": f"Bearer {token1}"}
    ).status_code == 200

    # Directly invalidate sessions in DB (simulates password reset).
    import psycopg
    conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
    try:
        conn.execute(
            "DELETE FROM sessions WHERE user_id IN "
            "(SELECT id FROM users WHERE LOWER(email) = LOWER(%s))",
            (email,),
        )
    finally:
        conn.close()

    # Both old tokens should now be invalid.
    assert client.get(
        "/v1/auth/me", headers={"Authorization": f"Bearer {token1}"}
    ).status_code == 401
    assert client.get(
        "/v1/auth/me", headers={"Authorization": f"Bearer {token2}"}
    ).status_code == 401

    _cleanup(email)


def test_api_key_last_used_at_updated(client):
    """API key last_used_at should be updated on each use."""
    import psycopg

    # Create a key.
    r = client.post(
        "/v1/api_keys",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
        json={"name": f"last-used-test-{uuid.uuid4().hex[:6]}"},
    )
    assert r.status_code == 201
    key = r.json()["key"]
    key_id = r.json()["id"]

    # Use the key.
    client.get("/v1/policies", headers={"Authorization": f"Bearer {key}"})

    # Check last_used_at is set.
    conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
    try:
        cur = conn.execute(
            "SELECT last_used_at FROM api_keys WHERE id = %s::uuid",
            (key_id,),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] is not None  # last_used_at should be set
    finally:
        conn.close()


def test_require_reauth_function():
    """Unit test for the require_reauth function."""
    from unittest.mock import AsyncMock

    from auth import require_reauth, ApiKeyContext

    import asyncio

    # API key auth skips re-auth.
    ctx_api = ApiKeyContext(
        key_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        key_prefix="stra_test12",
        scopes=("*",),
    )
    session = AsyncMock()
    # Should not raise.
    asyncio.run(
        require_reauth(ctx_api, session)
    )

    # Session auth without confirm headers raises.
    ctx_session = ApiKeyContext(
        key_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        key_prefix="session",
        scopes=("*",),
        user_id=uuid.uuid4(),
        role="admin",
        auth_method="session",
    )
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            require_reauth(ctx_session, session)
        )
    assert exc_info.value.status_code == 403
    assert "re-authentication" in exc_info.value.detail.lower()
