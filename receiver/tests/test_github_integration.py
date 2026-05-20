"""Tests for GitHub integration: CRUD + webhook handler."""

from __future__ import annotations

import hashlib
import hmac
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


WEBHOOK_SECRET = "test-webhook-secret-12345678"


def _unique_repo():
    return f"test-org/repo-{uuid.uuid4().hex[:8]}"


# ---- CRUD tests -------------------------------------------------------------


def test_register_github_integration(client):
    repo = _unique_repo()
    r = client.post(
        "/v1/integrations/github",
        headers=_auth(),
        json={
            "repo_full_name": repo,
            "webhook_secret": WEBHOOK_SECRET,
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["repo_full_name"] == repo
    # Cleanup.
    client.delete(f"/v1/integrations/github/{body['id']}", headers=_auth())


def test_register_duplicate_returns_409(client):
    repo = _unique_repo()
    r1 = client.post(
        "/v1/integrations/github",
        headers=_auth(),
        json={"repo_full_name": repo, "webhook_secret": WEBHOOK_SECRET},
    )
    assert r1.status_code == 201

    r2 = client.post(
        "/v1/integrations/github",
        headers=_auth(),
        json={"repo_full_name": repo, "webhook_secret": WEBHOOK_SECRET},
    )
    assert r2.status_code == 409

    client.delete(f"/v1/integrations/github/{r1.json()['id']}", headers=_auth())


def test_list_integrations(client):
    repo = _unique_repo()
    r = client.post(
        "/v1/integrations/github",
        headers=_auth(),
        json={"repo_full_name": repo, "webhook_secret": WEBHOOK_SECRET},
    )
    iid = r.json()["id"]

    r = client.get("/v1/integrations/github", headers=_auth())
    assert r.status_code == 200
    assert any(i["id"] == iid for i in r.json()["data"])

    client.delete(f"/v1/integrations/github/{iid}", headers=_auth())


def test_delete_integration(client):
    repo = _unique_repo()
    r = client.post(
        "/v1/integrations/github",
        headers=_auth(),
        json={"repo_full_name": repo, "webhook_secret": WEBHOOK_SECRET},
    )
    iid = r.json()["id"]

    r = client.delete(f"/v1/integrations/github/{iid}", headers=_auth())
    assert r.status_code == 204

    r = client.delete(f"/v1/integrations/github/{iid}", headers=_auth())
    assert r.status_code == 404


def test_delete_nonexistent(client):
    r = client.delete(
        f"/v1/integrations/github/{uuid.uuid4()}",
        headers=_auth(),
    )
    assert r.status_code == 404


# ---- Webhook tests -----------------------------------------------------------


def _sign_payload(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256,
    ).hexdigest()


def test_push_webhook_tracks_commits(client):
    repo = _unique_repo()
    r = client.post(
        "/v1/integrations/github",
        headers=_auth(),
        json={"repo_full_name": repo, "webhook_secret": WEBHOOK_SECRET},
    )
    assert r.status_code == 201

    payload = {
        "ref": "refs/heads/main",
        "repository": {"full_name": repo},
        "commits": [
            {
                "id": "abc123def456",
                "message": "feat: add new feature",
                "author": {"name": "Test User", "email": "test@example.com"},
                "timestamp": "2026-05-19T12:00:00Z",
            },
            {
                "id": "def789ghi012",
                "message": "fix: bug fix",
                "author": {"name": "Test User", "email": "test@example.com"},
                "timestamp": "2026-05-19T12:01:00Z",
            },
        ],
    }
    body = json.dumps(payload).encode()
    sig = _sign_payload(WEBHOOK_SECRET, body)

    r = client.post(
        "/v1/integrations/github/webhooks",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "push",
            "X-Hub-Signature-256": sig,
        },
    )
    assert r.status_code == 200
    assert r.json()["commits_tracked"] == 2

    # Verify commits are listed.
    r = client.get(
        "/v1/integrations/github/commits",
        headers=_auth(),
        params={"repo": repo},
    )
    assert r.status_code == 200
    commits = r.json()["data"]
    shas = {c["commit_sha"] for c in commits}
    assert "abc123def456" in shas
    assert "def789ghi012" in shas

    # Cleanup.
    import psycopg
    conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
    try:
        conn.execute("DELETE FROM git_commits WHERE repo_full_name = %s", (repo,))
        conn.execute("DELETE FROM github_integrations WHERE repo_full_name = %s", (repo,))
    finally:
        conn.close()


def test_webhook_invalid_signature(client):
    repo = _unique_repo()
    client.post(
        "/v1/integrations/github",
        headers=_auth(),
        json={"repo_full_name": repo, "webhook_secret": WEBHOOK_SECRET},
    )

    payload = {
        "repository": {"full_name": repo},
        "commits": [],
        "ref": "refs/heads/main",
    }
    body = json.dumps(payload).encode()

    r = client.post(
        "/v1/integrations/github/webhooks",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "push",
            "X-Hub-Signature-256": "sha256=invalid",
        },
    )
    assert r.status_code == 403

    # Cleanup.
    import psycopg
    conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
    try:
        conn.execute("DELETE FROM github_integrations WHERE repo_full_name = %s", (repo,))
    finally:
        conn.close()


def test_webhook_unknown_repo(client):
    payload = {
        "repository": {"full_name": "nonexistent/repo"},
        "commits": [],
    }
    body = json.dumps(payload).encode()

    r = client.post(
        "/v1/integrations/github/webhooks",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "push",
        },
    )
    assert r.status_code == 404


def test_ping_event(client):
    repo = _unique_repo()
    client.post(
        "/v1/integrations/github",
        headers=_auth(),
        json={"repo_full_name": repo, "webhook_secret": WEBHOOK_SECRET},
    )

    payload = {"repository": {"full_name": repo}, "zen": "Keep it simple."}
    body = json.dumps(payload).encode()
    sig = _sign_payload(WEBHOOK_SECRET, body)

    r = client.post(
        "/v1/integrations/github/webhooks",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "ping",
            "X-Hub-Signature-256": sig,
        },
    )
    assert r.status_code == 200
    assert r.json()["event"] == "ping"

    import psycopg
    conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
    try:
        conn.execute("DELETE FROM github_integrations WHERE repo_full_name = %s", (repo,))
    finally:
        conn.close()
