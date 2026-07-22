"""Project membership persistence.

Manages the project_members join table. Each (user_id, project_id) pair
has exactly one role. The role determines what the user can do via the
RBAC scope mapping in rbac.py.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models import Project, ProjectMember, User

logger = logging.getLogger("strathon.receiver.repositories.members")


async def get_member(
    session: AsyncSession,
    project_id: UUID,
    user_id: UUID,
) -> Optional[ProjectMember]:
    """Fetch a single membership. Returns None if not a member."""
    stmt = (
        select(ProjectMember)
        .where(ProjectMember.project_id == project_id)
        .where(ProjectMember.user_id == user_id)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_user_role(
    session: AsyncSession,
    project_id: UUID,
    user_id: UUID,
) -> Optional[str]:
    """Return the role string for a user in a LIVE project, or None.

    This is the auth boundary's membership check (auth.resolve_api_key,
    session branch). It deliberately joins to projects and requires
    deleted_at IS NULL: membership rows survive a project soft-delete,
    and without this filter a session holder could keep browsing and
    writing to a deleted project by sending its id in X-Project-Id --
    the session-auth twin of the revoke-keys-on-delete rule enforced in
    api/projects.delete_project. Fail closed.
    """
    stmt = (
        select(ProjectMember.role)
        .join(Project, ProjectMember.project_id == Project.id)
        .where(ProjectMember.project_id == project_id)
        .where(ProjectMember.user_id == user_id)
        .where(Project.deleted_at.is_(None))
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_members(
    session: AsyncSession,
    project_id: UUID,
) -> list[dict]:
    """List all members of a project with user details."""
    stmt = (
        select(
            ProjectMember.user_id,
            ProjectMember.role,
            ProjectMember.created_at,
            ProjectMember.invited_at,
            ProjectMember.accepted_at,
            User.email,
            User.display_name,
        )
        .join(User, ProjectMember.user_id == User.id)
        .where(ProjectMember.project_id == project_id)
        .order_by(ProjectMember.created_at.asc())
    )
    result = await session.execute(stmt)
    rows = result.all()
    return [
        {
            "user_id": str(r.user_id),
            "email": r.email,
            "display_name": r.display_name,
            "role": r.role,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "invited_at": r.invited_at.isoformat() if r.invited_at else None,
            "accepted_at": r.accepted_at.isoformat() if r.accepted_at else None,
        }
        for r in rows
    ]


async def add_member(
    session: AsyncSession,
    *,
    project_id: UUID,
    user_id: UUID,
    role: str,
    invited_by: Optional[UUID] = None,
) -> ProjectMember:
    """Add a user to a project with a role."""
    now = datetime.now(timezone.utc)
    member = ProjectMember(
        project_id=project_id,
        user_id=user_id,
        role=role,
        invited_at=now,
        accepted_at=now,  # auto-accept for API-based invites
        invited_by=invited_by,
    )
    session.add(member)
    await session.flush()
    return member


async def update_member_role(
    session: AsyncSession,
    project_id: UUID,
    user_id: UUID,
    new_role: str,
) -> bool:
    """Change a member's role. Returns True if a row was updated."""
    stmt = (
        update(ProjectMember)
        .where(ProjectMember.project_id == project_id)
        .where(ProjectMember.user_id == user_id)
        .values(role=new_role)
    )
    result = await session.execute(stmt)
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def remove_member(
    session: AsyncSession,
    project_id: UUID,
    user_id: UUID,
) -> bool:
    """Remove a member from a project. Returns True if a row was deleted."""
    stmt = (
        delete(ProjectMember)
        .where(ProjectMember.project_id == project_id)
        .where(ProjectMember.user_id == user_id)
    )
    result = await session.execute(stmt)
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def count_owners(session: AsyncSession, project_id: UUID) -> int:
    """Count owners of a project. Used to prevent removing the last owner."""
    stmt = (
        select(func.count())
        .select_from(ProjectMember)
        .where(ProjectMember.project_id == project_id)
        .where(ProjectMember.role == "owner")
    )
    result = await session.execute(stmt)
    return result.scalar_one()


async def get_user_projects(
    session: AsyncSession,
    user_id: UUID,
) -> list[dict]:
    """List all projects a user is a member of, with their role."""
    from models import Project

    stmt = (
        select(
            Project.id,
            Project.name,
            Project.slug,
            ProjectMember.role,
        )
        .join(Project, ProjectMember.project_id == Project.id)
        .where(ProjectMember.user_id == user_id)
        .where(Project.deleted_at.is_(None))
        .order_by(Project.name.asc())
    )
    result = await session.execute(stmt)
    rows = result.all()
    return [
        {
            "id": str(r.id),
            "name": r.name,
            "slug": r.slug,
            "role": r.role,
        }
        for r in rows
    ]
