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

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

import auth as auth_mod
from database import get_db_session
from models import ApiKey, Project, ProjectSettings
from repositories import members as members_repo

from schemas.responses import ProjectResponse, ProjectListResponse
from ._deps import require_scope


router = APIRouter(prefix="/v1/projects", tags=["projects"])

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{1,62}[a-z0-9]$")


class CreateProjectRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    slug: str = Field(
        ..., min_length=3, max_length=64,
        description="URL-safe identifier. Lowercase alphanumeric + hyphens.",
    )


class UpdateProjectRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)


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
    """List all projects."""
    stmt = select(Project).order_by(Project.name)
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
    result = await session.execute(
        select(Project).where(
            Project.slug == slug,
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

    result = await session.execute(
        update(Project)
        .where(Project.slug == slug, Project.deleted_at.is_(None))
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
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_PROJECTS_MANAGE)
    ),
    session: AsyncSession = Depends(get_db_session),
):
    """Soft-delete a project."""
    from sqlalchemy import func, select
    # Refuse to delete the last remaining project — an instance with zero
    # projects has no usable context. The caller must always have at least one.
    remaining = await session.execute(
        select(func.count())
        .select_from(Project)
        .where(Project.deleted_at.is_(None))
    )
    if (remaining.scalar() or 0) <= 1:
        raise HTTPException(
            status_code=409,
            detail="Cannot delete the last project. Create another project first.",
        )
    result = await session.execute(
        update(Project)
        .where(Project.slug == slug, Project.deleted_at.is_(None))
        .values(deleted_at=func.now())
    )
    if not result.rowcount:
        raise HTTPException(status_code=404, detail="project not found")
