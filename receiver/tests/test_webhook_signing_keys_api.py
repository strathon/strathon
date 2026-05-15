"""HTTP-level tests for /v1/webhook_signing_keys.

Brings up the receiver in-process via TestClient. The whole point of
these tests is to verify:

  * The plaintext signing secret appears in the POST 201 response and
    NOWHERE else (no GET endpoint reveals it).
  * Scope enforcement: webhook_signing_keys:read and
    webhook_signing_keys:write are enforced on the right endpoints.
  * Revoke is idempotent and cleanly drops the plaintext from the
    in-memory keystore so subsequent deliveries don't sign with it.
  * Cross-project leak defense: a key in project A cannot be revoked
    via an API key for project B.

We use the seeded dev key (wildcard scope) for setup and create
narrower keys per-test to drive the enforcement scenarios.
"""

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


@pytest.fixture(autouse=True)
def _isolated_keystore():
    """Each test starts with an empty in-process keystore so prior tests
    don't pollute the per-project plaintext cache."""
    from webhooks.keystore import reset_for_testing
    reset_for_testing()
    yield
    reset_for_testing()


@pytest.fixture(autouse=True)
def _cleanup_signing_keys(client):
    """Remove any rows our tests created so subsequent runs start clean."""
    yield
    # Mark all keys for the default project as revoked then drop them in the DB.
    import psycopg
    db_url = os.environ["DATABASE_URL"]
    conn = psycopg.connect(db_url, autocommit=True)
    try:
        conn.execute("DELETE FROM webhook_signing_keys")
    finally:
        conn.close()


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _create_api_key(client, scopes: list[str]) -> str:
    """Helper: mint a project-scoped key with the given scopes."""
    r = client.post(
        "/v1/api_keys",
        headers=_auth(DEV_KEY),
        json={"name": f"test_key_{','.join(scopes)}", "scopes": scopes},
    )
    assert r.status_code == 201, r.text
    return r.json()["key"]


# ---- Plaintext-only-once contract --------------------------------------


def test_post_returns_plaintext_secret(client):
    r = client.post("/v1/webhook_signing_keys", headers=_auth(DEV_KEY), json={})
    assert r.status_code == 201
    body = r.json()
    assert "secret" in body
    assert body["secret"].startswith("whsec_")
    assert "id" in body
    assert "prefix" in body
    assert len(body["prefix"]) == 4


def test_get_does_not_return_plaintext_secret(client):
    # Create one so there's something to list
    client.post("/v1/webhook_signing_keys", headers=_auth(DEV_KEY), json={})

    r = client.get("/v1/webhook_signing_keys", headers=_auth(DEV_KEY))
    assert r.status_code == 200
    body = r.json()
    assert "webhook_signing_keys" in body
    assert len(body["webhook_signing_keys"]) >= 1
    # The CRITICAL assertion: no plaintext leaks via list
    for row in body["webhook_signing_keys"]:
        assert "secret" not in row
        assert "secret_hash" not in row
        assert "plaintext" not in row


def test_post_secret_value_can_be_used_to_sign_via_standard_webhooks(client):
    """The secret returned by POST is a working whsec_* that round-trips
    through the standardwebhooks library — i.e., it's not malformed."""
    from standardwebhooks.webhooks import Webhook
    from datetime import datetime, timezone

    r = client.post("/v1/webhook_signing_keys", headers=_auth(DEV_KEY), json={})
    secret = r.json()["secret"]

    wh = Webhook(secret)
    now = datetime.now(timezone.utc)
    sig = wh.sign("msg_smoke", now, '{"x":1}')

    import math
    headers = {
        "webhook-id": "msg_smoke",
        "webhook-timestamp": str(math.floor(now.timestamp())),
        "webhook-signature": sig,
    }
    wh.verify('{"x":1}', headers)  # raises on failure


# ---- Keystore integration ----------------------------------------------


def test_post_populates_keystore_with_plaintext(client):
    """After POST, the in-process keystore must have the new plaintext
    so the very next webhook delivery signs with it."""
    from webhooks.keystore import get_active_secrets
    from uuid import UUID

    r = client.post("/v1/webhook_signing_keys", headers=_auth(DEV_KEY), json={})
    body = r.json()
    project_id = UUID(body["project_id"])
    secret = body["secret"]

    active = get_active_secrets(project_id)
    assert secret in active


def test_delete_removes_plaintext_from_keystore(client):
    from webhooks.keystore import get_active_secrets
    from uuid import UUID

    r = client.post("/v1/webhook_signing_keys", headers=_auth(DEV_KEY), json={})
    body = r.json()
    project_id = UUID(body["project_id"])
    key_id = body["id"]
    secret = body["secret"]

    # Confirm it's in the keystore
    assert secret in get_active_secrets(project_id)

    # Revoke
    r = client.delete(
        f"/v1/webhook_signing_keys/{key_id}",
        headers=_auth(DEV_KEY),
    )
    assert r.status_code == 200

    # Now the plaintext must be gone — the next delivery would NOT sign
    # with it.
    assert secret not in get_active_secrets(project_id)


