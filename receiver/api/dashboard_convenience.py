"""Dashboard convenience endpoints.

The dashboard BFF proxy calls paths like /v1/members and /v1/settings.
The receiver's existing endpoints use /v1/projects/{slug}/members and
/v1/project/settings. These convenience routes bridge the gap by
resolving the project from the authenticated user's context.

Also adds endpoints that don't exist yet: capabilities, change-password,
version, member MFA/password management, transfer-ownership, GDPR export.
"""

from __future__ import annotations

import os
import secrets
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request as FastAPIRequest, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import auth as auth_mod
from database import get_db_session
from password import hash_password

from ._deps import require_scope

router = APIRouter(tags=["dashboard"])

VERSION = "0.1.0"
API_VERSION = "v1"


# ---- Capabilities (no auth required) ----------------------------------------

@router.get("/v1/auth/capabilities")
async def get_capabilities() -> dict[str, Any]:
    """Return available auth features. Used by login/register pages."""
    smtp_configured = bool(os.environ.get("STRATHON_SMTP_HOST"))
    return {
        "registration_enabled": os.environ.get(
            "STRATHON_REGISTRATION_ENABLED", "true"
        ).lower() in ("1", "true", "yes"),
        "smtp_enabled": smtp_configured,
        "mfa_available": True,
        "mode": os.environ.get("STRATHON_MODE", "self-hosted"),
    }


# ---- Version (no auth required) ---------------------------------------------

@router.get("/v1/version")
async def get_version() -> dict[str, str]:
    return {"version": VERSION, "api_version": API_VERSION}


# ---- Change password ---------------------------------------------------------

class ChangePasswordBody(BaseModel):
    current_password: str
    new_password: str = Field(min_length=10, max_length=128)
    model_config = {"extra": "forbid"}


@router.post("/v1/auth/change-password")
async def change_password(
    body: ChangePasswordBody,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_AUDIT_READ)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Change own password. Requires current password verification."""
    from password import verify_password

    result = await session.execute(
        text("SELECT password_hash FROM users WHERE id = :uid"),
        {"uid": ctx.user_id},
    )
    row = result.first()
    if not row or not verify_password(row[0], body.current_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Current password is incorrect",
        )

    new_hash = hash_password(body.new_password)
    await session.execute(
        text(
            "UPDATE users SET password_hash = :h, "
            "force_password_change = false "
            "WHERE id = :uid"
        ),
        {"h": new_hash, "uid": ctx.user_id},
    )

    # Invalidate all sessions (user must re-login with new password).
    await session.execute(
        text("DELETE FROM sessions WHERE user_id = :uid"),
        {"uid": ctx.user_id},
    )
    await session.commit()
    return {"status": "password_changed"}


# ---- Members convenience (resolves project from auth context) ----------------

@router.get("/v1/members")
async def list_members_convenience(
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_AUDIT_READ)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """List members of the current project."""
    result = await session.execute(text("""
        SELECT u.id, u.email, u.display_name, m.role, m.created_at,
               u.last_login_at, u.mfa_enabled
        FROM memberships m
        JOIN users u ON u.id = m.user_id
        WHERE m.project_id = :pid
        ORDER BY m.created_at ASC
    """), {"pid": ctx.project_id})

    members = []
    for row in result.mappings().all():
        members.append({
            "id": str(row["id"]),
            "email": row["email"],
            "display_name": row["display_name"] or row["email"].split("@")[0],
            "role": row["role"],
            "joined_at": row["created_at"].isoformat() if row["created_at"] else None,
            "last_active": row["last_login_at"].isoformat() if row["last_login_at"] else None,
            "mfa_enabled": row["mfa_enabled"] or False,
        })

    return {"data": members}


class InviteMemberBody(BaseModel):
    email: str = Field(max_length=254)
    role: str = Field(pattern=r"^(viewer|operator|admin)$")
    model_config = {"extra": "forbid"}


@router.post("/v1/members", status_code=status.HTTP_201_CREATED)
async def invite_member(
    body: InviteMemberBody,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_PROJECT_SETTINGS_WRITE)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Invite a member to the current project."""
    # Check if user exists.
    result = await session.execute(
        text("SELECT id FROM users WHERE LOWER(email) = LOWER(:email)"),
        {"email": body.email},
    )
    user_row = result.first()

    if user_row:
        # Check if already a member.
        existing = await session.execute(
            text(
                "SELECT 1 FROM memberships "
                "WHERE user_id = :uid AND project_id = :pid"
            ),
            {"uid": user_row[0], "pid": ctx.project_id},
        )
        if existing.first():
            raise HTTPException(409, "User is already a member of this project")

        await session.execute(
            text(
                "INSERT INTO memberships (user_id, project_id, role) "
                "VALUES (:uid, :pid, :role)"
            ),
            {"uid": user_row[0], "pid": ctx.project_id, "role": body.role},
        )
    else:
        # Create pending invitation.
        await session.execute(
            text(
                "INSERT INTO pending_invitations (email, project_id, role) "
                "VALUES (LOWER(:email), :pid, :role) "
                "ON CONFLICT (email, project_id) DO UPDATE SET role = :role"
            ),
            {"email": body.email, "pid": ctx.project_id, "role": body.role},
        )

    await session.commit()
    return {"status": "invited", "email": body.email, "role": body.role}


