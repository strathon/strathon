"""HTTP-level tests for /v1/webhook_deliveries.

In-process TestClient hits the full app. We seed deliveries directly
via SQL (the actor isn't reachable from this test path; the API
surface is what we're exercising).

Coverage:
  * list with status/policy filters
  * single get returns payload, scopes to project
  * replay returns 202, transitions status, increments attempts cleared
  * replay on wrong status returns 409
  * scope enforcement on each endpoint
  * 400/404 error paths
"""

from __future__ import annotations

import os
import sys
import uuid

import pytest

_RECEIVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _RECEIVER_DIR)


DEV_KEY = "stra_dev_local_default_project_do_not_use_in_production"
DEFAULT_PROJECT_ID = "00000000-0000-0000-0000-000000000001"


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


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _create_api_key(client, scopes: list[str]) -> str:
    r = client.post(
        "/v1/api_keys",
        headers=_auth(DEV_KEY),
        json={"name": f"deliv_test_{','.join(scopes)}", "scopes": scopes},
    )
    assert r.status_code == 201, r.text
    return r.json()["key"]


@pytest.fixture
def seeded_delivery(client):
    """Seed one policy + one DLQ delivery for the default project.
    Returns (policy_id, delivery_id). Cleans up after the test."""
    import psycopg
    db_url = os.environ["DATABASE_URL"]
    policy_id = str(uuid.uuid4())
    delivery_id = str(uuid.uuid4())
    conn = psycopg.connect(db_url, autocommit=True)
    try:
        conn.execute(
            """INSERT INTO policies
               (id, project_id, name, description, match_expression,
                action, action_config, applies_to, enabled, priority)
               VALUES (%s::uuid, %s::uuid, %s, '', 'true', 'alert',
                       %s::jsonb, ARRAY[]::TEXT[], true, 0)""",
            (policy_id, DEFAULT_PROJECT_ID, f"api_test_{policy_id[:8]}",
             '{"webhook_url":"https://example.test/h"}'),
        )
        conn.execute(
            """INSERT INTO webhook_deliveries
               (id, project_id, policy_id, webhook_id, url, payload,
                status, attempts, max_attempts, last_response_status, last_error)
               VALUES (%s::uuid, %s::uuid, %s::uuid, %s, %s, %s::jsonb,
                       'dlq', 8, 8, 503, 'down')""",
            (delivery_id, DEFAULT_PROJECT_ID, policy_id,
             f"msg_apitest_{delivery_id[:8]}", "https://example.test/h",
             '{"event":"alert"}'),
        )
    finally:
        conn.close()

    yield policy_id, delivery_id

    # Cleanup
    conn = psycopg.connect(db_url, autocommit=True)
    try:
        conn.execute("DELETE FROM webhook_deliveries WHERE policy_id = %s::uuid",
                     (policy_id,))
        conn.execute("DELETE FROM policies WHERE id = %s::uuid", (policy_id,))
    finally:
        conn.close()


# ---- list -------------------------------------------------------------


def test_list_returns_seeded_delivery(client, seeded_delivery):
    _, delivery_id = seeded_delivery
    r = client.get("/v1/webhook_deliveries", headers=_auth(DEV_KEY))
    assert r.status_code == 200
    body = r.json()
    assert "webhook_deliveries" in body
    assert "next_cursor" in body
    ids = [d["id"] for d in body["webhook_deliveries"]]
    assert delivery_id in ids


def test_list_filters_by_status(client, seeded_delivery):
    _, delivery_id = seeded_delivery
    # DLQ filter includes our seeded row
    r = client.get(
        "/v1/webhook_deliveries?status_filter=dlq",
        headers=_auth(DEV_KEY),
    )
    assert r.status_code == 200
    assert delivery_id in [d["id"] for d in r.json()["webhook_deliveries"]]

    # succeeded filter excludes it
    r = client.get(
        "/v1/webhook_deliveries?status_filter=succeeded",
        headers=_auth(DEV_KEY),
    )
    assert delivery_id not in [d["id"] for d in r.json()["webhook_deliveries"]]


def test_list_filters_by_policy_id(client, seeded_delivery):
    policy_id, delivery_id = seeded_delivery
    r = client.get(
        f"/v1/webhook_deliveries?policy_id={policy_id}",
        headers=_auth(DEV_KEY),
    )
    assert r.status_code == 200
    assert delivery_id in [d["id"] for d in r.json()["webhook_deliveries"]]

    # Bogus policy_id returns empty (and 200)
    r = client.get(
        f"/v1/webhook_deliveries?policy_id={uuid.uuid4()}",
        headers=_auth(DEV_KEY),
    )
    assert r.status_code == 200
    assert r.json()["webhook_deliveries"] == []


def test_list_rejects_invalid_policy_id(client):
    r = client.get(
        "/v1/webhook_deliveries?policy_id=not-a-uuid",
        headers=_auth(DEV_KEY),
    )
    assert r.status_code == 400


def test_list_rejects_invalid_status(client):
    r = client.get(
        "/v1/webhook_deliveries?status_filter=imaginary",
        headers=_auth(DEV_KEY),
    )
    assert r.status_code == 400


def test_list_list_response_omits_payload_field(client, seeded_delivery):
    """Payload is intentionally not included in list responses to keep
    them small. The single-get endpoint is where you get the body."""
    _, delivery_id = seeded_delivery
    r = client.get("/v1/webhook_deliveries", headers=_auth(DEV_KEY))
    matching = [d for d in r.json()["webhook_deliveries"] if d["id"] == delivery_id]
    assert len(matching) == 1
    assert "payload" not in matching[0]


