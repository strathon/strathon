"""Tests for repositories/webhook_signing_keys.py.

Covers the row-level CRUD operations without going through the HTTP
surface. The HTTP tests live in test_webhook_signing_keys_api.py and
exercise scopes and response shape.

These tests share a session that rolls back at teardown, so every row
they insert disappears between tests. We never leak signing material
between test cases.
"""

from __future__ import annotations

import os
import sys

_RECEIVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _RECEIVER_DIR)

from repositories import webhook_signing_keys as keys_repo  # noqa: E402
from webhooks.signing import hash_secret  # noqa: E402


# ---- create_key ---------------------------------------------------------


async def test_create_key_inserts_active_row(session, isolated_project):
    result = await keys_repo.create_key(session, isolated_project)
    assert result.row.project_id == isolated_project
    assert result.row.revoked_at is None
    assert len(result.row.prefix) == 4
    assert result.plaintext.startswith("whsec_")


async def test_create_key_returns_plaintext_only_once(session, isolated_project):
    """The plaintext is in the CreateSigningKeyResult; no subsequent
    repository query exposes it. This is the contract."""
    r = await keys_repo.create_key(session, isolated_project)
    plaintext = r.plaintext

    # Re-fetch the row via list_keys: plaintext is NOT in the DTO.
    rows = await keys_repo.list_keys(session, isolated_project)
    assert len(rows) == 1
    # SigningKeyRow has no plaintext field at all
    assert not hasattr(rows[0], "plaintext")
    assert not hasattr(rows[0], "secret_hash")
    # And the plaintext from create_key DOES verify against the row's hash
    # via the signing.hash_secret helper.
    from models.webhooks import WebhookSigningKey
    from sqlalchemy import select
    db_row = await session.scalar(
        select(WebhookSigningKey).where(WebhookSigningKey.id == r.row.id)
    )
    assert bytes(db_row.secret_hash) == hash_secret(plaintext)


async def test_create_two_keys_for_same_project_both_active(session, isolated_project):
    r1 = await keys_repo.create_key(session, isolated_project)
    r2 = await keys_repo.create_key(session, isolated_project)
    assert r1.row.id != r2.row.id
    assert r1.plaintext != r2.plaintext
    rows = await keys_repo.list_keys(session, isolated_project)
    assert len(rows) == 2
    assert all(r.revoked_at is None for r in rows)


# ---- list_keys ----------------------------------------------------------


async def test_list_keys_default_hides_revoked(session, isolated_project):
    r = await keys_repo.create_key(session, isolated_project)
    await keys_repo.revoke_key(session, r.row.id, isolated_project)
    rows = await keys_repo.list_keys(session, isolated_project)
    assert rows == []


async def test_list_keys_with_include_revoked_shows_all(session, isolated_project):
    active = await keys_repo.create_key(session, isolated_project)
    to_revoke = await keys_repo.create_key(session, isolated_project)
    await keys_repo.revoke_key(session, to_revoke.row.id, isolated_project)

    rows = await keys_repo.list_keys(session, isolated_project, include_revoked=True)
    assert len(rows) == 2
    # Active sorts before revoked
    assert rows[0].id == active.row.id
    assert rows[0].revoked_at is None
    assert rows[1].id == to_revoke.row.id
    assert rows[1].revoked_at is not None


async def test_list_keys_scopes_to_project(session, isolated_project, async_engine):
    """A key in project A must not appear in a list for project B.
    Defense against cross-project leakage."""
    import uuid
    from sqlalchemy import insert, delete
    from sqlalchemy.ext.asyncio import AsyncSession
    from models import Project, ProjectSettings

    # Create a second committed project, write a key in it, then assert
    # the test's session (looking at isolated_project) does not see it.
    other_project_id = uuid.uuid4()
    async with AsyncSession(bind=async_engine, expire_on_commit=False) as s:
        await s.execute(insert(Project).values(
            id=other_project_id, name="other-proj",
            slug=f"other-{other_project_id.hex[:8]}",
        ))
        await s.execute(insert(ProjectSettings).values(project_id=other_project_id))
        await keys_repo.create_key(s, other_project_id)
        await s.commit()

    try:
        # Test's isolated_project should see zero keys
        rows = await keys_repo.list_keys(session, isolated_project)
        assert rows == []
        # The other project sees one
        rows_other = await keys_repo.list_keys(session, other_project_id)
        assert len(rows_other) == 1
    finally:
        async with AsyncSession(bind=async_engine, expire_on_commit=False) as s:
            from models.webhooks import WebhookSigningKey
            await s.execute(
                delete(WebhookSigningKey).where(
                    WebhookSigningKey.project_id == other_project_id
                )
            )
            await s.execute(
                delete(ProjectSettings).where(
                    ProjectSettings.project_id == other_project_id
                )
            )
            await s.execute(delete(Project).where(Project.id == other_project_id))
            await s.commit()


