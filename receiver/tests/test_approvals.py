"""Tests for human approval workflow."""

from __future__ import annotations

import os
import sys
import uuid

import pytest

_RECEIVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _RECEIVER_DIR)

DEV_KEY = "stra_dev_local_default_project_do_not_use_in_production"


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


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


# ---------------------------------------------------------------------------
# Policy with require_approval action
# ---------------------------------------------------------------------------


def test_create_require_approval_policy(client):
    resp = client.post(
        "/v1/policies",
        headers=_auth(DEV_KEY),
        json={
            "name": f"approval-{uuid.uuid4().hex[:8]}",
            "match_expression": "true",
            "action": "require_approval",
            "action_config": {"timeout_seconds": 60},
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["action"] == "require_approval"
    # Cleanup.
    client.delete(f"/v1/policies/{data['id']}", headers=_auth(DEV_KEY))


# ---------------------------------------------------------------------------
# Approvals API
# ---------------------------------------------------------------------------


def test_list_approvals_empty(client):
    resp = client.get("/v1/approvals", headers=_auth(DEV_KEY))
    assert resp.status_code == 200
    data = resp.json()
    assert "approvals" in data
    assert isinstance(data["approvals"], list)


def test_list_approvals_filter_by_status(client):
    resp = client.get(
        "/v1/approvals",
        headers=_auth(DEV_KEY),
        params={"status_filter": "pending"},
    )
    assert resp.status_code == 200


def test_list_approvals_invalid_status_returns_400(client):
    resp = client.get(
        "/v1/approvals",
        headers=_auth(DEV_KEY),
        params={"status_filter": "invalid"},
    )
    assert resp.status_code == 400


def test_get_approval_not_found(client):
    fake = str(uuid.uuid4())
    resp = client.get(
        f"/v1/approvals/{fake}", headers=_auth(DEV_KEY),
    )
    assert resp.status_code == 404


def test_approve_not_found(client):
    fake = str(uuid.uuid4())
    resp = client.post(
        f"/v1/approvals/{fake}/approve", headers=_auth(DEV_KEY),
    )
    assert resp.status_code == 404


def test_deny_not_found(client):
    fake = str(uuid.uuid4())
    resp = client.post(
        f"/v1/approvals/{fake}/deny", headers=_auth(DEV_KEY),
    )
    assert resp.status_code == 404


def test_approve_invalid_id_returns_400(client):
    resp = client.post(
        "/v1/approvals/not-a-uuid/approve", headers=_auth(DEV_KEY),
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Repository-level tests
# ---------------------------------------------------------------------------


async def test_create_and_resolve_approval(session, isolated_project):
    from repositories.approvals import create_approval, resolve_approval

    approval = await create_approval(
        session, isolated_project,
        policy_id=uuid.uuid4(),
        span_name="test.tool.search",
        tool_name="search",
        timeout_seconds=300,
    )
    assert approval.status == "pending"
    assert approval.expires_at is not None

    resolved = await resolve_approval(
        session, isolated_project, approval.id,
        decision="approved", resolved_by="test",
    )
    assert resolved is not None
    assert resolved.status == "approved"
    assert resolved.resolved_by == "test"


async def test_resolve_already_resolved_returns_none(session, isolated_project):
    from repositories.approvals import create_approval, resolve_approval

    approval = await create_approval(
        session, isolated_project,
        policy_id=uuid.uuid4(),
        timeout_seconds=60,
    )
    await resolve_approval(
        session, isolated_project, approval.id, decision="denied",
    )
    # Second resolve should return None.
    result = await resolve_approval(
        session, isolated_project, approval.id, decision="approved",
    )
    assert result is None


async def test_expire_pending_approvals(session, isolated_project):
    from datetime import datetime, timedelta, timezone
    from repositories.approvals import create_approval, expire_pending_approvals
    from models.approvals import Approval
    from sqlalchemy import select

    # Create an approval that's already expired (timeout_seconds=0 won't
    # work because expires_at = now + 0s = now, which might not be <= now
    # due to clock precision). Instead, create normally then manually
    # set expires_at to the past.
    approval = await create_approval(
        session, isolated_project,
        policy_id=uuid.uuid4(),
        timeout_seconds=1,
    )
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    approval.expires_at = past
    await session.flush()

    expired = await expire_pending_approvals(session)
    assert len(expired) >= 1

    # expire_pending_approvals runs a bulk UPDATE with
    # synchronize_session=False, which does not refresh the in-session ORM
    # object; populate_existing forces a reload so the assertion sees the
    # updated row rather than the stale identity-map copy.
    stmt = (
        select(Approval)
        .where(Approval.id == approval.id)
        .execution_options(populate_existing=True)
    )
    row = (await session.execute(stmt)).scalar_one()
    assert row.status == "expired"
    assert row.resolved_by == "timeout"
