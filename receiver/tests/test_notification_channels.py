"""Tests for notification channels CRUD and integrations."""

from __future__ import annotations

import json
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


def _auth():
    return {"Authorization": f"Bearer {DEV_KEY}"}


# ---- CRUD tests -------------------------------------------------------------


def test_create_slack_channel(client):
    r = client.post(
        "/v1/notification-channels",
        headers=_auth(),
        json={
            "channel_type": "slack",
            "name": f"test-slack-{uuid.uuid4().hex[:6]}",
            "config": {"webhook_url": "https://hooks.slack.com/services/T00/B00/xxx"},
            "events": ["incident", "approval_request"],
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["channel_type"] == "slack"
    assert body["enabled"] is True
    # Cleanup.
    client.delete(f"/v1/notification-channels/{body['id']}", headers=_auth())


def test_create_discord_channel(client):
    r = client.post(
        "/v1/notification-channels",
        headers=_auth(),
        json={
            "channel_type": "discord",
            "name": f"test-discord-{uuid.uuid4().hex[:6]}",
            "config": {"webhook_url": "https://discord.com/api/webhooks/123/abc"},
            "events": [],
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["channel_type"] == "discord"
    client.delete(f"/v1/notification-channels/{body['id']}", headers=_auth())


def test_create_invalid_channel_type(client):
    r = client.post(
        "/v1/notification-channels",
        headers=_auth(),
        json={
            "channel_type": "telegram",
            "name": "invalid",
            "config": {},
        },
    )
    assert r.status_code == 400


def test_create_invalid_event_type(client):
    r = client.post(
        "/v1/notification-channels",
        headers=_auth(),
        json={
            "channel_type": "slack",
            "name": "invalid-event",
            "config": {},
            "events": ["nonexistent_event"],
        },
    )
    assert r.status_code == 400


def test_list_channels(client):
    # Create two.
    ids = []
    for ct in ("slack", "discord"):
        r = client.post(
            "/v1/notification-channels",
            headers=_auth(),
            json={
                "channel_type": ct,
                "name": f"list-test-{ct}-{uuid.uuid4().hex[:6]}",
                "config": {"webhook_url": "https://example.com"},
            },
        )
        assert r.status_code == 201
        ids.append(r.json()["id"])

    r = client.get("/v1/notification-channels", headers=_auth())
    assert r.status_code == 200
    data = r.json()["data"]
    listed_ids = {ch["id"] for ch in data}
    for cid in ids:
        assert cid in listed_ids

    for cid in ids:
        client.delete(f"/v1/notification-channels/{cid}", headers=_auth())


def test_update_channel(client):
    r = client.post(
        "/v1/notification-channels",
        headers=_auth(),
        json={
            "channel_type": "webhook",
            "name": f"update-test-{uuid.uuid4().hex[:6]}",
            "config": {"url": "https://old.example.com"},
        },
    )
    cid = r.json()["id"]

    r = client.patch(
        f"/v1/notification-channels/{cid}",
        headers=_auth(),
        json={
            "name": "updated-name",
            "enabled": False,
        },
    )
    assert r.status_code == 200
    assert r.json()["name"] == "updated-name"
    assert r.json()["enabled"] is False

    client.delete(f"/v1/notification-channels/{cid}", headers=_auth())


def test_delete_channel(client):
    r = client.post(
        "/v1/notification-channels",
        headers=_auth(),
        json={
            "channel_type": "slack",
            "name": f"delete-test-{uuid.uuid4().hex[:6]}",
            "config": {},
        },
    )
    cid = r.json()["id"]

    r = client.delete(f"/v1/notification-channels/{cid}", headers=_auth())
    assert r.status_code == 204

    r = client.delete(f"/v1/notification-channels/{cid}", headers=_auth())
    assert r.status_code == 404


def test_delete_nonexistent(client):
    r = client.delete(
        f"/v1/notification-channels/{uuid.uuid4()}",
        headers=_auth(),
    )
    assert r.status_code == 404


# ---- Formatter tests --------------------------------------------------------


def test_slack_approval_formatter():
    from integrations.slack import format_approval_request

    event = {
        "approval_id": "abc-123",
        "agent_name": "research-bot",
        "tool_name": "send_email",
        "policy_name": "block-email",
        "timeout_seconds": 300,
    }
    payload = format_approval_request(event, "http://localhost:4318")
    assert "blocks" in payload
    # Has approve + deny buttons.
    actions = [b for b in payload["blocks"] if b.get("type") == "actions"]
    assert len(actions) == 1
    buttons = actions[0]["elements"]
    assert len(buttons) == 2
    assert buttons[0]["action_id"] == "strathon_approve"
    assert buttons[1]["action_id"] == "strathon_deny"


def test_slack_incident_formatter():
    from integrations.slack import format_incident

    event = {
        "severity": "critical",
        "trigger": "block_spike",
        "affected_agents": ["agent-a", "agent-b"],
        "eu_ai_act_reporting": {
            "deadline_days": 2,
            "deadline_date": "2026-06-01",
            "description": "Report to authority",
        },
    }
    payload = format_incident(event)
    assert "blocks" in payload
    text = json.dumps(payload)
    assert "Article 73" in text
    assert "2 days" in text


def test_discord_approval_formatter():
    from integrations.discord import format_approval_request

    event = {
        "approval_id": "xyz-789",
        "agent_name": "deploy-bot",
        "tool_name": "kubectl_apply",
        "policy_name": "require-approval-deploys",
        "timeout_seconds": 600,
    }
    payload = format_approval_request(event, "http://localhost:4318")
    assert "embeds" in payload
    assert "components" in payload
    buttons = payload["components"][0]["components"]
    assert buttons[0]["label"] == "Approve"
    assert buttons[1]["label"] == "Deny"


def test_discord_incident_formatter():
    from integrations.discord import format_incident

    event = {
        "severity": "high",
        "trigger": "error_spike",
        "affected_agents": ["agent-x"],
        "eu_ai_act_reporting": {"deadline_days": 15},
    }
    payload = format_incident(event)
    assert payload["embeds"][0]["color"] == 0xE67E22  # orange for high


def test_slack_signature_verification():
    import hashlib
    import hmac as hmac_mod
    import time

    from integrations.slack import verify_slack_signature

    secret = "test_signing_secret_123"
    ts = str(int(time.time()))
    body = b'payload=test'

    sig_basestring = f"v0:{ts}:{body.decode()}"
    expected = "v0=" + hmac_mod.new(
        secret.encode(), sig_basestring.encode(), hashlib.sha256,
    ).hexdigest()

    assert verify_slack_signature(secret, ts, body, expected) is True
    assert verify_slack_signature(secret, ts, body, "v0=bad") is False
    # Old timestamp (replay).
    old_ts = str(int(time.time()) - 600)
    assert verify_slack_signature(secret, old_ts, body, expected) is False
