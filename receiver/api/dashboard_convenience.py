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

from rbac import can_manage_role
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

VERSION = "1.2.3"
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
    new_password: str = Field(min_length=1, max_length=128)
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
    from api.auth_endpoints import _validate_password

    _validate_password(body.new_password)

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
        FROM project_members m
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
                "SELECT 1 FROM project_members "
                "WHERE user_id = :uid AND project_id = :pid"
            ),
            {"uid": user_row[0], "pid": ctx.project_id},
        )
        if existing.first():
            raise HTTPException(409, "User is already a member of this project")

        await session.execute(
            text(
                "INSERT INTO project_members (user_id, project_id, role) "
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


@router.get("/v1/members/pending")
async def list_pending_invitations(
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_PROJECT_SETTINGS_READ)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """List invitations that have been sent but not yet redeemed.

    These are rows in pending_invitations for this project — people who were
    invited but have not registered yet. Surfacing them lets an owner see
    outstanding invites instead of them being invisible until registration.
    """
    result = await session.execute(
        text(
            "SELECT email, role, created_at FROM pending_invitations "
            "WHERE project_id = :pid ORDER BY created_at DESC"
        ),
        {"pid": ctx.project_id},
    )
    pending = [
        {
            "email": row["email"],
            "role": row["role"],
            "invited_at": row["created_at"].isoformat() if row["created_at"] else None,
            "status": "pending",
        }
        for row in result.mappings()
    ]
    return {"data": pending}


@router.delete("/v1/members/pending/{email}")
async def revoke_pending_invitation(
    email: str,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_PROJECT_SETTINGS_WRITE)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Revoke an outstanding invitation before it is redeemed."""
    await session.execute(
        text(
            "DELETE FROM pending_invitations "
            "WHERE project_id = :pid AND LOWER(email) = LOWER(:email)"
        ),
        {"pid": ctx.project_id, "email": email},
    )
    await session.commit()
    return {"status": "revoked", "email": email}


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
            "SELECT role FROM project_members "
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
            "UPDATE project_members SET role = :role "
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
            "SELECT role FROM project_members "
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
            "DELETE FROM project_members "
            "WHERE user_id = :uid AND project_id = :pid"
        ),
        {"uid": member_id, "pid": ctx.project_id},
    )
    await session.commit()


# ---- Member admin actions ----------------------------------------------------

async def _require_can_manage_member(session, ctx, member_id: str) -> None:
    """403 unless the caller strictly outranks the target member.

    Used for sensitive member actions (password reset, MFA disable). An admin
    may act on operators and viewers, but not on a peer admin or the owner.
    Owners outrank everyone. Falls open only when the caller has no session
    role (pure API-key calls with the manage scope), which is by design.
    """
    if not getattr(ctx, "role", None):
        return
    row = (await session.execute(
        text(
            "SELECT role FROM project_members "
            "WHERE user_id = :uid AND project_id = :pid"
        ),
        {"uid": member_id, "pid": ctx.project_id},
    )).first()
    if not row:
        raise HTTPException(404, "Member not found")
    if not can_manage_role(ctx.role, row[0]):
        raise HTTPException(
            403,
            "You can only manage members whose role is below your own.",
        )


def _gen_member_temp_password() -> str:
    """Temp password that satisfies the password policy.

    Guarantees a letter, a digit, and a special character so the value passes
    validation. The member is forced to change it on next login.
    """
    import string
    specials = "!@#$%^&*-_=+"
    chars = [
        secrets.choice(string.ascii_letters),
        secrets.choice(string.digits),
        secrets.choice(specials),
    ]
    pool = string.ascii_letters + string.digits + specials
    chars += [secrets.choice(pool) for _ in range(13)]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


@router.post("/v1/members/{member_id}/reset-password")
async def reset_member_password(
    member_id: str,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_PROJECT_SETTINGS_WRITE)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Admin resets a member's password. Returns temp password ONCE."""
    await _require_can_manage_member(session, ctx, member_id)
    temp_password = _gen_member_temp_password()
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
    await _require_can_manage_member(session, ctx, member_id)
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


async def _require_owner(session, user_id, project_id) -> None:
    """Raise 403 unless the user is the owner of the project."""
    row = (await session.execute(
        text(
            "SELECT role FROM project_members "
            "WHERE user_id = :uid AND project_id = :pid"
        ),
        {"uid": user_id, "pid": project_id},
    )).first()
    if not row or row[0] != "owner":
        raise HTTPException(403, "Only the project owner can transfer ownership")


@router.post("/v1/members/{member_id}/transfer-ownership")
async def initiate_transfer_ownership(
    member_id: str,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_PROJECT_SETTINGS_WRITE)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Initiate an ownership transfer to an existing admin member.

    This does NOT change any roles. It records a pending transfer that the
    recipient must explicitly accept. Only the current owner may initiate,
    and the target must already be an admin of the project.
    """
    await _require_owner(session, ctx.user_id, ctx.project_id)

    if str(member_id) == str(ctx.user_id):
        raise HTTPException(400, "You are already the owner")

    target = (await session.execute(
        text(
            "SELECT role FROM project_members "
            "WHERE user_id = :uid AND project_id = :pid"
        ),
        {"uid": member_id, "pid": ctx.project_id},
    )).first()
    if not target:
        raise HTTPException(404, "Member not found")
    if target[0] != "admin":
        raise HTTPException(400, "Target must be an admin to become owner")

    # One pending transfer per project; re-initiating replaces the prior one.
    await session.execute(
        text(
            "INSERT INTO pending_ownership_transfers "
            "(project_id, from_user_id, to_user_id) "
            "VALUES (:pid, :from_uid, :to_uid) "
            "ON CONFLICT (project_id) DO UPDATE "
            "SET from_user_id = :from_uid, to_user_id = :to_uid, "
            "created_at = NOW()"
        ),
        {"pid": ctx.project_id, "from_uid": ctx.user_id, "to_uid": member_id},
    )
    await session.commit()
    return {"status": "transfer_pending"}


@router.get("/v1/ownership-transfers/pending")
async def list_pending_transfers(
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_AUDIT_READ)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """List pending ownership transfers addressed to the current user.

    The dashboard renders these as accept/reject cards under Members.
    """
    rows = (await session.execute(
        text(
            "SELECT t.id, t.project_id, t.from_user_id, t.created_at, "
            "       u.display_name, u.email, p.name AS project_name "
            "FROM pending_ownership_transfers t "
            "JOIN users u ON u.id = t.from_user_id "
            "JOIN projects p ON p.id = t.project_id "
            "WHERE t.to_user_id = :uid AND t.project_id = :pid"
        ),
        {"uid": ctx.user_id, "pid": ctx.project_id},
    )).mappings().all()
    return {"data": [dict(r) for r in rows]}


@router.post("/v1/ownership-transfers/{transfer_id}/accept")
async def accept_transfer_ownership(
    transfer_id: str,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_PROJECT_SETTINGS_WRITE)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Accept a pending ownership transfer. Only the recipient may accept.

    On accept, the role swap happens atomically and the pending record is
    removed. If the initiating owner is no longer the owner (e.g. another
    transfer completed first), the swap is rejected.
    """
    row = (await session.execute(
        text(
            "SELECT project_id, from_user_id, to_user_id "
            "FROM pending_ownership_transfers "
            "WHERE id = :tid FOR UPDATE"
        ),
        {"tid": transfer_id},
    )).first()
    if not row:
        raise HTTPException(404, "Transfer not found")
    project_id, from_user_id, to_user_id = row[0], row[1], row[2]

    if str(to_user_id) != str(ctx.user_id):
        raise HTTPException(403, "Only the transfer recipient can accept")
    if str(project_id) != str(ctx.project_id):
        raise HTTPException(400, "Transfer is for a different project")

    # The initiator must still be the owner.
    owner = (await session.execute(
        text(
            "SELECT role FROM project_members "
            "WHERE user_id = :uid AND project_id = :pid"
        ),
        {"uid": from_user_id, "pid": project_id},
    )).first()
    if not owner or owner[0] != "owner":
        await session.execute(
            text("DELETE FROM pending_ownership_transfers WHERE id = :tid"),
            {"tid": transfer_id},
        )
        await session.commit()
        raise HTTPException(409, "The initiating owner is no longer the owner")

    await session.execute(
        text(
            "UPDATE project_members SET role = 'admin' "
            "WHERE user_id = :uid AND project_id = :pid"
        ),
        {"uid": from_user_id, "pid": project_id},
    )
    await session.execute(
        text(
            "UPDATE project_members SET role = 'owner' "
            "WHERE user_id = :uid AND project_id = :pid"
        ),
        {"uid": to_user_id, "pid": project_id},
    )
    await session.execute(
        text("DELETE FROM pending_ownership_transfers WHERE id = :tid"),
        {"tid": transfer_id},
    )
    await session.commit()
    return {"status": "ownership_transferred"}


@router.post("/v1/ownership-transfers/{transfer_id}/reject")
async def reject_transfer_ownership(
    transfer_id: str,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_PROJECT_SETTINGS_WRITE)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Reject or cancel a pending transfer.

    The recipient may reject; the initiating owner may cancel. Either way the
    pending record is removed and no roles change.
    """
    row = (await session.execute(
        text(
            "SELECT from_user_id, to_user_id "
            "FROM pending_ownership_transfers WHERE id = :tid"
        ),
        {"tid": transfer_id},
    )).first()
    if not row:
        raise HTTPException(404, "Transfer not found")
    from_user_id, to_user_id = row[0], row[1]
    if str(ctx.user_id) not in (str(from_user_id), str(to_user_id)):
        raise HTTPException(403, "Not a party to this transfer")

    await session.execute(
        text("DELETE FROM pending_ownership_transfers WHERE id = :tid"),
        {"tid": transfer_id},
    )
    await session.commit()
    return {"status": "transfer_cancelled"}


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

    # Read typed columns from project_settings.
    settings = await session.execute(
        text(
            "SELECT trace_retention_days, pii_redaction_enabled, "
            "content_capture_enabled, intervention_default_action "
            "FROM project_settings WHERE project_id = :pid"
        ),
        {"pid": ctx.project_id},
    )
    row = settings.first()

    return {
        "project_name": project[0] if project else "default",
        "project_slug": project[1] if project else "default",
        "retention": {
            "traces_days": row[0] if row else 30,
        },
        "pii_redaction_enabled": row[1] if row else True,
        "content_capture_enabled": row[2] if row else False,
        "intervention_default_action": row[3] if row else "allow",
    }


class UpdateSettingsBody(BaseModel):
    project_name: str | None = None
    retention: dict[str, int] | None = None
    pii_redaction_enabled: bool | None = None
    content_capture_enabled: bool | None = None
    intervention_default_action: str | None = None
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

    # Build SET clauses for project_settings columns.
    set_parts: list[str] = []
    params: dict[str, Any] = {"pid": ctx.project_id, "uid": ctx.user_id}

    if body.retention and "traces_days" in body.retention:
        set_parts.append("trace_retention_days = :traces_days")
        params["traces_days"] = body.retention["traces_days"]

    if body.pii_redaction_enabled is not None:
        set_parts.append("pii_redaction_enabled = :pii")
        params["pii"] = body.pii_redaction_enabled

    if body.content_capture_enabled is not None:
        set_parts.append("content_capture_enabled = :capture")
        params["capture"] = body.content_capture_enabled

    if body.intervention_default_action is not None:
        set_parts.append("intervention_default_action = :action")
        params["action"] = body.intervention_default_action

    if set_parts:
        set_parts.append("updated_by_user_id = :uid")
        sql = (
            f"UPDATE project_settings SET {', '.join(set_parts)} "
            f"WHERE project_id = :pid"
        )
        await session.execute(text(sql), params)

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
    if u is None:
        raise HTTPException(status_code=404, detail="User not found")

    memberships = await session.execute(
        text(
            "SELECT p.name, p.slug, m.role, m.created_at "
            "FROM project_members m JOIN projects p ON p.id = m.project_id "
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
        request=request,
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
    return await mfa_setup(request=request, authorization=authorization, session=session)
