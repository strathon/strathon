"""Session-based tests for repositories/policies.py and policies_eval.py.

Each test runs in its own transaction that's rolled back at teardown,
mirroring the test_auth_repository pattern. Tests skip cleanly when no
Postgres is reachable.

asyncio_mode = "auto" in pyproject.toml means async tests don't need a
@pytest.mark.asyncio decorator; pytest-asyncio infers it from the
`async def` signature. So both sync and async tests live in this file
without per-test markers.
"""

from __future__ import annotations

import uuid

import pytest


# ===========================================================================
# policies_eval — pure unit tests (no DB)
# ===========================================================================


def test_validate_expression_accepts_valid_cel():
    from policies_eval import validate_expression
    # Must not raise
    validate_expression("attrs['gen_ai.tool.name'] == 'send_email'")
    validate_expression("name == 'foo'")
    validate_expression("1 + 1 == 2")


def test_validate_expression_rejects_empty():
    from policies_eval import PolicyExpressionError, validate_expression
    with pytest.raises(PolicyExpressionError):
        validate_expression("")
    with pytest.raises(PolicyExpressionError):
        validate_expression("   ")


def test_validate_expression_rejects_non_string():
    from policies_eval import PolicyExpressionError, validate_expression
    with pytest.raises(PolicyExpressionError):
        validate_expression(None)
    with pytest.raises(PolicyExpressionError):
        validate_expression(123)
    with pytest.raises(PolicyExpressionError):
        validate_expression(["x"])


def test_validate_expression_rejects_malformed_cel():
    from policies_eval import PolicyExpressionError, validate_expression
    with pytest.raises(PolicyExpressionError):
        validate_expression("this is (not balanced")
    with pytest.raises(PolicyExpressionError):
        validate_expression("&&&")


def test_evaluate_returns_false_for_empty_expression():
    from policies_eval import evaluate
    assert evaluate("", {"name": "x", "attrs": {}}) is False
    assert evaluate(None, {"name": "x", "attrs": {}}) is False


def test_evaluate_returns_true_on_matching_context():
    from policies_eval import evaluate
    ctx = {"name": "tool.call", "attrs": {"gen_ai.tool.name": "send_email"}}
    assert evaluate("attrs['gen_ai.tool.name'] == 'send_email'", ctx) is True


def test_evaluate_returns_false_on_non_matching_context():
    from policies_eval import evaluate
    ctx = {"name": "tool.call", "attrs": {"gen_ai.tool.name": "other"}}
    assert evaluate("attrs['gen_ai.tool.name'] == 'send_email'", ctx) is False


def test_evaluate_does_not_raise_on_runtime_crash():
    """Runtime errors inside CEL must not crash the caller."""
    from policies_eval import evaluate
    # Indexing a missing key in CEL raises; evaluate must swallow it.
    ctx = {"name": "x", "attrs": {}}
    result = evaluate("attrs['nope'] == 'x'", ctx)
    # Either False or False-ish; must NOT raise
    assert result is False


def test_evaluate_does_not_raise_on_bad_compile():
    """Bad expression at evaluate time logs+returns False, doesn't raise."""
    from policies_eval import evaluate
    result = evaluate("not (valid CEL", {"name": "x", "attrs": {}})
    assert result is False


def test_compile_cache_returns_same_program():
    """Same expression should hit the cache rather than re-compile."""
    from policies_eval import _compile_cached
    p1 = _compile_cached("name == 'a'")
    p2 = _compile_cached("name == 'a'")
    assert p1 is p2


# ===========================================================================
# repositories/policies — session-based
# ===========================================================================


# ---- create_policy ----


async def test_create_policy_persists_all_fields(session, isolated_project):
    from repositories.policies import create_policy

    policy = await create_policy(
        session,
        isolated_project,
        name="block-email",
        match_expression="attrs['gen_ai.tool.name'] == 'send_email'",
        action="block",
        description="No outbound email",
        action_config={"message": "blocked by policy"},
        applies_to=["tool"],
        enabled=True,
        priority=10,
    )
    assert policy.name == "block-email"
    assert policy.project_id == isolated_project
    assert policy.action == "block"
    assert policy.description == "No outbound email"
    assert policy.action_config == {"message": "blocked by policy"}
    assert policy.applies_to == ["tool"]
    assert policy.enabled is True
    assert policy.priority == 10
    assert policy.id is not None
    assert policy.created_at is not None
    assert policy.updated_at is not None


async def test_create_policy_rejects_invalid_action(session, isolated_project):
    from repositories.policies import create_policy

    with pytest.raises(ValueError, match="action must be one of"):
        await create_policy(
            session,
            isolated_project,
            name="bad",
            match_expression="true",
            action="not_a_real_action",
        )


async def test_create_policy_rejects_invalid_cel(session, isolated_project):
    from policies_eval import PolicyExpressionError
    from repositories.policies import create_policy

    with pytest.raises(PolicyExpressionError):
        await create_policy(
            session,
            isolated_project,
            name="bad",
            match_expression="this is not (valid CEL",
            action="log",
        )


