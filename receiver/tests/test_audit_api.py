"""End-to-end tests for /v1/audit/* endpoints and mutation hooks.

Covers:

- Scope enforcement on each endpoint (read/write/admin separations)
- Listing, single-event get, verify, anchor list
- Cursor pagination over /events
- SCIM filter parse errors → 400
- Audit-of-audit: GET /events itself produces an audit.read event
- Stream CRUD
- Integration: every hooked mutation endpoint produces an audit row
  with the right action + category + actor

The fixtures here mirror the established TestClient pattern from
tests/test_api_key_scopes.py: one module-scoped client running the
real lifespan, helper to mint scoped keys, the dev key for setup.
"""

from __future__ import annotations

import os
import secrets
import uuid

import pytest


DEV_KEY = "stra_dev_local_default_project_do_not_use_in_production"


@pytest.fixture(scope="module", autouse=True)
def _audit_hmac_key():
    """Set a stable test HMAC key for the whole module, then put it back.

    Without the teardown this random key outlives the module and every test
    that runs afterwards in the same process signs and verifies audit rows
    under it -- a different key than they would otherwise use. That is the
    same shared-state-mutation class that produced the long-standing CI
    flake (a test mutating global state its neighbours depend on), so
    restore the previous value even though nothing depends on it today.
    """
    previous = os.environ.get("STRATHON_AUDIT_HMAC_KEY")
    os.environ["STRATHON_AUDIT_HMAC_KEY"] = secrets.token_hex(32)
    yield
    if previous is None:
        os.environ.pop("STRATHON_AUDIT_HMAC_KEY", None)
    else:
        os.environ["STRATHON_AUDIT_HMAC_KEY"] = previous


@pytest.fixture(scope="module")
def client():
    db_url = os.getenv(
        "DATABASE_URL",
        "postgresql://strathon:strathon_dev@127.0.0.1:5432/strathon",
    )
    os.environ["DATABASE_URL"] = db_url
    try:
        import psycopg
        psycopg.connect(db_url, autocommit=True).close()
    except Exception:
        pytest.skip("Postgres not reachable")

    from fastapi.testclient import TestClient
    import main

    with TestClient(main.app) as c:
        yield c

    # Cleanup: these tests create policies, halts, and model_prices
    # through the live API (which commits to the DB). Without cleanup
    # those rows survive the test run and pollute other tests'
    # expectations — notably any test that scans policies and assumes
    # the only rows present are its own.
    import psycopg
    conn = psycopg.connect(db_url, autocommit=True)
    try:
        conn.execute(
            "DELETE FROM policy_matches WHERE policy_id IN ("
            "SELECT id FROM policies "
            "WHERE name LIKE 'int-pol-%' OR name LIKE 'audit-policy-%' "
            "OR name LIKE 'del-%' OR name LIKE 'v-%'"
            ")"
        )
        conn.execute(
            "DELETE FROM policies "
            "WHERE name LIKE 'int-pol-%' OR name LIKE 'audit-policy-%' "
            "OR name LIKE 'del-%' OR name LIKE 'v-%'"
        )
        conn.execute(
            "DELETE FROM halt_state WHERE reason = 'integration test'"
        )
        conn.execute(
            "DELETE FROM model_price_overrides "
            "WHERE model_name LIKE 'test-model-%'"
        )
    finally:
        conn.close()


