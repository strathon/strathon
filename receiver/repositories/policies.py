"""Policy persistence operations.

Session-aware replacements for the raw-asyncpg CRUD functions previously
in receiver/policies.py. The CEL evaluator now lives in policies_eval.py
and the ingest-side matcher (evaluate_for_span) stays in policies.py.
Webhook delivery moved to the webhooks/ package in commit C1.

Update semantics:
    The `updated_at` column is set explicitly by update_policy. The
    previous asyncpg implementation also relied on the DB trigger
    `trg_projects_updated_at`-style setup; for the policies table the
    raw-SQL migration did NOT install such a trigger, so we must set
    updated_at ourselves on UPDATE. The original asyncpg code did this
    by including updated_at in the SET clause via the RETURNING value;
    we mirror that here.

Transaction model (as documented in repositories/__init__.py):
    Functions never commit. The endpoint's `get_db_session` dependency
    owns the commit decision. Background tasks construct their own
    session and commit explicitly.
"""

from __future__ import annotations

import logging
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models import Policy, PolicyMatch, PolicyVersion
from policies_eval import validate_expression
from schemas.policies import VALID_ACTIONS, PolicyRead, validate_action_config

logger = logging.getLogger("strathon.receiver.repositories.policies")


# ---- Read paths ----------------------------------------------------------


async def list_policies(
    session: AsyncSession,
    project_id: UUID,
    only_enabled: bool = False,
) -> list[PolicyRead]:
    """List policies for a project, ordered priority DESC then name ASC.

    The ordering matters: when multiple policies could match a span, the
    enforcer applies them highest-priority first. Name is the tiebreaker
    so the ordering is stable when two policies share a priority.
    """
    stmt = select(Policy).where(Policy.project_id == project_id)
    if only_enabled:
        stmt = stmt.where(Policy.enabled.is_(True))
    stmt = stmt.order_by(Policy.priority.desc(), Policy.name.asc())

    result = await session.execute(stmt)
    policies = result.scalars().all()
    return [PolicyRead.model_validate(p) for p in policies]


async def get_policy(
    session: AsyncSession,
    project_id: UUID,
    policy_id: UUID,
) -> Optional[PolicyRead]:
    """Fetch a single policy by id, scoped to a project. None if not found."""
    stmt = select(Policy).where(
        Policy.project_id == project_id,
        Policy.id == policy_id,
    )
    result = await session.execute(stmt)
    policy = result.scalar_one_or_none()
    return PolicyRead.model_validate(policy) if policy is not None else None


# ---- Write paths ---------------------------------------------------------


async def create_policy(
    session: AsyncSession,
    project_id: UUID,
    name: str,
    match_expression: str,
    action: str,
    description: Optional[str] = None,
    action_config: Optional[dict[str, Any]] = None,
    applies_to: Optional[list[str]] = None,
    enabled: bool = True,
    priority: int = 0,
    shadow: bool = False,
) -> PolicyRead:
    """Insert a policy. Validates action enum and CEL expression before write."""
    if action not in VALID_ACTIONS:
        raise ValueError(
            f"action must be one of {sorted(VALID_ACTIONS)}, got {action!r}"
        )
    validate_action_config(action, action_config or {})
    validate_expression(match_expression)

    policy = Policy(
        project_id=project_id,
        name=name,
        description=description,
        match_expression=match_expression,
        action=action,
        action_config=action_config or {},
        applies_to=list(applies_to or []),
        enabled=enabled,
        priority=priority,
        shadow=shadow,
    )
    session.add(policy)
    # Flush so id and timestamps populate before we serialize. Commit happens
    # at the request boundary, not here.
    await session.flush()
    await session.refresh(policy)
    result = PolicyRead.model_validate(policy)
    await _capture_version(session, result, "create")
    return result


async def update_policy(
    session: AsyncSession,
    project_id: UUID,
    policy_id: UUID,
    **changes: Any,
) -> Optional[PolicyRead]:
    """Apply partial updates. Unknown keys ignored, None values skipped.

    Returns the updated row, or None if the policy wasn't found.
    """
    allowed = {
        "name",
        "description",
        "match_expression",
        "action",
        "action_config",
        "applies_to",
        "enabled",
        "priority",
        "shadow",
    }
    updates = {k: v for k, v in changes.items() if k in allowed and v is not None}
    if not updates:
        # Nothing to change — return the current row so the endpoint can
        # still respond with the unchanged policy.
        return await get_policy(session, project_id, policy_id)

    if "action" in updates and updates["action"] not in VALID_ACTIONS:
        raise ValueError(
            f"action must be one of {sorted(VALID_ACTIONS)}, got {updates['action']!r}"
        )
    # Validate action_config shape against either the new action (when
    # action is being changed in this same PATCH) or the existing action
    # (when only action_config is being changed). Either path that ends
    # in a row with action="throttle" needs a well-formed config.
    if "action_config" in updates:
        if "action" in updates:
            effective_action = updates["action"]
        else:
            existing = await get_policy(session, project_id, policy_id)
            effective_action = existing.action if existing is not None else ""
        validate_action_config(effective_action, updates["action_config"] or {})
    if "match_expression" in updates:
        validate_expression(updates["match_expression"])

    # Coerce applies_to to list (asyncpg accepted iterables; SQLAlchemy ARRAY
    # is strict on Python type).
    if "applies_to" in updates:
        updates["applies_to"] = list(updates["applies_to"])

    # Always bump updated_at on a real update. The policies table has no
    # DB-side trigger for this (unlike projects/budgets/project_settings),
    # so the app is responsible.
    updates["updated_at"] = func.now()

    stmt = (
        update(Policy)
        .where(Policy.project_id == project_id, Policy.id == policy_id)
        .values(**updates)
        .returning(Policy)
    )
    result = await session.execute(stmt)
    policy = result.scalar_one_or_none()
    if policy is None:
        return None
    updated = PolicyRead.model_validate(policy)
    await _capture_version(session, updated, "update")
    return updated


