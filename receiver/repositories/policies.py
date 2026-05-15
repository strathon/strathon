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

from models import Policy, PolicyMatch
from policies_eval import validate_expression
from schemas.policies import VALID_ACTIONS, PolicyRead

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
) -> PolicyRead:
    """Insert a policy. Validates action enum and CEL expression before write."""
    if action not in VALID_ACTIONS:
        raise ValueError(
            f"action must be one of {sorted(VALID_ACTIONS)}, got {action!r}"
        )
    # Raises PolicyExpressionError on malformed CEL — caller's HTTPException
    # handler turns this into a 400.
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
    )
    session.add(policy)
    # Flush so id and timestamps populate before we serialize. Commit happens
    # at the request boundary, not here.
    await session.flush()
    await session.refresh(policy)
    return PolicyRead.model_validate(policy)


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
    return PolicyRead.model_validate(policy) if policy is not None else None


async def delete_policy(
    session: AsyncSession,
    project_id: UUID,
    policy_id: UUID,
) -> bool:
    """Hard-delete a policy. Returns True iff a row was actually deleted."""
    stmt = delete(Policy).where(
        Policy.project_id == project_id,
        Policy.id == policy_id,
    )
    result = await session.execute(stmt)
    return bool(result.rowcount)


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
    """Append a row to policy_matches for audit.

    Never raises — failure to record a match must not break ingest. The
    surrounding caller (in main.py) already wraps this in try/except and
    swallows errors at the logger.exception() level. We mirror that here
    so the contract is honored regardless of which session this runs on.
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
    except Exception:
        logger.exception("failed to record policy match for policy %s", policy_id)