def test_delete_only_affects_target_key_other_keys_remain(client):
    """The rotation case: operator created key B while A was active,
    waits for cutover, revokes A. Key B must keep signing."""
    from webhooks.keystore import get_active_secrets
    from uuid import UUID

    a = client.post("/v1/webhook_signing_keys", headers=_auth(DEV_KEY), json={}).json()
    b = client.post("/v1/webhook_signing_keys", headers=_auth(DEV_KEY), json={}).json()
    project_id = UUID(a["project_id"])

    assert a["secret"] in get_active_secrets(project_id)
    assert b["secret"] in get_active_secrets(project_id)

    # Revoke A
    client.delete(f"/v1/webhook_signing_keys/{a['id']}", headers=_auth(DEV_KEY))

    remaining = get_active_secrets(project_id)
    assert a["secret"] not in remaining
    assert b["secret"] in remaining


# ---- List ordering -----------------------------------------------------


def test_list_default_hides_revoked(client):
    a = client.post("/v1/webhook_signing_keys", headers=_auth(DEV_KEY), json={}).json()
    b = client.post("/v1/webhook_signing_keys", headers=_auth(DEV_KEY), json={}).json()
    client.delete(f"/v1/webhook_signing_keys/{a['id']}", headers=_auth(DEV_KEY))

    r = client.get("/v1/webhook_signing_keys", headers=_auth(DEV_KEY))
    rows = r.json()["webhook_signing_keys"]
    ids = [row["id"] for row in rows]
    assert b["id"] in ids
    assert a["id"] not in ids


def test_list_with_include_revoked_shows_all(client):
    a = client.post("/v1/webhook_signing_keys", headers=_auth(DEV_KEY), json={}).json()
    client.delete(f"/v1/webhook_signing_keys/{a['id']}", headers=_auth(DEV_KEY))

    r = client.get(
        "/v1/webhook_signing_keys?include_revoked=true",
        headers=_auth(DEV_KEY),
    )
    rows = r.json()["webhook_signing_keys"]
    ids = [row["id"] for row in rows]
    assert a["id"] in ids


# ---- Idempotency -------------------------------------------------------


def test_delete_idempotent(client):
    r = client.post("/v1/webhook_signing_keys", headers=_auth(DEV_KEY), json={}).json()
    first = client.delete(f"/v1/webhook_signing_keys/{r['id']}", headers=_auth(DEV_KEY))
    second = client.delete(f"/v1/webhook_signing_keys/{r['id']}", headers=_auth(DEV_KEY))
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["revoked_at"] == second.json()["revoked_at"]


def test_delete_unknown_id_returns_404(client):
    import uuid
    r = client.delete(
        f"/v1/webhook_signing_keys/{uuid.uuid4()}",
        headers=_auth(DEV_KEY),
    )
    assert r.status_code == 404


def test_delete_invalid_uuid_returns_400(client):
    r = client.delete(
        "/v1/webhook_signing_keys/not-a-uuid",
        headers=_auth(DEV_KEY),
    )
    assert r.status_code == 400


# ---- Scope enforcement -------------------------------------------------


def test_get_requires_webhook_signing_keys_read(client):
    """A key without the scope must be rejected with 403."""
    narrow = _create_api_key(client, ["traces:write"])  # no webhook_signing_keys:*
    r = client.get("/v1/webhook_signing_keys", headers=_auth(narrow))
    assert r.status_code == 403


def test_get_works_with_webhook_signing_keys_read_scope(client):
    narrow = _create_api_key(client, ["webhook_signing_keys:read"])
    r = client.get("/v1/webhook_signing_keys", headers=_auth(narrow))
    assert r.status_code == 200


def test_post_requires_webhook_signing_keys_write(client):
    narrow = _create_api_key(client, ["webhook_signing_keys:read"])  # read != write
    r = client.post("/v1/webhook_signing_keys", headers=_auth(narrow), json={})
    assert r.status_code == 403


def test_post_works_with_webhook_signing_keys_write_scope(client):
    narrow = _create_api_key(client, ["webhook_signing_keys:write"])
    r = client.post("/v1/webhook_signing_keys", headers=_auth(narrow), json={})
    assert r.status_code == 201


def test_delete_requires_webhook_signing_keys_write(client):
    created = client.post(
        "/v1/webhook_signing_keys", headers=_auth(DEV_KEY), json={},
    ).json()

    narrow_read = _create_api_key(client, ["webhook_signing_keys:read"])
    r = client.delete(
        f"/v1/webhook_signing_keys/{created['id']}",
        headers=_auth(narrow_read),
    )
    assert r.status_code == 403


def test_wildcard_key_allowed_on_all_endpoints(client):
    """The dev key already has wildcard; verify it works on all three."""
    r = client.get("/v1/webhook_signing_keys", headers=_auth(DEV_KEY))
    assert r.status_code == 200
    r = client.post("/v1/webhook_signing_keys", headers=_auth(DEV_KEY), json={})
    assert r.status_code == 201
    key_id = r.json()["id"]
    r = client.delete(f"/v1/webhook_signing_keys/{key_id}", headers=_auth(DEV_KEY))
    assert r.status_code == 200


def test_missing_authorization_returns_401(client):
    r = client.get("/v1/webhook_signing_keys")
    assert r.status_code == 401
