"""HTTP-level tests for capability scope enforcement.

These tests bring up the FastAPI app via TestClient and hit it with
keys that have specific scope sets. They're slower than the in-process
repository tests (each module-scoped fixture runs the lifespan, which
runs migrations) but they're the only way to exercise the require_scope
dependency end-to-end.

Test matrix per scope:
  * Key WITHOUT the scope -> 403 with a descriptive body
  * Key WITH the scope    -> 2xx (specific code per endpoint)
  * Key with wildcard '*' -> 2xx (always)
  * No Authorization      -> 401

We also cover validate_scopes (pure) and the create_api_key endpoint's
input validation for the scopes field.
"""

from __future__ import annotations

import os
import sys
import uuid

import pytest

_RECEIVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _RECEIVER_DIR)


# ===========================================================================
# Pure unit tests for the scope helpers
# ===========================================================================


def test_validate_scopes_accepts_known_scopes():
    from auth import (
        SCOPE_POLICIES_READ,
        SCOPE_TRACES_WRITE,
        validate_scopes,
    )
    # Must not raise
    validate_scopes([SCOPE_TRACES_WRITE])
    validate_scopes([SCOPE_TRACES_WRITE, SCOPE_POLICIES_READ])
    validate_scopes(["*"])


def test_validate_scopes_rejects_empty():
    from auth import validate_scopes
    with pytest.raises(ValueError, match="non-empty"):
        validate_scopes([])


def test_validate_scopes_rejects_unknown():
    from auth import validate_scopes
    with pytest.raises(ValueError, match="unknown scope"):
        validate_scopes(["traces:write", "not_a_real_scope"])


def test_validate_scopes_lists_known_scopes_on_error():
    """The error message should help the caller fix the bad input."""
    from auth import validate_scopes
    try:
        validate_scopes(["nope"])
    except ValueError as exc:
        msg = str(exc)
        # The error should mention BOTH the bad input and what's valid
        assert "nope" in msg
        assert "Known scopes" in msg
        assert "traces:write" in msg  # at least one real scope shown
    else:
        pytest.fail("expected ValueError")


def test_key_has_scope_wildcard_grants_everything():
    from auth import key_has_scope
    assert key_has_scope(("*",), "traces:write") is True
    assert key_has_scope(("*",), "anything:at_all") is True


def test_key_has_scope_exact_match():
    from auth import key_has_scope
    assert key_has_scope(("traces:write",), "traces:write") is True
    assert key_has_scope(("traces:write",), "policies:write") is False


def test_key_has_scope_empty_tuple_grants_nothing():
    from auth import key_has_scope
    assert key_has_scope((), "traces:write") is False


# ===========================================================================
# HTTP-level tests via TestClient
# ===========================================================================
#
# Each test uses the in-process app and the seeded dev key for setup
# (wildcard scope). It then creates project-scoped keys with specific
# scope sets to drive the enforcement scenarios.


DEV_KEY = "stra_dev_local_default_project_do_not_use_in_production"


@pytest.fixture(scope="module")
def client():
    """In-process FastAPI TestClient. Runs the lifespan (migrations + lifespan
    setup) once per module."""
    db_url = os.getenv(
        "DATABASE_URL",
        "postgresql://strathon:strathon_dev@127.0.0.1:5432/strathon",
    )
    # Make sure the receiver sees the DB
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


