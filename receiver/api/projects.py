"""Project management endpoints.

  POST   /v1/projects              create a project
  GET    /v1/projects              list projects
  GET    /v1/projects/{slug}       get by slug
  PATCH  /v1/projects/{slug}       update name
  DELETE /v1/projects/{slug}       soft delete

Scope: projects:manage (system-level, not project-scoped).

Creating a project also creates its project_settings row and mints
an initial API key with default SDK scopes. The response includes
the key plaintext so the operator can immediately start ingesting
traces.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from schemas.base import NulSafeStr
from sqlalchemy import insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID

import auth as auth_mod
from database import get_db_session
from models import ApiKey, Project, ProjectSettings
from repositories import members as members_repo

from schemas.responses import ProjectResponse, ProjectListResponse
from ._deps import require_scope


router = APIRouter(prefix="/v1/projects", tags=["projects"])


async def _caller_org_id(session: AsyncSession, ctx: "auth_mod.ApiKeyContext") -> UUID:
    """Resolve the organization the caller belongs to, from their current
    project. On self-host that is the single default organization. Every
    project read/write in this router scopes to this org so a projects:manage
    credential cannot reach, rename, or delete another tenant's projects.
    (Cloud will resolve org from org-scoped auth when that lands.)
    """
    org_row = await session.execute(
        select(Project.org_id).where(Project.id == ctx.project_id)
    )
    org_id = org_row.scalar_one_or_none()
    if org_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="could not resolve organization for the calling project",
        )
    return org_id

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{1,62}[a-z0-9]$")


class CreateProjectRequest(BaseModel):
    name: NulSafeStr = Field(..., min_length=1, max_length=200)
    slug: str = Field(
        ..., min_length=3, max_length=64,
        description="URL-safe identifier. Lowercase alphanumeric + hyphens.",
    )


class UpdateProjectRequest(BaseModel):
    name: Optional[NulSafeStr] = Field(default=None, min_length=1, max_length=200)


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ProjectResponse)
async def create_project(
    body: CreateProjectRequest,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_PROJECTS_MANAGE)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Create a new project with settings and an initial API key."""
    if not _SLUG_RE.match(body.slug):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "slug must be 3-64 chars, lowercase alphanumeric + hyphens, "
                "cannot start/end with hyphen"
            ),
        )

    # Resolve the organization this project belongs to: the same org as the
    # caller's current project. On self-host that is always the single
    # default organization. (Cloud will resolve org from the authenticated
    # organization context when org-scoped auth lands.)
    org_row = await session.execute(
        select(Project.org_id).where(Project.id == ctx.project_id)
    )
    org_id = org_row.scalar_one_or_none()
    if org_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="could not resolve organization for the calling project",
        )

    # Check uniqueness within the organization (slug is unique per-org).
    existing = await session.execute(
        select(Project.id).where(
            Project.slug == body.slug,
            Project.org_id == org_id,
            Project.deleted_at.is_(None),
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"project with slug {body.slug!r} already exists",
        )

    # Create project.
    project = Project(name=body.name, slug=body.slug, org_id=org_id)
    session.add(project)
    await session.flush()
    await session.refresh(project)

    # Create settings row.
    await session.execute(
        insert(ProjectSettings).values(project_id=project.id)
    )

    # Mint initial API key.
    raw_key, prefix, key_hash = auth_mod.generate_api_key()
    api_key = ApiKey(
        project_id=project.id,
        name=f"{body.slug}-default-key",
        key_prefix=prefix,
        key_hash=key_hash,
        scopes=list(auth_mod.DEFAULT_SDK_SCOPES),
    )
    session.add(api_key)
    await session.flush()

    # If a human (session auth) created this project, enroll them as its
    # owner so it appears in their membership list / project switcher.
    # API-key callers have no user_id and are skipped.
    if ctx.user_id is not None:
        await members_repo.add_member(
            session,
            project_id=project.id,
            user_id=ctx.user_id,
            role="owner",
        )

    return {
        "id": str(project.id),
        "name": project.name,
        "slug": project.slug,
        "api_key": raw_key,
        "api_key_scopes": list(auth_mod.DEFAULT_SDK_SCOPES),
    }


@router.get("", response_model=ProjectListResponse)
async def list_projects(
    include_deleted: bool = Query(default=False),
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_PROJECTS_MANAGE)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """List the projects in the caller's organization."""
    org_id = await _caller_org_id(session, ctx)
    stmt = select(Project).where(Project.org_id == org_id).order_by(Project.name)
    if not include_deleted:
        stmt = stmt.where(Project.deleted_at.is_(None))
    result = await session.execute(stmt)
    projects = result.scalars().all()
    return {
        "data": [
            {
                "id": str(p.id),
                "name": p.name,
                "slug": p.slug,
                "created_at": p.created_at.isoformat() if p.created_at else None,
                "deleted_at": p.deleted_at.isoformat() if p.deleted_at else None,
            }
            for p in projects
        ]
    }


@router.get("/{slug}")
async def get_project(
    slug: str,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_PROJECTS_MANAGE)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Get a project by slug."""
    org_id = await _caller_org_id(session, ctx)
    result = await session.execute(
        select(Project).where(
            Project.slug == slug,
            Project.org_id == org_id,
            Project.deleted_at.is_(None),
        )
    )
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")

    # Count active resources.
    counts = await session.execute(text(
        "SELECT "
        "(SELECT count(*) FROM api_keys WHERE project_id = :pid AND revoked_at IS NULL) AS api_keys, "
        "(SELECT count(*) FROM policies WHERE project_id = :pid) AS policies, "
        "(SELECT count(*) FROM traces WHERE project_id = :pid) AS traces"
    ), {"pid": project.id})
    row = counts.mappings().first()

    return {
        "id": str(project.id),
        "name": project.name,
        "slug": project.slug,
        "created_at": project.created_at.isoformat() if project.created_at else None,
        "resource_counts": dict(row) if row else {},
    }


@router.patch("/{slug}")
async def update_project(
    slug: str,
    body: UpdateProjectRequest,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_PROJECTS_MANAGE)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Update a project's name."""
    if body.name is None:
        raise HTTPException(status_code=400, detail="nothing to update")

    org_id = await _caller_org_id(session, ctx)
    result = await session.execute(
        update(Project)
        .where(
            Project.slug == slug,
            Project.org_id == org_id,
            Project.deleted_at.is_(None),
        )
        .values(name=body.name)
        .returning(Project)
    )
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return {
        "id": str(project.id),
        "name": project.name,
        "slug": project.slug,
    }


@router.delete("/{slug}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    slug: str,
    request: Request,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_PROJECTS_MANAGE)
    ),
    session: AsyncSession = Depends(get_db_session),
):
    """Soft-delete a project."""
    # Deleting a project is destructive; a stolen session cookie alone must not
    # be able to do it.
    from ._deps import enforce_reauth
    await enforce_reauth(request, ctx, session)

    from sqlalchemy import func, select
    org_id = await _caller_org_id(session, ctx)
    # Refuse to delete the last remaining project in the org -- an instance with
    # zero projects has no usable context. The caller must always have at least one.
    remaining = await session.execute(
        select(func.count())
        .select_from(Project)
        .where(Project.org_id == org_id, Project.deleted_at.is_(None))
    )
    if (remaining.scalar() or 0) <= 1:
        raise HTTPException(
            status_code=409,
            detail="Cannot delete the last project. Create another project first.",
        )
    result = await session.execute(
        update(Project)
        .where(
            Project.slug == slug,
            Project.org_id == org_id,
            Project.deleted_at.is_(None),
        )
        .values(deleted_at=func.now())
        .returning(Project.id)
    )
    deleted_row = result.first()
    if deleted_row is None:
        raise HTTPException(status_code=404, detail="project not found")

    # A deleted project must not keep authenticating. API keys are scoped to
    # a project; without this, every key minted for the project keeps working
    # after deletion -- it can still ingest spans into and read data out of a
    # project the operator believes is gone. Revoke all live keys with the
    # project (fail closed). There is no restore flow; if the project is
    # recreated, new keys are minted for it.
    await session.execute(
        update(ApiKey)
        .where(ApiKey.project_id == deleted_row[0], ApiKey.revoked_at.is_(None))
        .values(revoked_at=func.now())
    )
