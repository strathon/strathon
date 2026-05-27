"""Tests for MFA (TOTP) and password reset endpoints."""

from __future__ import annotations

import os
import sys

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


def _register_user(client, email, password="TestPass123!"):
    """Register a test user. Returns the response."""
    return client.post(
        "/v1/auth/register",
        json={"email": email, "password": password},
    )


def _login(client, email, password="TestPass123!"):
    """Login a user. Returns the response."""
    return client.post(
        "/v1/auth/login",
        json={"email": email, "password": password},
    )


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# MFA unit tests (repository-level)
# ---------------------------------------------------------------------------


class TestMfaRepository:
    def test_generate_totp_secret(self):
        from repositories.mfa import generate_totp_secret
        secret = generate_totp_secret()
        assert len(secret) == 32  # base32, 160 bits
        assert secret.isalnum()

    def test_verify_totp_code(self):
        import pyotp
        from repositories.mfa import generate_totp_secret, verify_totp_code
        secret = generate_totp_secret()
        totp = pyotp.TOTP(secret)
        code = totp.now()
        assert verify_totp_code(secret, code) is True
        assert verify_totp_code(secret, "000000") is False

    def test_generate_backup_codes(self):
        from repositories.mfa import generate_backup_codes
        plain, hashed = generate_backup_codes()
        assert len(plain) == 8
        assert len(hashed) == 8
        # Plain and hashed are different.
        assert plain[0] != hashed[0]
        # Hashed are SHA-256 hex (64 chars).
        assert all(len(h) == 64 for h in hashed)

    def test_backup_code_hash_matches(self):
        from repositories.mfa import generate_backup_codes, _hash_backup_code
        plain, hashed = generate_backup_codes()
        assert _hash_backup_code(plain[0]) == hashed[0]


# ---------------------------------------------------------------------------
# Password reset unit tests
# ---------------------------------------------------------------------------


class TestPasswordResetRepository:
    def test_hash_token_deterministic(self):
        from repositories.password_reset import _hash_token
        h1 = _hash_token("test-token")
        h2 = _hash_token("test-token")
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex


# ---------------------------------------------------------------------------
# MFA API-level tests
# ---------------------------------------------------------------------------


def test_mfa_setup_requires_session_auth(client):
    """MFA setup with API key is rejected."""
    resp = client.post(
        "/v1/auth/mfa/setup",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
    )
    assert resp.status_code == 400


def test_mfa_verify_setup_requires_session_auth(client):
    resp = client.post(
        "/v1/auth/mfa/verify-setup",
        json={"code": "123456"},
        headers={"Authorization": f"Bearer {DEV_KEY}"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Password reset API-level tests
# ---------------------------------------------------------------------------


def test_reset_request_returns_501_without_smtp(client):
    """Without STRATHON_SMTP_HOST, email reset returns 501."""
    os.environ.pop("STRATHON_SMTP_HOST", None)
    resp = client.post(
        "/v1/auth/reset-password/request",
        json={"email": "nobody@example.com"},
    )
    assert resp.status_code == 501


def test_reset_confirm_rejects_invalid_token(client):
    resp = client.post(
        "/v1/auth/reset-password/confirm",
        json={"token": "invalid-token", "new_password": "NewPass123!"},
    )
    assert resp.status_code == 400


def test_reset_confirm_rejects_short_password(client):
    resp = client.post(
        "/v1/auth/reset-password/confirm",
        json={"token": "some-token", "new_password": "short"},
    )
    assert resp.status_code == 400


def test_admin_reset_requires_session_auth(client):
    """Admin reset with API key is rejected."""
    resp = client.post(
        "/v1/auth/admin-reset-password",
        json={"email": "nobody@example.com"},
        headers={"Authorization": f"Bearer {DEV_KEY}"},
    )
    assert resp.status_code == 400