def _create_key(client, name: str, scopes: list[str] | None = None) -> str:
    """Helper: use the dev key to mint a new key with explicit scopes.
    Returns the raw key string."""
    payload: dict = {"name": name}
    if scopes is not None:
        payload["scopes"] = scopes
    resp = client.post(
        "/v1/api_keys",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
        json=payload,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["key"]


# ---- /v1/policies scope enforcement -------------------------------------


def test_policies_read_with_traces_only_returns_403(client):
    """A key with only traces:write must NOT be able to read policies."""
    key = _create_key(client, f"sdk-{uuid.uuid4().hex[:8]}", ["traces:write"])
    resp = client.get(
        "/v1/policies",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 403
    assert "policies:read" in resp.json()["detail"]


def test_policies_read_with_policies_read_succeeds(client):
    key = _create_key(client, f"reader-{uuid.uuid4().hex[:8]}", ["policies:read"])
    resp = client.get(
        "/v1/policies",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200
    assert "policies" in resp.json()


def test_policies_write_with_read_only_returns_403(client):
    """policies:read should not let you create policies."""
    key = _create_key(client, f"reader-{uuid.uuid4().hex[:8]}", ["policies:read"])
    resp = client.post(
        "/v1/policies",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "name": "should_fail",
            "match_expression": "true",
            "action": "log",
        },
    )
    assert resp.status_code == 403
    assert "policies:write" in resp.json()["detail"]


def test_policies_write_with_write_scope_succeeds(client):
    key = _create_key(client, f"writer-{uuid.uuid4().hex[:8]}", ["policies:write"])
    resp = client.post(
        "/v1/policies",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "name": f"smoke-{uuid.uuid4().hex[:8]}",
            "match_expression": "true",
            "action": "log",
        },
    )
    assert resp.status_code == 201


def test_wildcard_scope_grants_policies_read(client):
    """The dev key has wildcard; this must work."""
    resp = client.get(
        "/v1/policies",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
    )
    assert resp.status_code == 200


# ---- /v1/api_keys scope enforcement -------------------------------------


def test_api_keys_list_without_admin_scope_returns_403(client):
    """The previously-unauthenticated endpoint now requires api_keys:read."""
    key = _create_key(
        client, f"sdk-{uuid.uuid4().hex[:8]}",
        ["traces:write", "policies:read"],
    )
    resp = client.get(
        "/v1/api_keys",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 403


def test_api_keys_create_without_admin_scope_returns_403(client):
    """An SDK key cannot mint new keys."""
    key = _create_key(client, f"sdk-{uuid.uuid4().hex[:8]}", ["traces:write"])
    resp = client.post(
        "/v1/api_keys",
        headers={"Authorization": f"Bearer {key}"},
        json={"name": "should_fail"},
    )
    assert resp.status_code == 403
    assert "api_keys:write" in resp.json()["detail"]


def test_api_keys_create_with_admin_scope_succeeds(client):
    key = _create_key(
        client, f"admin-{uuid.uuid4().hex[:8]}",
        ["api_keys:read", "api_keys:write"],
    )
    resp = client.post(
        "/v1/api_keys",
        headers={"Authorization": f"Bearer {key}"},
        json={"name": f"child-{uuid.uuid4().hex[:8]}"},
    )
    assert resp.status_code == 201


def test_api_keys_revoke_without_admin_scope_returns_403(client):
    """Revocation requires api_keys:write."""
    # Use the dev key to create a victim
    victim = client.post(
        "/v1/api_keys",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
        json={"name": f"victim-{uuid.uuid4().hex[:8]}"},
    ).json()
    victim_id = victim["id"]

    # And a non-admin attacker
    attacker = _create_key(
        client, f"attacker-{uuid.uuid4().hex[:8]}", ["traces:write"],
    )
    resp = client.delete(
        f"/v1/api_keys/{victim_id}",
        headers={"Authorization": f"Bearer {attacker}"},
    )
    assert resp.status_code == 403


# ---- /v1/traces scope enforcement (smoke) -------------------------------


def test_traces_post_without_traces_scope_returns_403(client):
    """A policies-only key cannot ingest traces."""
    key = _create_key(client, f"ro-{uuid.uuid4().hex[:8]}", ["policies:read"])
    # Empty protobuf body would normally 400; the auth check fires first.
    resp = client.post(
        "/v1/traces",
        headers={"Authorization": f"Bearer {key}"},
        content=b"",
    )
    assert resp.status_code == 403
    assert "traces:write" in resp.json()["detail"]


# ---- Default scopes & validation on POST /v1/api_keys ------------------


def test_create_api_key_default_scopes(client):
    """When scopes is omitted, the new key gets the SDK defaults."""
    resp = client.post(
        "/v1/api_keys",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
        json={"name": f"default-{uuid.uuid4().hex[:8]}"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert set(body["scopes"]) == {"traces:write", "policies:read"}


def test_create_api_key_with_explicit_scopes(client):
    resp = client.post(
        "/v1/api_keys",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
        json={
            "name": f"explicit-{uuid.uuid4().hex[:8]}",
            "scopes": ["policies:read"],
        },
    )
    assert resp.status_code == 201
    assert resp.json()["scopes"] == ["policies:read"]


def test_create_api_key_rejects_unknown_scope(client):
    resp = client.post(
        "/v1/api_keys",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
        json={
            "name": f"bad-{uuid.uuid4().hex[:8]}",
            "scopes": ["traces:write", "not_a_scope"],
        },
    )
    assert resp.status_code == 400
    assert "not_a_scope" in resp.json()["detail"]


def test_create_api_key_rejects_empty_scopes_list(client):
    resp = client.post(
        "/v1/api_keys",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
        json={
            "name": f"empty-{uuid.uuid4().hex[:8]}",
            "scopes": [],
        },
    )
    assert resp.status_code == 400


def test_create_api_key_rejects_non_list_scopes(client):
    resp = client.post(
        "/v1/api_keys",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
        json={
            "name": f"wrong-type-{uuid.uuid4().hex[:8]}",
            "scopes": "traces:write",
        },
    )
    # scopes is typed list[str] on the request schema, so a bare string is
    # rejected at the validation layer with 422 (Unprocessable Entity).
    assert resp.status_code == 422


# ---- Auth missing vs auth lacking scope ---------------------------------


def test_no_authorization_header_returns_401(client):
    """Sanity: missing credential is 401, not 403."""
    resp = client.get("/v1/policies")
    assert resp.status_code == 401


def test_invalid_token_returns_401(client):
    resp = client.get(
        "/v1/policies",
        headers={"Authorization": "Bearer stra_not_a_real_key_at_all"},
    )
    assert resp.status_code == 401


# ---- /v1/policies/generate auth -----------------------------------------
#
# This endpoint calls a paid LLM API using the server's STRATHON_AI_API_KEY.
# It was previously UNAUTHENTICATED, which is a credit-burn / open-LLM-proxy
# vector. These tests lock in that it now requires policies:write.


def test_policies_generate_unauthenticated_returns_401(client):
    """No Authorization header must be rejected before any LLM call."""
    resp = client.post(
        "/v1/policies/generate",
        json={"description": "block all outbound email"},
    )
    assert resp.status_code == 401


def test_policies_generate_with_read_only_returns_403(client):
    """policies:read is not enough; generation is a write-class action."""
    key = _create_key(client, f"reader-{uuid.uuid4().hex[:8]}", ["policies:read"])
    resp = client.post(
        "/v1/policies/generate",
        headers={"Authorization": f"Bearer {key}"},
        json={"description": "block all outbound email"},
    )
    assert resp.status_code == 403
    assert "policies:write" in resp.json()["detail"]


def test_policies_generate_with_write_scope_passes_auth(client):
    """With policies:write the request clears auth and reaches the endpoint.
    Without STRATHON_AI_API_KEY configured it returns 400 (not 401/403),
    which proves authorization succeeded."""
    key = _create_key(client, f"writer-{uuid.uuid4().hex[:8]}", ["policies:write"])
    resp = client.post(
        "/v1/policies/generate",
        headers={"Authorization": f"Bearer {key}"},
        json={"description": "block all outbound email"},
    )
    # 400 = passed auth, failed because no AI key is set in the test env.
    # (If a key were configured it would be 200/502; never 401/403 here.)
    assert resp.status_code not in (401, 403)