class UpdateRoleBody(BaseModel):
    role: str = Field(pattern=r"^(viewer|operator|admin)$")
    model_config = {"extra": "forbid"}


@router.patch("/v1/members/{member_id}")
async def update_member_role(
    member_id: str,
    body: UpdateRoleBody,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_PROJECT_SETTINGS_WRITE)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Change a member's role. Cannot change owner or self."""
    # Check target's current role.
    result = await session.execute(
        text(
            "SELECT role FROM memberships "
            "WHERE user_id = :uid AND project_id = :pid"
        ),
        {"uid": member_id, "pid": ctx.project_id},
    )
    row = result.first()
    if not row:
        raise HTTPException(404, "Member not found")
    if row[0] == "owner":
        raise HTTPException(403, "Cannot change the owner's role")
    if str(ctx.user_id) == member_id:
        raise HTTPException(403, "Cannot change your own role")

    await session.execute(
        text(
            "UPDATE memberships SET role = :role "
            "WHERE user_id = :uid AND project_id = :pid"
        ),
        {"uid": member_id, "pid": ctx.project_id, "role": body.role},
    )
    await session.commit()
    return {"status": "updated", "role": body.role}


@router.delete("/v1/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    member_id: str,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_PROJECT_SETTINGS_WRITE)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    """Remove a member. Cannot remove owner or self."""
    result = await session.execute(
        text(
            "SELECT role FROM memberships "
            "WHERE user_id = :uid AND project_id = :pid"
        ),
        {"uid": member_id, "pid": ctx.project_id},
    )
    row = result.first()
    if not row:
        raise HTTPException(404, "Member not found")
    if row[0] == "owner":
        raise HTTPException(403, "Cannot remove the project owner")
    if str(ctx.user_id) == member_id:
        raise HTTPException(403, "Cannot remove yourself")

    await session.execute(
        text(
            "DELETE FROM memberships "
            "WHERE user_id = :uid AND project_id = :pid"
        ),
        {"uid": member_id, "pid": ctx.project_id},
    )
    await session.commit()


# ---- Member admin actions ----------------------------------------------------

@router.post("/v1/members/{member_id}/reset-password")
async def reset_member_password(
    member_id: str,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_PROJECT_SETTINGS_WRITE)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Admin resets a member's password. Returns temp password ONCE."""
    temp_password = secrets.token_urlsafe(16)
    new_hash = hash_password(temp_password)
    result = await session.execute(
        text(
            "UPDATE users SET password_hash = :h, "
            "force_password_change = true "
            "WHERE id = :uid RETURNING email"
        ),
        {"h": new_hash, "uid": member_id},
    )
    row = result.first()
    if not row:
        raise HTTPException(404, "User not found")
    await session.commit()
    return {
        "status": "password_reset",
        "temporary_password": temp_password,
        "email": row[0],
        "message": "User must change password on next login",
    }


@router.post("/v1/members/{member_id}/disable-mfa")
async def disable_member_mfa(
    member_id: str,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_PROJECT_SETTINGS_WRITE)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Admin disables MFA for a member who lost their device + backup codes."""
    result = await session.execute(
        text(
            "UPDATE users SET mfa_enabled = false, totp_secret = NULL, "
            "backup_codes = NULL "
            "WHERE id = :uid RETURNING email"
        ),
        {"uid": member_id},
    )
    row = result.first()
    if not row:
        raise HTTPException(404, "User not found")
    await session.commit()
    return {"status": "mfa_disabled", "email": row[0]}


@router.post("/v1/members/{member_id}/transfer-ownership")
async def transfer_ownership(
    member_id: str,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_PROJECT_SETTINGS_WRITE)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Transfer project ownership. Only current owner can call."""
    # Verify caller is owner.
    caller_role = await session.execute(
        text(
            "SELECT role FROM memberships "
            "WHERE user_id = :uid AND project_id = :pid"
        ),
        {"uid": ctx.user_id, "pid": ctx.project_id},
    )
    caller_row = caller_role.first()
    if not caller_row or caller_row[0] != "owner":
        raise HTTPException(403, "Only the project owner can transfer ownership")

    # Verify target is admin.
    target_role = await session.execute(
        text(
            "SELECT role FROM memberships "
            "WHERE user_id = :uid AND project_id = :pid"
        ),
        {"uid": member_id, "pid": ctx.project_id},
    )
    target_row = target_role.first()
    if not target_row:
        raise HTTPException(404, "Member not found")
    if target_row[0] not in ("admin",):
        raise HTTPException(400, "Target must be an admin to become owner")

    # Swap roles.
    await session.execute(
        text(
            "UPDATE memberships SET role = 'admin' "
            "WHERE user_id = :uid AND project_id = :pid"
        ),
        {"uid": ctx.user_id, "pid": ctx.project_id},
    )
    await session.execute(
        text(
            "UPDATE memberships SET role = 'owner' "
            "WHERE user_id = :uid AND project_id = :pid"
        ),
        {"uid": member_id, "pid": ctx.project_id},
    )
    await session.commit()
    return {"status": "ownership_transferred"}


# ---- Settings convenience (resolves project from auth context) ---------------

@router.get("/v1/settings")
async def get_settings_convenience(
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_AUDIT_READ)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Get project settings."""
    result = await session.execute(
        text("SELECT name, slug FROM projects WHERE id = :pid"),
        {"pid": ctx.project_id},
    )
    project = result.first()

    # Get retention settings.
    retention = await session.execute(
        text(
            "SELECT key, value FROM project_settings "
            "WHERE project_id = :pid AND key LIKE 'retention_%'"
        ),
        {"pid": ctx.project_id},
    )
    retention_dict = {r[0]: r[1] for r in retention.all()}

    return {
        "project_name": project[0] if project else "default",
        "project_slug": project[1] if project else "default",
        "timezone": retention_dict.get("retention_timezone", "UTC"),
        "retention": {
            "traces_days": int(retention_dict.get("retention_traces_days", "30")),
            "audit_days": int(retention_dict.get("retention_audit_days", "365")),
            "spans_days": int(retention_dict.get("retention_spans_days", "30")),
        },
    }


class UpdateSettingsBody(BaseModel):
    project_name: str | None = None
    timezone: str | None = None
    retention: dict[str, int] | None = None
    model_config = {"extra": "forbid"}


@router.patch("/v1/settings")
async def update_settings_convenience(
    body: UpdateSettingsBody,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_PROJECT_SETTINGS_WRITE)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Update project settings."""
    if body.project_name:
        await session.execute(
            text("UPDATE projects SET name = :name WHERE id = :pid"),
            {"name": body.project_name, "pid": ctx.project_id},
        )

    if body.timezone:
        await session.execute(
            text(
                "INSERT INTO project_settings (project_id, key, value) "
                "VALUES (:pid, 'retention_timezone', :tz) "
                "ON CONFLICT (project_id, key) DO UPDATE SET value = :tz"
            ),
            {"pid": ctx.project_id, "tz": body.timezone},
        )

    if body.retention:
        for k, v in body.retention.items():
            await session.execute(
                text(
                    "INSERT INTO project_settings (project_id, key, value) "
                    "VALUES (:pid, :key, :val) "
                    "ON CONFLICT (project_id, key) DO UPDATE SET value = :val"
                ),
                {"pid": ctx.project_id, "key": f"retention_{k}", "val": str(v)},
            )

    await session.commit()
    return {"status": "updated"}


# ---- GDPR export -------------------------------------------------------------

@router.get("/v1/auth/me/export")
async def export_my_data(
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_AUDIT_READ)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """GDPR Article 20 data portability. Export all user data."""
    user = await session.execute(
        text(
            "SELECT email, display_name, created_at, last_login_at, "
            "mfa_enabled FROM users WHERE id = :uid"
        ),
        {"uid": ctx.user_id},
    )
    u = user.first()

    memberships = await session.execute(
        text(
            "SELECT p.name, p.slug, m.role, m.created_at "
            "FROM memberships m JOIN projects p ON p.id = m.project_id "
            "WHERE m.user_id = :uid"
        ),
        {"uid": ctx.user_id},
    )

    return {
        "user": {
            "email": u[0],
            "display_name": u[1],
            "created_at": u[2].isoformat() if u[2] else None,
            "last_login_at": u[3].isoformat() if u[3] else None,
            "mfa_enabled": u[4],
        },
        "memberships": [
            {
                "project": r[0], "slug": r[1],
                "role": r[2], "joined_at": r[3].isoformat() if r[3] else None,
            }
            for r in memberships.all()
        ],
    }


# ---- Path aliases (dashboard BFF paths → receiver canonical paths) -----------


@router.post("/v1/auth/password-reset-request")
async def password_reset_request_alias(
    request: FastAPIRequest,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Alias: dashboard sends here, forwards to reset-password/request."""
    from api.auth_endpoints import request_password_reset, PasswordResetRequestBody
    body = await request.json()
    return await request_password_reset(
        body=PasswordResetRequestBody(**body),
        request=request,
        session=session,
    )


@router.post("/v1/auth/password-reset")
async def password_reset_confirm_alias(
    request: FastAPIRequest,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Alias: dashboard sends here, forwards to reset-password/confirm."""
    from api.auth_endpoints import confirm_password_reset, PasswordResetConfirmBody
    body = await request.json()
    return await confirm_password_reset(
        body=PasswordResetConfirmBody(**body),
        session=session,
    )


@router.post("/v1/auth/mfa/enable")
async def mfa_enable_alias(
    request: FastAPIRequest,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Alias: dashboard sends mfa/enable, receiver has mfa/setup."""
    from api.auth_endpoints import mfa_setup
    authorization = request.headers.get("authorization", "")
    return await mfa_setup(authorization=authorization, session=session)