# ---- get single -------------------------------------------------------


def test_get_single_returns_full_payload(client, seeded_delivery):
    _, delivery_id = seeded_delivery
    r = client.get(
        f"/v1/webhook_deliveries/{delivery_id}",
        headers=_auth(DEV_KEY),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == delivery_id
    assert body["payload"] == {"event": "alert"}
    assert body["status"] == "dlq"


def test_get_unknown_id_returns_404(client):
    r = client.get(
        f"/v1/webhook_deliveries/{uuid.uuid4()}",
        headers=_auth(DEV_KEY),
    )
    assert r.status_code == 404


def test_get_invalid_uuid_returns_400(client):
    r = client.get(
        "/v1/webhook_deliveries/not-a-uuid",
        headers=_auth(DEV_KEY),
    )
    assert r.status_code == 400


# ---- replay -----------------------------------------------------------


def test_replay_dlq_returns_202_and_resets(client, seeded_delivery):
    _, delivery_id = seeded_delivery
    r = client.post(
        f"/v1/webhook_deliveries/{delivery_id}/replay",
        headers=_auth(DEV_KEY),
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "pending"
    assert body["attempts"] == 0
    assert body["last_response_status"] is None
    assert body["last_error"] is None


def test_replay_succeeded_returns_409(client):
    """Replaying a successful delivery is a 409 with a descriptive
    message — operators get a clear hint that replay is for failures
    only."""
    import psycopg
    db_url = os.environ["DATABASE_URL"]
    policy_id = str(uuid.uuid4())
    delivery_id = str(uuid.uuid4())
    conn = psycopg.connect(db_url, autocommit=True)
    try:
        conn.execute(
            """INSERT INTO policies (id, project_id, name, description,
                                     match_expression, action, action_config,
                                     applies_to, enabled, priority)
               VALUES (%s::uuid, %s::uuid, %s, '', 'true', 'alert',
                       %s::jsonb, ARRAY[]::TEXT[], true, 0)""",
            (policy_id, DEFAULT_PROJECT_ID, f"replay409_{policy_id[:8]}",
             '{"webhook_url":"https://example.test/h"}'),
        )
        conn.execute(
            """INSERT INTO webhook_deliveries
               (id, project_id, policy_id, webhook_id, url, payload,
                status, attempts, max_attempts, last_response_status)
               VALUES (%s::uuid, %s::uuid, %s::uuid, %s, %s, %s::jsonb,
                       'succeeded', 1, 8, 200)""",
            (delivery_id, DEFAULT_PROJECT_ID, policy_id,
             f"msg_409_{delivery_id[:8]}", "https://example.test/h", '{"x":1}'),
        )

        r = client.post(
            f"/v1/webhook_deliveries/{delivery_id}/replay",
            headers=_auth(DEV_KEY),
        )
        assert r.status_code == 409
        assert "replay is only allowed" in r.json()["detail"]
    finally:
        conn.execute("DELETE FROM webhook_deliveries WHERE id = %s::uuid",
                     (delivery_id,))
        conn.execute("DELETE FROM policies WHERE id = %s::uuid", (policy_id,))
        conn.close()


def test_replay_unknown_id_returns_404(client):
    r = client.post(
        f"/v1/webhook_deliveries/{uuid.uuid4()}/replay",
        headers=_auth(DEV_KEY),
    )
    assert r.status_code == 404


def test_replay_invalid_uuid_returns_400(client):
    r = client.post(
        "/v1/webhook_deliveries/not-a-uuid/replay",
        headers=_auth(DEV_KEY),
    )
    assert r.status_code == 400


# ---- scope enforcement ------------------------------------------------


def test_list_requires_read_scope(client):
    narrow = _create_api_key(client, ["traces:write"])  # no webhook_deliveries:*
    r = client.get("/v1/webhook_deliveries", headers=_auth(narrow))
    assert r.status_code == 403


def test_list_works_with_deliveries_read_scope(client):
    narrow = _create_api_key(client, ["webhook_deliveries:read"])
    r = client.get("/v1/webhook_deliveries", headers=_auth(narrow))
    assert r.status_code == 200


def test_get_requires_read_scope(client, seeded_delivery):
    _, delivery_id = seeded_delivery
    narrow = _create_api_key(client, ["traces:write"])
    r = client.get(
        f"/v1/webhook_deliveries/{delivery_id}",
        headers=_auth(narrow),
    )
    assert r.status_code == 403


def test_replay_requires_write_scope(client, seeded_delivery):
    """webhook_deliveries:read is NOT enough for replay — replay
    produces an externally-visible side effect (re-dispatches the
    webhook) so it gets the stricter scope."""
    _, delivery_id = seeded_delivery
    narrow_read = _create_api_key(client, ["webhook_deliveries:read"])
    r = client.post(
        f"/v1/webhook_deliveries/{delivery_id}/replay",
        headers=_auth(narrow_read),
    )
    assert r.status_code == 403


def test_replay_works_with_write_scope(client, seeded_delivery):
    _, delivery_id = seeded_delivery
    narrow_write = _create_api_key(
        client, ["webhook_deliveries:read", "webhook_deliveries:write"],
    )
    r = client.post(
        f"/v1/webhook_deliveries/{delivery_id}/replay",
        headers=_auth(narrow_write),
    )
    assert r.status_code == 202


def test_no_authorization_returns_401(client):
    r = client.get("/v1/webhook_deliveries")
    assert r.status_code == 401