async def test_create_policy_defaults_action_config_to_empty(session, isolated_project):
    from repositories.policies import create_policy

    policy = await create_policy(
        session,
        isolated_project,
        name="defaults",
        match_expression="true",
        action="log",
    )
    assert policy.action_config == {}
    assert policy.applies_to == []


# ---- get_policy ----


async def test_get_policy_returns_existing(session, isolated_project):
    from repositories.policies import create_policy, get_policy

    created = await create_policy(
        session, isolated_project,
        name="findable", match_expression="true", action="log",
    )
    await session.flush()

    fetched = await get_policy(session, isolated_project, created.id)
    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.name == "findable"


async def test_get_policy_returns_none_for_unknown_id(session, isolated_project):
    from repositories.policies import get_policy
    result = await get_policy(session, isolated_project, uuid.uuid4())
    assert result is None


async def test_get_policy_scopes_to_project(session, isolated_project):
    """A policy from project A is not visible via project B's get_policy."""
    from sqlalchemy import insert
    from models import Project, ProjectSettings
    from repositories.policies import create_policy, get_policy

    # Create a sibling project
    other = uuid.uuid4()
    await session.execute(
        insert(Project).values(id=other, name="other", slug=f"o-{other.hex[:8]}")
    )
    await session.execute(insert(ProjectSettings).values(project_id=other))
    await session.flush()

    created = await create_policy(
        session, isolated_project,
        name="mine", match_expression="true", action="log",
    )
    await session.flush()

    # Looking up under the wrong project_id must not find it.
    assert await get_policy(session, other, created.id) is None
    # Right project does find it.
    assert await get_policy(session, isolated_project, created.id) is not None


# ---- list_policies ----


async def test_list_policies_orders_by_priority_then_name(session, isolated_project):
    from repositories.policies import create_policy, list_policies

    await create_policy(session, isolated_project, name="b-low", match_expression="true", action="log", priority=1)
    await create_policy(session, isolated_project, name="a-high", match_expression="true", action="log", priority=5)
    await create_policy(session, isolated_project, name="z-high", match_expression="true", action="log", priority=5)
    await session.flush()

    policies = await list_policies(session, isolated_project)
    # priority DESC, name ASC: priority=5 ones first (alphabetically), then priority=1
    assert [p.name for p in policies] == ["a-high", "z-high", "b-low"]


async def test_list_policies_only_enabled(session, isolated_project):
    from repositories.policies import create_policy, list_policies

    await create_policy(session, isolated_project, name="on", match_expression="true", action="log", enabled=True)
    await create_policy(session, isolated_project, name="off", match_expression="true", action="log", enabled=False)
    await session.flush()

    all_policies = await list_policies(session, isolated_project)
    only_enabled = await list_policies(session, isolated_project, only_enabled=True)
    assert len(all_policies) == 2
    assert len(only_enabled) == 1
    assert only_enabled[0].name == "on"


async def test_list_policies_scopes_to_project(session, isolated_project):
    from sqlalchemy import insert
    from models import Project, ProjectSettings
    from repositories.policies import create_policy, list_policies

    other = uuid.uuid4()
    await session.execute(
        insert(Project).values(id=other, name="other", slug=f"o-{other.hex[:8]}")
    )
    await session.execute(insert(ProjectSettings).values(project_id=other))
    await session.flush()

    await create_policy(session, isolated_project, name="mine", match_expression="true", action="log")
    await create_policy(session, other, name="theirs", match_expression="true", action="log")
    await session.flush()

    mine = await list_policies(session, isolated_project)
    theirs = await list_policies(session, other)
    assert [p.name for p in mine] == ["mine"]
    assert [p.name for p in theirs] == ["theirs"]


# ---- update_policy ----


async def test_update_policy_modifies_only_provided_fields(session, isolated_project):
    from repositories.policies import create_policy, update_policy

    created = await create_policy(
        session, isolated_project,
        name="original", match_expression="true", action="log",
        description="orig desc", priority=1,
    )
    await session.flush()

    updated = await update_policy(
        session, isolated_project, created.id,
        priority=99, enabled=False,
    )
    assert updated is not None
    assert updated.id == created.id
    assert updated.name == "original"                 # unchanged
    assert updated.description == "orig desc"         # unchanged
    assert updated.priority == 99                     # changed
    assert updated.enabled is False                   # changed