async def delete_policy(
    session: AsyncSession,
    project_id: UUID,
    policy_id: UUID,
) -> bool:
    """Hard-delete a policy. Returns True iff a row was actually deleted.

    Captures a final 'delete' version snapshot before removal.
    """
    # Capture the policy state before deletion for the version log.
    before = await get_policy(session, project_id, policy_id)
    if before is not None:
        await _capture_version(session, before, "delete")

    stmt = delete(Policy).where(
        Policy.project_id == project_id,
        Policy.id == policy_id,
    )
    result = await session.execute(stmt)
    return bool(result.rowcount)  # type: ignore[attr-defined]


# ---- Audit trail --------------------------------------------------------


async def record_match(
    session: AsyncSession,
    policy_id: UUID,
    project_id: UUID,
    trace_id: bytes,
    span_id: bytes,
    action: str,
    action_outcome: str,
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    """Append a row to policy_matches for audit and update policy metrics.

    Atomically increments match_count and sets last_matched_at on the
    policy row so operators can see which policies fire most often
    without querying policy_matches.

    Never raises — failure to record a match must not break ingest.
    """
    try:
        stmt = insert(PolicyMatch).values(
            policy_id=policy_id,
            project_id=project_id,
            trace_id=trace_id,
            span_id=span_id,
            action=action,
            action_outcome=action_outcome,
            match_metadata=metadata or {},
        )
        await session.execute(stmt)

        # Update match_count + last_matched_at on the policy row.
        # Single atomic UPDATE, no SELECT needed.
        from sqlalchemy import func as sa_func, update
        await session.execute(
            update(Policy)
            .where(Policy.id == policy_id)
            .values(
                match_count=Policy.match_count + 1,
                last_matched_at=sa_func.now(),
            )
        )
    except Exception:
        logger.exception("failed to record policy match for policy %s", policy_id)


# ---- Version tracking --------------------------------------------------------


async def _next_version(session: AsyncSession, policy_id: UUID) -> int:
    """Get the next version number for a policy."""
    from sqlalchemy import text
    result = await session.execute(
        text(
            "SELECT COALESCE(MAX(version), 0) + 1 "
            "FROM policy_versions WHERE policy_id = :pid"
        ),
        {"pid": policy_id},
    )
    return result.scalar_one()


async def _capture_version(
    session: AsyncSession,
    policy: PolicyRead,
    change_type: str,
) -> None:
    """Insert a version snapshot for a policy."""
    version = await _next_version(session, policy.id)
    session.add(PolicyVersion(
        policy_id=policy.id,
        project_id=policy.project_id,
        version=version,
        name=policy.name,
        description=policy.description,
        match_expression=policy.match_expression,
        action=policy.action,
        action_config=policy.action_config,
        applies_to=list(policy.applies_to),
        enabled=policy.enabled,
        priority=policy.priority,
        change_type=change_type,
    ))
    await session.flush()


async def list_versions(
    session: AsyncSession,
    project_id: UUID,
    policy_id: UUID,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List version history for a policy, newest first."""
    from sqlalchemy import text
    result = await session.execute(
        text(
            "SELECT * FROM policy_versions "
            "WHERE policy_id = :pid AND project_id = :proj "
            "ORDER BY version DESC "
            "LIMIT :lim"
        ),
        {"pid": policy_id, "proj": project_id, "lim": limit},
    )
    return [dict(r) for r in result.mappings().all()]


async def get_version(
    session: AsyncSession,
    project_id: UUID,
    policy_id: UUID,
    version: int,
) -> dict[str, Any] | None:
    """Get a specific version of a policy."""
    from sqlalchemy import text
    result = await session.execute(
        text(
            "SELECT * FROM policy_versions "
            "WHERE policy_id = :pid AND project_id = :proj "
            "AND version = :ver "
            "LIMIT 1"
        ),
        {"pid": policy_id, "proj": project_id, "ver": version},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None
