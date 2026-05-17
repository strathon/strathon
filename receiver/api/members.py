"""Project membership management endpoints.

  GET    /v1/projects/{slug}/members              list members
  POST   /v1/projects/{slug}/members              add member
  PATCH  /v1/projects/{slug}/members/{user_id}    change role
  DELETE /v1/projects/{slug}/members/{user_id}    remove member

Access control:
  - List: any project member (viewer+)
  - Add/change/remove: owner or admin only, subject to role hierarchy
  - Cannot remove the last owner of a project
  - Can only assign roles strictly below your own rank
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import auth as auth_mod
from database import get_db_session
from models import Project
from rbac import VALID_ROLES, can_manage_role
from repositories import members as members_repo
from repositories import users as users_repo

from ._deps import require_role, require_scope

logger = logging.getLogger("strathon.receiver.api.members")

router = APIRouter(prefix="/v1/projects", tags=["members"])


# ---- Schemas -------------------------------------------------------------

class AddMemberRequest(BaseModel):
    email: str = Field(..., description="Email of the user to add")
    role: str = Field(..., description="Role to assign: admin, operator, or viewer")


class UpdateMemberRequest(BaseModel):
    role: str = Field(..., description="New role: admin, operator, or viewer")


# ---- Helpers -------------------------------------------------------------

async def _resolve_project(session, slug: str) -> Project:
    """Resolve a project slug to its model. Raises 404 if not found."""
    stmt = (
        select(Project)
        .where(Project.slug == slug)
        .where(Project.deleted_at.is_(None))
    )
    result = await session.execute(stmt)
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project not found: {slug}",
        )
    return project


# ---- Endpoints -----------------------------------------------------------


@router.get("/{slug}/members")
async def list_members(
    slug: str,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_TRACES_READ)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    """List all members of a project.

    Any authenticated user with at least traces:read scope can view
    the member list. This is intentionally permissive — knowing who
    has access is a transparency feature, not a secret.
    """
    project = await _resolve_project(session, slug)
    members = await members_repo.list_members(session, project.id)
    return {"members": members, "count": len(members)}


@router.post("/{slug}/members", status_code=status.HTTP_201_CREATED)
async def add_member(
    slug: str,
    body: AddMemberRequest,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_role("owner", "admin")
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    """Add a user to the project by email.

    The user must already have a registered account. Role hierarchy is
    enforced: you can only assign roles below your own rank.
    """
    project = await _resolve_project(session, slug)

    if body.role not in VALID_ROLES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid role: {body.role}. Must be one of: {sorted(VALID_ROLES)}",
        )

    # Cannot assign owner role via this endpoint
    if body.role == "owner":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Owner role can only be assigned by transferring ownership",
        )

    # Check role hierarchy (only for session auth where we have a role)
    if ctx.role and not can_manage_role(ctx.role, body.role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Cannot assign role '{body.role}' — must be below your role '{ctx.role}'",
        )

    # Find the user by email
    user = await users_repo.find_by_email(session, body.email.strip().lower())
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No registered user with email: {body.email}",
        )

    # Check if already a member
    existing = await members_repo.get_member(session, project.id, user.id)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"User {body.email} is already a member of this project",
        )

    await members_repo.add_member(
        session,
        project_id=project.id,
        user_id=user.id,
        role=body.role,
        invited_by=ctx.user_id,
    )

    # Audit trail

    await session.commit()

    return {
        "user_id": str(user.id),
        "email": user.email,
        "display_name": user.display_name,
        "role": body.role,
        "project_slug": slug,
    }


@router.patch("/{slug}/members/{user_id}")
async def update_member_role(
    slug: str,
    user_id: UUID,
    body: UpdateMemberRequest,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_role("owner", "admin")
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    """Change a member's role.

    Role hierarchy enforced: cannot promote someone to or above your
    own rank. Cannot change an owner's role (use ownership transfer).
    """
    project = await _resolve_project(session, slug)

    if body.role not in VALID_ROLES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid role: {body.role}. Must be one of: {sorted(VALID_ROLES)}",
        )

    if body.role == "owner":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Owner role can only be assigned by transferring ownership",
        )

    # Fetch the target member
    target = await members_repo.get_member(session, project.id, user_id)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User is not a member of this project",
        )

    # Cannot change an owner's role
    if target.role == "owner":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot change owner role. Use ownership transfer.",
        )

    # Role hierarchy check
    if ctx.role and not can_manage_role(ctx.role, target.role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Cannot modify user with role '{target.role}'",
        )
    if ctx.role and not can_manage_role(ctx.role, body.role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Cannot assign role '{body.role}'",
        )

    # Cannot change your own role
    if ctx.user_id and ctx.user_id == user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot change your own role",
        )

    await members_repo.update_member_role(session, project.id, user_id, body.role)
    await session.commit()

    return {
        "user_id": str(user_id),
        "role": body.role,
        "project_slug": slug,
    }


@router.delete("/{slug}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    slug: str,
    user_id: UUID,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_role("owner", "admin")
    ),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    """Remove a member from the project.

    Cannot remove the last owner. Cannot remove yourself (leave
    the project instead). Role hierarchy enforced.
    """
    project = await _resolve_project(session, slug)

    # Fetch the target member
    target = await members_repo.get_member(session, project.id, user_id)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User is not a member of this project",
        )

    # Cannot remove yourself
    if ctx.user_id and ctx.user_id == user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot remove yourself. Use the leave endpoint or ask another admin.",
        )

    # Cannot remove an owner (unless you're also an owner)
    if target.role == "owner" and ctx.role != "owner":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only an owner can remove another owner",
        )

    # Prevent removing the last owner
    if target.role == "owner":
        owner_count = await members_repo.count_owners(session, project.id)
        if owner_count <= 1:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot remove the last owner of a project",
            )

    # Role hierarchy check
    if ctx.role and not can_manage_role(ctx.role, target.role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Cannot remove user with role '{target.role}'",
        )

    await members_repo.remove_member(session, project.id, user_id)
    await session.commit()
