"""Session-based tests for repositories/auth.py.

Each test runs in its own transaction that's rolled back at teardown, so
the tests are self-contained and don't pollute each other. The
`isolated_project` fixture gives each test its own project_id so api_keys
created by one test can't be seen by another.
"""

from __future__ import annotations

import uuid



# ---- create_api_key -----------------------------------------------------


async def test_create_api_key_returns_raw_key_and_persists_hash(
    session, isolated_project
):
    from repositories.auth import create_api_key

    response = await create_api_key(session, isolated_project, name="test-key")

    # Raw key is returned
    assert response.raw_key.startswith("stra_")
    assert len(response.raw_key) > 40

    # Schema fields populated
    assert response.api_key.name == "test-key"
    assert response.api_key.project_id == isolated_project
    assert response.api_key.key_prefix == response.raw_key[:12]
    assert response.api_key.revoked_at is None
    assert response.api_key.last_used_at is None
    assert response.api_key.created_at is not None
    assert response.api_key.id is not None


async def test_create_api_key_generates_unique_keys(session, isolated_project):
    """Two calls must produce different keys (the entropy in the random tail)."""
    from repositories.auth import create_api_key

    r1 = await create_api_key(session, isolated_project, name="key-1")
    r2 = await create_api_key(session, isolated_project, name="key-2")

    assert r1.raw_key != r2.raw_key
    assert r1.api_key.id != r2.api_key.id


# ---- verify_token_and_touch (the hot path) ------------------------------


async def test_verify_token_returns_key_on_valid_match(session, isolated_project):
    from repositories.auth import create_api_key, verify_token_and_touch

    created = await create_api_key(session, isolated_project, name="hot-path")
    raw = created.raw_key

    key = await verify_token_and_touch(session, raw)
    assert key is not None
    assert key.id == created.api_key.id
    assert key.project_id == isolated_project


async def test_verify_token_returns_none_on_unknown_prefix(session):
    from repositories.auth import verify_token_and_touch

    # Random token that won't match any prefix in the DB
    key = await verify_token_and_touch(session, "stra_zzzzzzz" + "x" * 40)
    assert key is None


async def test_verify_token_returns_none_on_known_prefix_wrong_hash(
    session, isolated_project
):
    """Most subtle case: prefix collides but hash doesn't.

    Reproduces the exact scenario the constant-time compare exists for:
    an attacker who knows a valid prefix but is brute-forcing the secret.
    """
    from repositories.auth import create_api_key, verify_token_and_touch

    created = await create_api_key(session, isolated_project, name="prefix-test")
    real_raw = created.raw_key
    real_prefix = real_raw[:12]

    # Forge a token that shares the prefix but has a completely different tail.
    forged = real_prefix + "x" * (len(real_raw) - 12)
    assert forged != real_raw

    key = await verify_token_and_touch(session, forged)
    assert key is None


async def test_verify_token_returns_none_for_revoked_key(session, isolated_project):
    from repositories.auth import (
        create_api_key,
        revoke_api_key,
        verify_token_and_touch,
    )

    created = await create_api_key(session, isolated_project, name="revoke-test")
    await session.flush()

    # Revoke
    revoked = await revoke_api_key(session, created.api_key.id)
    assert revoked is True
    await session.flush()

    # Now the same raw key should not authenticate
    key = await verify_token_and_touch(session, created.raw_key)
    assert key is None


async def test_verify_token_updates_last_used_at(session, isolated_project):
    """The successful verify path bumps last_used_at."""
    from sqlalchemy import select

    from models import ApiKey
    from repositories.auth import create_api_key, verify_token_and_touch

    created = await create_api_key(session, isolated_project, name="touch-test")
    assert created.api_key.last_used_at is None

    await verify_token_and_touch(session, created.raw_key)
    await session.flush()

    # Re-fetch
    row = (
        await session.execute(
            select(ApiKey).where(ApiKey.id == created.api_key.id)
        )
    ).scalar_one()
    assert row.last_used_at is not None