# ---- revoke_key ---------------------------------------------------------


async def test_revoke_key_sets_revoked_at(session, isolated_project):
    r = await keys_repo.create_key(session, isolated_project)
    revoked = await keys_repo.revoke_key(session, r.row.id, isolated_project)
    assert revoked is not None
    assert revoked.revoked_at is not None
    assert revoked.id == r.row.id


async def test_revoke_key_idempotent(session, isolated_project):
    """Two revokes return the same row, with revoked_at set to the
    first revocation's timestamp (not updated on the second call)."""
    r = await keys_repo.create_key(session, isolated_project)
    first = await keys_repo.revoke_key(session, r.row.id, isolated_project)
    second = await keys_repo.revoke_key(session, r.row.id, isolated_project)
    assert first.revoked_at == second.revoked_at


async def test_revoke_key_unknown_id_returns_none(session, isolated_project):
    import uuid
    nonexistent = uuid.uuid4()
    result = await keys_repo.revoke_key(session, nonexistent, isolated_project)
    assert result is None


async def test_revoke_key_wrong_project_returns_none(session, isolated_project, async_engine):
    """Revoking a key with the wrong project_id returns None, even if
    the id exists in another project. This is the cross-project leak
    defense at the repository layer."""
    import uuid
    from sqlalchemy import insert, delete
    from sqlalchemy.ext.asyncio import AsyncSession
    from models import Project, ProjectSettings
    from models.webhooks import WebhookSigningKey

    other_project_id = uuid.uuid4()
    other_key_id = None
    async with AsyncSession(bind=async_engine, expire_on_commit=False) as s:
        await s.execute(insert(Project).values(
            id=other_project_id, name="other",
            slug=f"other-{other_project_id.hex[:8]}",
        ))
        await s.execute(insert(ProjectSettings).values(project_id=other_project_id))
        r = await keys_repo.create_key(s, other_project_id)
        other_key_id = r.row.id
        await s.commit()

    try:
        # Try to revoke the other project's key from the test session
        # using isolated_project (the wrong project). Must return None.
        result = await keys_repo.revoke_key(session, other_key_id, isolated_project)
        assert result is None
    finally:
        async with AsyncSession(bind=async_engine, expire_on_commit=False) as s:
            await s.execute(
                delete(WebhookSigningKey).where(
                    WebhookSigningKey.project_id == other_project_id
                )
            )
            await s.execute(
                delete(ProjectSettings).where(
                    ProjectSettings.project_id == other_project_id
                )
            )
            await s.execute(delete(Project).where(Project.id == other_project_id))
            await s.commit()


# ---- find_project_for_secret -------------------------------------------


async def test_find_project_for_secret_returns_project_id_for_active_key(
    session, isolated_project,
):
    r = await keys_repo.create_key(session, isolated_project)
    found = await keys_repo.find_project_for_secret(session, r.plaintext)
    assert found == isolated_project


async def test_find_project_for_secret_skips_revoked_keys(session, isolated_project):
    r = await keys_repo.create_key(session, isolated_project)
    await keys_repo.revoke_key(session, r.row.id, isolated_project)
    found = await keys_repo.find_project_for_secret(session, r.plaintext)
    assert found is None


async def test_find_project_for_secret_unknown_returns_none(session, isolated_project):
    found = await keys_repo.find_project_for_secret(
        session, "whsec_definitely_not_in_the_db",
    )
    assert found is None


# ---- list_active_keys_all_projects -------------------------------------


async def test_list_active_keys_all_projects_returns_active_only(
    session, isolated_project,
):
    active = await keys_repo.create_key(session, isolated_project)
    revoked_one = await keys_repo.create_key(session, isolated_project)
    await keys_repo.revoke_key(session, revoked_one.row.id, isolated_project)

    rows = await keys_repo.list_active_keys_all_projects(session)
    # Should contain the active one for our project (might contain others
    # from previous tests if they leaked; check membership not equality)
    active_for_us = [r for r in rows if r[0] == isolated_project]
    assert len(active_for_us) == 1
    assert active_for_us[0][1] == hash_secret(active.plaintext)