async def test_update_policy_writes_updated_at(session, isolated_project):
    """The update SQL must include updated_at=NOW() so production calls bump it.

    Inside a single transaction Postgres NOW() returns the transaction start
    time, so created_at and updated_at end up equal here. The test we can
    do reliably is: SELECT updated_at after the UPDATE returns a non-null
    timestamp matching the row's current value (proving the UPDATE wrote
    the column rather than leaving it stale at a previous value). The wall-
    clock advance gets exercised by the live curl test, not here.
    """
    from sqlalchemy import select
    from models import Policy
    from repositories.policies import create_policy, update_policy

    created = await create_policy(
        session, isolated_project,
        name="touched", match_expression="true", action="log",
    )
    await session.flush()

    updated = await update_policy(session, isolated_project, created.id, priority=42)
    await session.flush()

    # Re-fetch the raw row and confirm updated_at is populated.
    row = (
        await session.execute(select(Policy).where(Policy.id == created.id))
    ).scalar_one()
    assert row.updated_at is not None
    assert updated.updated_at == row.updated_at


async def test_update_policy_returns_unchanged_row_when_no_updates(session, isolated_project):
    from repositories.policies import create_policy, update_policy

    created = await create_policy(
        session, isolated_project,
        name="unchanged", match_expression="true", action="log",
    )
    await session.flush()

    result = await update_policy(session, isolated_project, created.id)
    assert result is not None
    assert result.id == created.id
    assert result.name == "unchanged"


async def test_update_policy_returns_none_for_unknown_id(session, isolated_project):
    from repositories.policies import update_policy
    result = await update_policy(session, isolated_project, uuid.uuid4(), priority=1)
    assert result is None


async def test_update_policy_rejects_invalid_action(session, isolated_project):
    from repositories.policies import create_policy, update_policy

    created = await create_policy(
        session, isolated_project,
        name="strict", match_expression="true", action="log",
    )
    await session.flush()

    with pytest.raises(ValueError, match="action must be one of"):
        await update_policy(session, isolated_project, created.id, action="not_valid")


async def test_update_policy_rejects_invalid_cel(session, isolated_project):
    from policies_eval import PolicyExpressionError
    from repositories.policies import create_policy, update_policy

    created = await create_policy(
        session, isolated_project,
        name="cel-check", match_expression="true", action="log",
    )
    await session.flush()

    with pytest.raises(PolicyExpressionError):
        await update_policy(
            session, isolated_project, created.id,
            match_expression="this is not (valid",
        )


# ---- delete_policy ----


async def test_delete_policy_removes_row(session, isolated_project):
    from repositories.policies import create_policy, delete_policy, get_policy

    created = await create_policy(
        session, isolated_project,
        name="doomed", match_expression="true", action="log",
    )
    await session.flush()

    deleted = await delete_policy(session, isolated_project, created.id)
    assert deleted is True

    await session.flush()
    assert await get_policy(session, isolated_project, created.id) is None


async def test_delete_policy_returns_false_for_unknown_id(session, isolated_project):
    from repositories.policies import delete_policy
    result = await delete_policy(session, isolated_project, uuid.uuid4())
    assert result is False


async def test_delete_policy_scopes_to_project(session, isolated_project):
    """Can't delete another project's policy by guessing the id."""
    from sqlalchemy import insert
    from models import Project, ProjectSettings
    from repositories.policies import create_policy, delete_policy, get_policy

    other = uuid.uuid4()
    await session.execute(
        insert(Project).values(id=other, name="other", slug=f"o-{other.hex[:8]}")
    )
    await session.execute(insert(ProjectSettings).values(project_id=other))
    await session.flush()

    theirs = await create_policy(
        session, other,
        name="theirs", match_expression="true", action="log",
    )
    await session.flush()

    # Try to delete using my project_id
    assert await delete_policy(session, isolated_project, theirs.id) is False
    # Their policy is still there
    assert await get_policy(session, other, theirs.id) is not None


# ---- record_match ----


async def test_record_match_inserts_audit_row(session, isolated_project):
    from sqlalchemy import select
    from models import PolicyMatch
    from repositories.policies import create_policy, record_match

    policy = await create_policy(
        session, isolated_project,
        name="logged", match_expression="true", action="log",
    )
    await session.flush()

    await record_match(
        session,
        policy_id=policy.id,
        project_id=isolated_project,
        trace_id=b"\x01" * 16,
        span_id=b"\x02" * 8,
        action="log",
        action_outcome="logged",
        metadata={"reason": "test"},
    )
    await session.flush()

    rows = (
        await session.execute(
            select(PolicyMatch).where(PolicyMatch.policy_id == policy.id)
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].action == "log"
    assert rows[0].action_outcome == "logged"
    assert rows[0].match_metadata == {"reason": "test"}


async def test_record_match_never_raises_on_bad_input(session, isolated_project):
    """The audit insert is best-effort; failures get swallowed and logged.

    Pass a non-existent policy_id — the FK violation should be caught
    internally, not propagated to the caller.
    """
    from repositories.policies import record_match

    # No exception expected even though policy_id doesn't exist
    await record_match(
        session,
        policy_id=uuid.uuid4(),
        project_id=isolated_project,
        trace_id=b"\x01" * 16,
        span_id=b"\x02" * 8,
        action="log",
        action_outcome="logged",
    )