# ---- list_api_keys ------------------------------------------------------


async def test_list_excludes_revoked_by_default(session, isolated_project):
    from repositories.auth import create_api_key, list_api_keys, revoke_api_key

    await create_api_key(session, isolated_project, name="active")
    to_revoke = await create_api_key(session, isolated_project, name="revoked")
    await revoke_api_key(session, to_revoke.api_key.id)
    await session.flush()

    keys = await list_api_keys(session, isolated_project)
    names = {k.name for k in keys}
    assert "active" in names
    assert "revoked" not in names


async def test_list_includes_revoked_when_asked(session, isolated_project):
    from repositories.auth import create_api_key, list_api_keys, revoke_api_key

    await create_api_key(session, isolated_project, name="active")
    to_revoke = await create_api_key(session, isolated_project, name="revoked")
    await revoke_api_key(session, to_revoke.api_key.id)
    await session.flush()

    keys = await list_api_keys(session, isolated_project, include_revoked=True)
    names = {k.name for k in keys}
    assert names == {"active", "revoked"}


async def test_list_returns_stable_order(session, isolated_project):
    """Verify ORDER BY clause produces deterministic results.

    With server-side `NOW()` defaults, multiple inserts in one transaction
    can share a created_at timestamp. The repository adds id as a stable
    tiebreaker so listings don't reshuffle between calls.
    """
    from repositories.auth import create_api_key, list_api_keys

    await create_api_key(session, isolated_project, name="first")
    await create_api_key(session, isolated_project, name="second")
    await create_api_key(session, isolated_project, name="third")
    await session.flush()

    listing1 = [k.id for k in await list_api_keys(session, isolated_project)]
    listing2 = [k.id for k in await list_api_keys(session, isolated_project)]
    assert listing1 == listing2
    assert len(listing1) == 3


async def test_list_scopes_to_project(session, isolated_project):
    """Keys from one project never leak into another's listing."""
    from sqlalchemy import insert

    from models import Project, ProjectSettings
    from repositories.auth import create_api_key, list_api_keys

    # Make a second project in the same transaction
    other_id = uuid.uuid4()
    other_slug = f"test-other-{other_id.hex[:8]}"
    await session.execute(
        insert(Project).values(id=other_id, name="Other", slug=other_slug)
    )
    await session.execute(insert(ProjectSettings).values(project_id=other_id))
    await session.flush()

    await create_api_key(session, isolated_project, name="mine")
    await create_api_key(session, other_id, name="theirs")
    await session.flush()

    mine = await list_api_keys(session, isolated_project)
    theirs = await list_api_keys(session, other_id)

    assert [k.name for k in mine] == ["mine"]
    assert [k.name for k in theirs] == ["theirs"]


# ---- revoke_api_key -----------------------------------------------------


async def test_revoke_marks_revoked_at(session, isolated_project):
    from sqlalchemy import select

    from models import ApiKey
    from repositories.auth import create_api_key, revoke_api_key

    created = await create_api_key(session, isolated_project, name="to-revoke")
    await session.flush()

    revoked = await revoke_api_key(session, created.api_key.id)
    assert revoked is True

    await session.flush()
    row = (
        await session.execute(
            select(ApiKey).where(ApiKey.id == created.api_key.id)
        )
    ).scalar_one()
    assert row.revoked_at is not None


async def test_revoke_returns_false_when_already_revoked(session, isolated_project):
    """Idempotency check — revoking twice doesn't claim success the second time."""
    from repositories.auth import create_api_key, revoke_api_key

    created = await create_api_key(session, isolated_project, name="double-revoke")
    await session.flush()

    first = await revoke_api_key(session, created.api_key.id)
    assert first is True

    await session.flush()

    second = await revoke_api_key(session, created.api_key.id)
    assert second is False


async def test_revoke_returns_false_for_unknown_key(session):
    """Revoking a key that doesn't exist returns False, not an exception."""
    from repositories.auth import revoke_api_key

    result = await revoke_api_key(session, uuid.uuid4())
    assert result is False
