"""Tests for multi-party approval workflow.

Covers:
- Single approver (backward compat: approvers_required=1, one approve resolves)
- Multi-party: 2 required, first approve stays pending, second resolves
- Multi-party: deny on first deny regardless of approval count
- Approval decisions array tracks each actor's decision
- Create with approvers_required from action_config
- to_json includes multi-party fields
"""

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
# API-level: verify response shape includes multi-party fields
# ---------------------------------------------------------------------------


def test_approval_response_includes_multi_party_fields(client):
    """GET approval shows approvers_required, current_approvals, decisions."""
    # Create a require_approval policy with approvers_required=2.
    pol = client.post(
        "/v1/policies",
        headers=_auth(DEV_KEY),
        json={
            "name": f"mp-{uuid.uuid4().hex[:8]}",
            "match_expression": "true",
            "action": "require_approval",
            "action_config": {"approvers_required": 2, "timeout_seconds": 600},
        },
    )
    assert pol.status_code == 201
    policy_id = pol.json()["id"]
    try:
        # List approvals — just check shape is valid.
        resp = client.get("/v1/approvals", headers=_auth(DEV_KEY))
        assert resp.status_code == 200
        for a in resp.json()["approvals"]:
            assert "approvers_required" in a
            assert "current_approvals" in a
            assert "approval_decisions" in a
    finally:
        client.delete(f"/v1/policies/{policy_id}", headers=_auth(DEV_KEY))


# ---------------------------------------------------------------------------
# Repository-level: multi-party approval logic
# ---------------------------------------------------------------------------


async def test_single_approver_backward_compat(session, isolated_project):
    """approvers_required=1 (default): one approve resolves."""
    from repositories.approvals import create_approval, resolve_approval

    approval = await create_approval(
        session, isolated_project,
        policy_id=uuid.uuid4(),
        timeout_seconds=300,
        approvers_required=1,
    )
    assert approval.approvers_required == 1
    assert approval.current_approvals == 0
    assert approval.status == "pending"

    resolved = await resolve_approval(
        session, isolated_project, approval.id,
        decision="approved", resolved_by="alice",
    )
    assert resolved is not None
    assert resolved.status == "approved"
    assert resolved.current_approvals == 1
    assert len(resolved.approval_decisions) == 1
    assert resolved.approval_decisions[0]["actor"] == "alice"
    assert resolved.approval_decisions[0]["decision"] == "approved"


async def test_multi_party_two_of_two(session, isolated_project):
    """approvers_required=2: first approve stays pending, second resolves."""
    from repositories.approvals import create_approval, resolve_approval

    approval = await create_approval(
        session, isolated_project,
        policy_id=uuid.uuid4(),
        timeout_seconds=300,
        approvers_required=2,
    )
    assert approval.approvers_required == 2

    # First approve — stays pending.
    after_first = await resolve_approval(
        session, isolated_project, approval.id,
        decision="approved", resolved_by="alice",
    )
    assert after_first is not None
    assert after_first.status == "pending"
    assert after_first.current_approvals == 1
    assert len(after_first.approval_decisions) == 1

    # Second approve — resolves.
    after_second = await resolve_approval(
        session, isolated_project, approval.id,
        decision="approved", resolved_by="bob",
    )
    assert after_second is not None
    assert after_second.status == "approved"
    assert after_second.current_approvals == 2
    assert len(after_second.approval_decisions) == 2
    assert after_second.resolved_at is not None


async def test_multi_party_deny_is_immediate_veto(session, isolated_project):
    """First deny kills approval regardless of count."""
    from repositories.approvals import create_approval, resolve_approval

    approval = await create_approval(
        session, isolated_project,
        policy_id=uuid.uuid4(),
        timeout_seconds=300,
        approvers_required=3,
    )

    # First actor approves.
    await resolve_approval(
        session, isolated_project, approval.id,
        decision="approved", resolved_by="alice",
    )

    # Second actor denies — immediate veto.
    denied = await resolve_approval(
        session, isolated_project, approval.id,
        decision="denied", resolved_by="bob",
    )
    assert denied is not None
    assert denied.status == "denied"
    assert denied.current_approvals == 1  # alice's approve counted
    assert len(denied.approval_decisions) == 2
    assert denied.approval_decisions[1]["decision"] == "denied"
    assert denied.resolved_by == "bob"


async def test_multi_party_cannot_resolve_after_denied(session, isolated_project):
    """After deny, further approvals return None (already resolved)."""
    from repositories.approvals import create_approval, resolve_approval

    approval = await create_approval(
        session, isolated_project,
        policy_id=uuid.uuid4(),
        timeout_seconds=300,
        approvers_required=2,
    )

    await resolve_approval(
        session, isolated_project, approval.id,
        decision="denied", resolved_by="alice",
    )

    result = await resolve_approval(
        session, isolated_project, approval.id,
        decision="approved", resolved_by="bob",
    )
    assert result is None  # already resolved


async def test_decisions_array_tracks_all_actors(session, isolated_project):
    """approval_decisions array records each actor with timestamp."""
    from repositories.approvals import create_approval, resolve_approval

    approval = await create_approval(
        session, isolated_project,
        policy_id=uuid.uuid4(),
        timeout_seconds=300,
        approvers_required=3,
    )

    await resolve_approval(
        session, isolated_project, approval.id,
        decision="approved", resolved_by="alice",
    )
    await resolve_approval(
        session, isolated_project, approval.id,
        decision="approved", resolved_by="bob",
    )
    final = await resolve_approval(
        session, isolated_project, approval.id,
        decision="approved", resolved_by="charlie",
    )

    assert final.status == "approved"
    assert len(final.approval_decisions) == 3
    actors = [d["actor"] for d in final.approval_decisions]
    assert actors == ["alice", "bob", "charlie"]
    for d in final.approval_decisions:
        assert "timestamp" in d
        assert d["decision"] == "approved"


async def test_to_json_includes_multi_party_fields(session, isolated_project):
    """to_json serializes approvers_required, current_approvals, decisions."""
    from repositories.approvals import create_approval

    approval = await create_approval(
        session, isolated_project,
        policy_id=uuid.uuid4(),
        timeout_seconds=60,
        approvers_required=5,
    )
    data = approval.to_json()
    assert data["approvers_required"] == 5
    assert data["current_approvals"] == 0
    assert data["approval_decisions"] == []
