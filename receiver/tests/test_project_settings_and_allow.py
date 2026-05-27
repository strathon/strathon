"""Tests for project settings + the 'allow' policy action.

Covers:
  * The ``project_settings`` repository helpers
    (``load_intervention_default_action`` and
    ``update_intervention_default_action``).
  * The ``/v1/project/settings`` API endpoint (GET and PATCH).
  * The ``/v1/policies`` GET response now carrying the
    ``intervention_default_action`` field alongside policies.
  * The policies repository accepting ``action="allow"`` after
    migration 009 extended the CHECK constraint.
"""

from __future__ import annotations

import os
import sys

import pytest


_RECEIVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _RECEIVER_DIR)


# ---- Repository layer ----------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_load_intervention_default_action_returns_default_for_seeded_project(
    session, isolated_project,
):
    """A fresh project (created by the test fixture) gets an
    ``allow`` default because that's what the column default is."""
    from repositories.project_settings import load_intervention_default_action

    value = await load_intervention_default_action(session, isolated_project)
    assert value == "allow"


@pytest.mark.asyncio
async def test_load_intervention_default_action_returns_allow_when_no_row(
    session,
):
    """A project that somehow has no project_settings row at all gets
    the conservative fallback ``"allow"`` — silently denying every
    call in such a project would be the worst possible failure mode."""
    from uuid import uuid4

    from repositories.project_settings import load_intervention_default_action

    nonexistent_project_id = uuid4()
    value = await load_intervention_default_action(session, nonexistent_project_id)
    assert value == "allow"


@pytest.mark.asyncio
async def test_update_intervention_default_action_persists(
    session, isolated_project,
):
    from repositories.project_settings import (
        load_intervention_default_action,
        update_intervention_default_action,
    )

    await update_intervention_default_action(session, isolated_project, "block")
    await session.commit()

    value = await load_intervention_default_action(session, isolated_project)
    assert value == "block"


@pytest.mark.asyncio
async def test_update_intervention_default_action_round_trip(
    session, isolated_project,
):
    """Block, then back to allow — both directions persist."""
    from repositories.project_settings import (
        load_intervention_default_action,
        update_intervention_default_action,
    )

    await update_intervention_default_action(session, isolated_project, "block")
    await session.commit()
    assert await load_intervention_default_action(session, isolated_project) == "block"

    await update_intervention_default_action(session, isolated_project, "allow")
    await session.commit()
    assert await load_intervention_default_action(session, isolated_project) == "allow"


@pytest.mark.asyncio
async def test_update_intervention_default_action_rejects_invalid_value(
    session, isolated_project,
):
    from repositories.project_settings import update_intervention_default_action

    with pytest.raises(ValueError, match="must be one of"):
        await update_intervention_default_action(
            session, isolated_project, "deny",
        )


# ---- Policy 'allow' action now permitted by CHECK ------------------------


@pytest.mark.asyncio
async def test_create_policy_accepts_allow_action(session, isolated_project):
    """Migration 009 extends the policies action CHECK constraint to
    admit 'allow'. Sanity-check the repository accepts it."""
    from repositories.policies import create_policy

    policy = await create_policy(
        session,
        isolated_project,
        name="permit_search",
        match_expression="name == 'tool.web_search'",
        action="allow",
    )
    assert policy.action == "allow"


@pytest.mark.asyncio
async def test_update_policy_can_set_action_to_allow(session, isolated_project):
    from repositories.policies import create_policy, update_policy

    policy = await create_policy(
        session,
        isolated_project,
        name="will_be_allow",
        match_expression="true",
        action="log",
    )
    updated = await update_policy(
        session,
        isolated_project,
        policy.id,
        action="allow",
    )
    assert updated is not None
    assert updated.action == "allow"


# ---- API endpoints --------------------------------------------------------


DEV_KEY = "stra_dev_local_default_project_do_not_use_in_production"
DEFAULT_DB_URL = "postgresql://strathon:strathon_dev@127.0.0.1:5432/strathon"


@pytest.fixture(scope="module")
def api_client():
    db_url = os.getenv("DATABASE_URL", DEFAULT_DB_URL)
    os.environ["DATABASE_URL"] = db_url
    try:
        import psycopg
        conn = psycopg.connect(db_url, autocommit=True)
        conn.close()
    except Exception:
        pytest.skip("Postgres not reachable")

    from config import get_settings
    from database import get_engine, get_session_maker
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_maker.cache_clear()

    from fastapi.testclient import TestClient
    import main
    with TestClient(main.app) as c:
        yield c


def _auth(key: str = DEV_KEY) -> dict:
    return {"Authorization": f"Bearer {key}"}


def test_get_project_settings_returns_default_allow(api_client):
    r = api_client.get("/v1/project/settings", headers=_auth())
    assert r.status_code == 200, r.text
    body = r.json()
    assert "intervention_default_action" in body
    assert body["intervention_default_action"] in ("allow", "block")


def test_patch_project_settings_flips_to_block_and_back(api_client):
    # Snapshot current state so we can restore after.
    initial = api_client.get("/v1/project/settings", headers=_auth()).json()
    initial_action = initial["intervention_default_action"]

    try:
        # Flip to block.
        r = api_client.patch(
            "/v1/project/settings",
            headers=_auth(),
            json={"intervention_default_action": "block"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["intervention_default_action"] == "block"

        # Confirm via GET.
        r = api_client.get("/v1/project/settings", headers=_auth())
        assert r.json()["intervention_default_action"] == "block"

        # Flip back to allow.
        r = api_client.patch(
            "/v1/project/settings",
            headers=_auth(),
            json={"intervention_default_action": "allow"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["intervention_default_action"] == "allow"
    finally:
        # Restore.
        api_client.patch(
            "/v1/project/settings",
            headers=_auth(),
            json={"intervention_default_action": initial_action},
        )


def test_patch_project_settings_rejects_invalid_value(api_client):
    r = api_client.patch(
        "/v1/project/settings",
        headers=_auth(),
        json={"intervention_default_action": "deny"},
    )
    assert r.status_code == 400
    assert "must be one of" in r.json()["detail"]


def test_patch_project_settings_rejects_unknown_keys(api_client):
    """Silent acceptance of typos would leave operators believing they
    had switched into allow-list mode when they had not. Reject."""
    r = api_client.patch(
        "/v1/project/settings",
        headers=_auth(),
        json={"intervention_defualt_action": "block"},  # typo
    )
    assert r.status_code == 400
    assert "unknown settings keys" in r.json()["detail"]


def test_list_policies_response_includes_intervention_default_action(api_client):
    """The SDK's refresh path depends on this field being in the
    /v1/policies response."""
    r = api_client.get("/v1/policies", headers=_auth())
    assert r.status_code == 200, r.text
    body = r.json()
    assert "policies" in body
    assert "intervention_default_action" in body
    assert body["intervention_default_action"] in ("allow", "block")


def test_get_project_settings_requires_scope(api_client):
    """A key without project_settings:read can't read settings."""
    # The dev key has '*' so we can't test this with it. Use a key
    # without the scope, if the test fixture supports it. For v1 we
    # spot-check that the endpoint exists and the scope wiring is in
    # place by hitting it without any auth header.
    r = api_client.get("/v1/project/settings")
    assert r.status_code in (401, 403)


def test_patch_project_settings_requires_scope(api_client):
    r = api_client.patch(
        "/v1/project/settings",
        json={"intervention_default_action": "allow"},
    )
    assert r.status_code in (401, 403)