def _mint(client, name: str, scopes: list[str]) -> str:
    payload = {"name": name, "scopes": scopes}
    resp = client.post(
        "/v1/api_keys",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
        json=payload,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["key"]


def _all_events(client, key: str, **params) -> list[dict]:
    resp = client.get(
        "/v1/audit/events",
        headers={"Authorization": f"Bearer {key}"},
        params=params,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


# --- Scope enforcement ------------------------------------------------------


def test_get_events_requires_audit_read(client):
    key = _mint(client, f"writer-{uuid.uuid4().hex[:6]}", ["policies:write"])
    resp = client.get(
        "/v1/audit/events",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 403


def test_get_events_succeeds_with_audit_read(client):
    key = _mint(client, f"audit-r-{uuid.uuid4().hex[:6]}", ["audit:read"])
    resp = client.get(
        "/v1/audit/events",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200


def test_create_stream_requires_audit_write(client):
    key = _mint(client, f"audit-ro-{uuid.uuid4().hex[:6]}", ["audit:read"])
    resp = client.post(
        "/v1/audit/streams",
        headers={"Authorization": f"Bearer {key}"},
        json={"name": "x", "url": "https://example.com/audit"},
    )
    assert resp.status_code == 403


def test_create_stream_succeeds_with_audit_write(client):
    key = _mint(
        client, f"audit-rw-{uuid.uuid4().hex[:6]}",
        ["audit:read", "audit:write"],
    )
    name = f"stream-{uuid.uuid4().hex[:6]}"
    resp = client.post(
        "/v1/audit/streams",
        headers={"Authorization": f"Bearer {key}"},
        json={"name": name, "url": "https://example.com/audit"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == name

    # Cleanup
    sid = body["id"]
    client.delete(
        f"/v1/audit/streams/{sid}",
        headers={"Authorization": f"Bearer {key}"},
    )


# --- Read path ---------------------------------------------------------------


def test_list_events_returns_newest_first(client):
    key = _mint(client, f"audit-r-{uuid.uuid4().hex[:6]}", ["audit:read"])
    # The very act of GET /events produces an event itself (audit-of-audit).
    events = _all_events(client, key, limit=5)
    if len(events) > 1:
        # Sequence numbers are monotone; descending order means newest first.
        seqs = [e["sequence_no"] for e in events]
        assert seqs == sorted(seqs, reverse=True)


def test_get_single_event_after_creation(client):
    """Create a policy → look it up in the audit log → fetch by id."""
    key = _mint(
        client, f"audit-test-{uuid.uuid4().hex[:6]}",
        ["policies:write", "audit:read"],
    )
    pname = f"audit-policy-{uuid.uuid4().hex[:6]}"
    resp = client.post(
        "/v1/policies",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "name": pname,
            "match_expression": "true",
            "action": "log",
        },
    )
    assert resp.status_code == 201, resp.text

    # Find the audit event for this creation.
    events = _all_events(
        client, key,
        filter='action eq "policy.create"',
        limit=20,
    )
    matches = [
        e for e in events
        if e.get("after_state", {}).get("name") == pname
    ]
    assert len(matches) >= 1
    eid = matches[0]["id"]

    # Fetch by id.
    single = client.get(
        f"/v1/audit/events/{eid}",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert single.status_code == 200
    body = single.json()
    assert body["action"] == "policy.create"
    assert body["action_category"] == "policy"
    assert body["outcome"] == "allow"
    assert body["resource"]["type"] == "policy"


def test_get_event_404_for_unknown_id(client):
    key = _mint(client, f"audit-r-{uuid.uuid4().hex[:6]}", ["audit:read"])
    resp = client.get(
        f"/v1/audit/events/{uuid.uuid4()}",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 404


def test_verify_event_endpoint(client):
    """Verify a fresh event roundtrips: emit, fetch id, verify."""
    key = _mint(
        client, f"audit-v-{uuid.uuid4().hex[:6]}",
        ["policies:write", "audit:read"],
    )
    resp = client.post(
        "/v1/policies",
        headers={"Authorization": f"Bearer {key}"},
        json={"name": f"v-{uuid.uuid4().hex[:6]}", "match_expression": "true", "action": "log"},
    )
    assert resp.status_code == 201
    events = _all_events(client, key, filter='action eq "policy.create"', limit=10)
    eid = events[0]["id"]
    verify = client.get(
        f"/v1/audit/events/{eid}/verify",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert verify.status_code == 200
    body = verify.json()
    assert body["valid"] is True
    assert body["event_id"] == eid


# --- SCIM filter error path -------------------------------------------------


def test_invalid_filter_returns_400(client):
    key = _mint(client, f"audit-r-{uuid.uuid4().hex[:6]}", ["audit:read"])
    resp = client.get(
        "/v1/audit/events?filter=garbage_attr+eq+%22x%22",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 400
    assert "invalid filter" in resp.json()["detail"]


def test_invalid_cursor_returns_500_or_400(client):
    """Bad cursor surfaces as an error to the operator, not a silent reset."""
    key = _mint(client, f"audit-r-{uuid.uuid4().hex[:6]}", ["audit:read"])
    resp = client.get(
        "/v1/audit/events?cursor=not-base64-or-json",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code in (400, 500)


# --- Anchors -----------------------------------------------------------------


def _set_admin_key(value):
    """Set STRATHON_ADMIN_API_KEY on the live settings and clear the cache."""
    import os as _os

    from config import get_settings

    if value is None:
        _os.environ.pop("STRATHON_ADMIN_API_KEY", None)
    else:
        _os.environ["STRATHON_ADMIN_API_KEY"] = value
    get_settings.cache_clear()


def _set_mode(value):
    """Set STRATHON_MODE ('self-hosted' or 'cloud') and clear the cache."""
    import os as _os

    from config import get_settings

    if value is None:
        _os.environ.pop("STRATHON_MODE", None)
    else:
        _os.environ["STRATHON_MODE"] = value
    get_settings.cache_clear()


def test_anchors_list_requires_instance_admin(client):
    """In cloud (multi-tenant) mode the full anchor chain is instance-wide (one
    Merkle chain over every tenant), so a per-project audit:read key must NOT
    read it -- it requires the instance-admin credential."""
    admin = secrets.token_hex(32)
    _set_mode("cloud")
    _set_admin_key(admin)
    try:
        key = _mint(client, f"audit-a-{uuid.uuid4().hex[:6]}", ["audit:read"])
        # audit:read is rejected (no instance-admin credential)
        resp = client.get(
            "/v1/audit/anchors", headers={"Authorization": f"Bearer {key}"}
        )
        assert resp.status_code == 401
        # the instance-admin key works
        resp = client.get(
            "/v1/audit/anchors", headers={"Authorization": f"Bearer {admin}"}
        )
        assert resp.status_code == 200
        assert isinstance(resp.json()["data"], list)
    finally:
        _set_admin_key(None)
        _set_mode(None)


def test_anchors_list_fails_closed_without_admin_key(client):
    """In cloud mode, if STRATHON_ADMIN_API_KEY is unset the endpoint denies
    (503) rather than falling back to per-project auth or allowing the
    request."""
    _set_mode("cloud")
    _set_admin_key(None)
    try:
        key = _mint(client, f"audit-a-{uuid.uuid4().hex[:6]}", ["audit:read"])
        resp = client.get(
            "/v1/audit/anchors", headers={"Authorization": f"Bearer {key}"}
        )
        assert resp.status_code == 503
    finally:
        _set_mode(None)


def test_anchors_list_uses_audit_read_in_self_hosted(client):
    """In self-hosted (single-tenant) mode the chain only covers the one
    organization, so an audit:read key is the correct authority and no
    instance-admin credential is required. This matches the pre-audit
    behavior -- the operator is not locked out of their own integrity chain."""
    _set_mode("self-hosted")
    _set_admin_key(None)
    try:
        key = _mint(client, f"audit-a-{uuid.uuid4().hex[:6]}", ["audit:read"])
        resp = client.get(
            "/v1/audit/anchors", headers={"Authorization": f"Bearer {key}"}
        )
        assert resp.status_code == 200
        assert isinstance(resp.json()["data"], list)
    finally:
        _set_mode(None)


def test_anchor_status_is_per_project(client):
    """The per-project status endpoint returns only anchored/latest_at under a
    normal audit:read key, with no cross-tenant anchor detail."""
    key = _mint(client, f"audit-s-{uuid.uuid4().hex[:6]}", ["audit:read"])
    resp = client.get(
        "/v1/audit/anchors/status", headers={"Authorization": f"Bearer {key}"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"anchored", "latest_at"}
    assert isinstance(body["anchored"], bool)


# --- Audit-of-audit ---------------------------------------------------------


def test_get_events_self_logs(client):
    """Calling GET /events must itself produce an audit.read event."""
    key = _mint(client, f"audit-r-{uuid.uuid4().hex[:6]}", ["audit:read"])
    # Snapshot existing audit.read count for this key.
    before = _all_events(
        client, key,
        filter='action eq "audit.read"',
        limit=1000,
    )
    initial = len(before)
    # Now make a fresh GET. Each GET should add at least one new audit.read.
    _all_events(client, key, limit=5)
    after = _all_events(
        client, key,
        filter='action eq "audit.read"',
        limit=1000,
    )
    # The second list call adds events itself; we just need the count to grow.
    assert len(after) > initial


# --- Integration: each hooked endpoint produces matching audit row ----------


def _find_event_for(events: list[dict], *, action: str, resource_id: str) -> dict | None:
    for e in events:
        if e["action"] == action and e["resource"]["id"] == resource_id:
            return e
    return None


def test_policy_create_emits_audit(client):
    key = _mint(
        client, f"i-pol-{uuid.uuid4().hex[:6]}",
        ["policies:write", "audit:read"],
    )
    pname = f"int-pol-{uuid.uuid4().hex[:6]}"
    resp = client.post(
        "/v1/policies",
        headers={"Authorization": f"Bearer {key}"},
        json={"name": pname, "match_expression": "true", "action": "log"},
    )
    assert resp.status_code == 201
    policy_id = resp.json()["id"]

    events = _all_events(
        client, key,
        filter='action eq "policy.create"', limit=50,
    )
    match = _find_event_for(events, action="policy.create", resource_id=policy_id)
    assert match is not None
    assert match["actor"]["type"] == "service_account"
    assert match["outcome"] == "allow"


def test_policy_delete_emits_audit(client):
    key = _mint(
        client, f"i-pold-{uuid.uuid4().hex[:6]}",
        ["policies:write", "audit:read"],
    )
    resp = client.post(
        "/v1/policies",
        headers={"Authorization": f"Bearer {key}"},
        json={"name": f"del-{uuid.uuid4().hex[:6]}", "match_expression": "true", "action": "log"},
    )
    policy_id = resp.json()["id"]
    delete_resp = client.delete(
        f"/v1/policies/{policy_id}",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert delete_resp.status_code == 204

    events = _all_events(
        client, key,
        filter='action eq "policy.delete"', limit=50,
    )
    match = _find_event_for(events, action="policy.delete", resource_id=policy_id)
    assert match is not None


def test_halt_issue_emits_audit(client):
    key = _mint(
        client, f"i-halt-{uuid.uuid4().hex[:6]}",
        ["halts:write", "halts:read", "audit:read"],
    )
    resp = client.post(
        "/v1/halts",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "scope": "agent",
            "scope_value": f"a-{uuid.uuid4().hex[:6]}",
            "reason": "integration test",
        },
    )
    assert resp.status_code == 201, resp.text
    halt_id = str(resp.json()["halt"]["id"])
    events = _all_events(
        client, key,
        filter='action eq "halt.issue"', limit=50,
    )
    match = _find_event_for(events, action="halt.issue", resource_id=halt_id)
    assert match is not None
    assert match["action_category"] == "halt"


def test_api_key_create_emits_audit_without_raw_value(client):
    """Audit event for api_key.create must NOT include the raw key."""
    _mint(client, f"i-ak-{uuid.uuid4().hex[:6]}", ["policies:read"])
    audit_reader = _mint(
        client, f"i-ar-{uuid.uuid4().hex[:6]}", ["audit:read"]
    )

    events = _all_events(
        client, audit_reader,
        filter='action eq "api_key.create"', limit=50,
    )
    # Most recent api_key.create row should NOT contain the raw key value.
    for e in events:
        after = e.get("after_state") or {}
        # We never log a top-level "key" or "value" field.
        assert "key" not in after
        assert "value" not in after
        # raw key strings would start with stra_; nothing in the row
        # should contain a key prefix that looks like a full secret.
        # (The 12-char public prefix is fine; full ~40-char keys are not.)
        for v in after.values():
            if isinstance(v, str):
                assert not v.startswith("stra_") or len(v) <= 16


def test_project_settings_update_emits_audit(client):
    key = _mint(
        client, f"i-ps-{uuid.uuid4().hex[:6]}",
        ["project_settings:write", "audit:read"],
    )
    resp = client.patch(
        "/v1/project/settings",
        headers={"Authorization": f"Bearer {key}"},
        json={"intervention_default_action": "block"},
    )
    assert resp.status_code == 200, resp.text
    events = _all_events(
        client, key,
        filter='action eq "project_settings.update"', limit=50,
    )
    assert events
    latest = events[0]
    assert latest["before_state"] is not None
    assert latest["after_state"] == {
        "intervention_default_action": "block",
        "trace_retention_days": 30,
    }

    # Restore so other tests don't see allow-list mode.
    client.patch(
        "/v1/project/settings",
        headers={"Authorization": f"Bearer {key}"},
        json={"intervention_default_action": "allow"},
    )


def test_model_price_set_emits_audit(client):
    key = _mint(
        client, f"i-mp-{uuid.uuid4().hex[:6]}",
        ["model_prices:write", "audit:read"],
    )
    model = f"test-model-{uuid.uuid4().hex[:6]}"
    resp = client.post(
        "/v1/model_prices",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model_name": model,
            "input_cost_per_token": "0.00001",
            "output_cost_per_token": "0.00002",
        },
    )
    assert resp.status_code == 200, resp.text
    events = _all_events(
        client, key,
        filter='action eq "model_price.set"', limit=50,
    )
    match = _find_event_for(events, action="model_price.set", resource_id=model)
    assert match is not None
    client.delete(
        f"/v1/model_prices/{model}",
        headers={"Authorization": f"Bearer {key}"},
    )


def test_audit_stream_create_emits_audit(client):
    """Even creating an audit stream is itself audited."""
    key = _mint(
        client, f"i-as-{uuid.uuid4().hex[:6]}",
        ["audit:read", "audit:write"],
    )
    sname = f"as-{uuid.uuid4().hex[:6]}"
    resp = client.post(
        "/v1/audit/streams",
        headers={"Authorization": f"Bearer {key}"},
        json={"name": sname, "url": "https://example.com/audit"},
    )
    assert resp.status_code == 201
    sid = resp.json()["id"]
    events = _all_events(
        client, key,
        filter='action eq "audit_stream.create"', limit=50,
    )
    match = _find_event_for(events, action="audit_stream.create", resource_id=sid)
    assert match is not None
    client.delete(
        f"/v1/audit/streams/{sid}",
        headers={"Authorization": f"Bearer {key}"},
    )


