"""Authentication endpoints for dashboard users.

  POST   /v1/auth/register   create account (first user auto-becomes owner)
  POST   /v1/auth/login      email+password → session token
  POST   /v1/auth/logout     invalidate session
  GET    /v1/auth/me          current user + projects + roles

These endpoints do NOT require API key auth — they use email+password
credentials and session tokens. The /me endpoint requires a valid
session token.
"""

from __future__ import annotations

import ipaddress as _ipaddress
import logging
import re
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db_session
from password import check_needs_rehash, hash_password, verify_password
from repositories import members as members_repo
from repositories import sessions as sessions_repo
from repositories import users as users_repo

logger = logging.getLogger("strathon.receiver.api.auth")

router = APIRouter(prefix="/v1/auth", tags=["auth"])

# Minimum password requirements
_MIN_PASSWORD_LENGTH = 8
_MAX_PASSWORD_LENGTH = 128


# ---- Request/response schemas -------------------------------------------

class RegisterRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=255)
    password: str = Field(..., min_length=_MIN_PASSWORD_LENGTH, max_length=_MAX_PASSWORD_LENGTH)
    display_name: Optional[str] = Field(default=None, max_length=200)


class LoginRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=255)
    password: str = Field(..., min_length=1, max_length=_MAX_PASSWORD_LENGTH)


class AuthResponse(BaseModel):
    token: str
    user: dict
    message: str


class MeResponse(BaseModel):
    user: dict
    projects: list[dict]


# ---- Helpers -------------------------------------------------------------

_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")


def _validate_email(email: str) -> str:
    """Basic email format validation. Returns normalized email."""
    email = email.strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid email format",
        )
    return email


def _validate_password(password: str) -> None:
    """Enforce minimum password requirements."""
    if len(password) < _MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Password must be at least {_MIN_PASSWORD_LENGTH} characters",
        )


def _client_ip(request: Request) -> str | None:
    client = request.client
    if client is None or not client.host:
        return None
    try:
        _ipaddress.ip_address(client.host)
        return client.host
    except ValueError:
        return None


# ---- Endpoints -----------------------------------------------------------


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> AuthResponse:
    """Register a new user account.

    The first user to register auto-becomes owner of the default project.
    Subsequent users are created without project membership — an existing
    owner or admin must invite them.
    """
    # Check if registration is enabled
    from config import get_settings
    _settings = get_settings()
    if not _settings.registration_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Registration is disabled. Ask an admin to invite you.",
        )

    email = _validate_email(body.email)
    _validate_password(body.password)

    # Check if email is already taken
    existing = await users_repo.find_by_email(session, email)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered",
        )

    # Check if this is the first user (auto-owner)
    user_count = await users_repo.count_users(session)
    is_first_user = user_count == 0

    # Hash password with Argon2id
    pw_hash = hash_password(body.password)

    # Create the user
    user = await users_repo.create_user(
        session,
        email=email,
        password_hash=pw_hash,
        display_name=body.display_name,
    )

    # First user gets auto-added as owner of default project
    if is_first_user:
        from sqlalchemy import select
        from models import Project

        default_project = await session.execute(
            select(Project).where(Project.slug == "default").where(Project.deleted_at.is_(None))
        )
        project = default_project.scalar_one_or_none()
        if project:
            await members_repo.add_member(
                session,
                project_id=project.id,
                user_id=user.id,
                role="owner",
            )
            logger.info("first user %s auto-assigned as owner of default project", email)

    # Create session token
    raw_token, _ = await sessions_repo.create_session(
        session,
        user_id=user.id,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )

    await session.commit()

    return AuthResponse(
        token=raw_token,
        user={
            "id": str(user.id),
            "email": user.email,
            "display_name": user.display_name,
        },
        message="first user — auto-assigned as project owner" if is_first_user else "registered",
    )


@router.post("/login")
async def login(
    body: LoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    """Authenticate with email + password, returns a session token.

    Rate-limited per client IP to prevent brute-force attacks. Performs
    a dummy Argon2 verification even when the email doesn't exist, to
    prevent timing-based user enumeration.
    """
    # Per-IP login rate limiting
    client_ip = _client_ip(request) or "unknown"
    login_limiter = getattr(request.app.state, "login_rate_limiter", None)
    if login_limiter is not None:
        allowed, remaining, retry_after = await login_limiter.consume(client_ip)
        if not allowed:
            import math
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many login attempts. Try again later.",
                headers={"Retry-After": str(math.ceil(retry_after))},
            )

    email = body.email.strip().lower()

    # Account lockout check (auto-activate: 5 failures → 15 min lock).
    from security_auto import (
        check_account_lockout, record_failed_login, reset_failed_login,
        enforce_session_cap,
    )
    lockout_msg = await check_account_lockout(session, email)
    if lockout_msg:
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail=lockout_msg,
        )

    user = await users_repo.find_by_email(session, email)

    if user is None or not user.password_hash:
        # Dummy hash to prevent timing-based enumeration
        verify_password(hash_password("dummy"), body.password)
        await record_failed_login(session, email)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is deactivated",
        )

    if not verify_password(user.password_hash, body.password):
        await record_failed_login(session, email)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    # Transparent parameter upgrade on successful login
    if check_needs_rehash(user.password_hash):
        new_hash = hash_password(body.password)
        await users_repo.update_password_hash(session, user.id, new_hash)
        logger.info("rehashed password for user %s (params upgraded)", user.id)

    # Reset lockout counter on successful login.
    await reset_failed_login(session, email)

    # Enforce concurrent session cap (evicts oldest if over limit).
    await enforce_session_cap(session, user.id)

    # Update last_login_at
    await users_repo.touch_last_login(session, user.id)

    # MFA check: if MFA is enabled, return an MFA challenge instead
    # of a session token. The client must then call /v1/auth/mfa/verify.
    if user.mfa_enabled:
        # Create a short-lived MFA token (5 minutes).
        mfa_raw, _ = await sessions_repo.create_session(
            session,
            user_id=user.id,
            ip_address=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            ttl_hours=5 / 60,  # 5 minutes
        )
        await session.commit()
        return {
            "mfa_required": True,
            "mfa_token": mfa_raw,
            "message": "MFA verification required",
        }

    # Create session with configurable TTL
    from config import get_settings
    _settings = get_settings()
    raw_token, _ = await sessions_repo.create_session(
        session,
        user_id=user.id,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
        ttl_hours=_settings.session_ttl_hours,
    )

    await session.commit()

    return {
        "token": raw_token,
        "user": {
            "id": str(user.id),
            "email": user.email,
            "display_name": user.display_name,
        },
        "message": "logged in",
    }


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    """Invalidate the current session token."""
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
        )

    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed Authorization header",
        )
    token = parts[1].strip()

    # Don't logout API keys
    if token.startswith("stra_"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot logout an API key. Use session tokens for dashboard auth.",
        )

    sess = await sessions_repo.resolve_session_token(session, token)
    if sess is None:
        # Already expired or invalid — succeed silently (idempotent)
        return

    await sessions_repo.delete_session(session, sess.id)
    await session.commit()


@router.get("/me")
async def me(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> MeResponse:
    """Return the current user's profile and project memberships."""
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
        )

    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed Authorization header",
        )
    token = parts[1].strip()

    if token.startswith("stra_"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="/auth/me is for session-based auth. API keys don't have user profiles.",
        )

    sess = await sessions_repo.resolve_session_token(session, token)
    if sess is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session token",
        )

    user = await users_repo.find_by_id(session, sess.user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )

    projects = await members_repo.get_user_projects(session, user.id)

    # Get primary project + role for dashboard context.
    primary_project = projects[0] if projects else None
    role = primary_project.get("role") if primary_project else None

    return MeResponse(
        user={
            "id": str(user.id),
            "email": user.email,
            "display_name": user.display_name,
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
            "mfa_enabled": getattr(user, "mfa_enabled", False),
            "force_password_change": getattr(user, "force_password_change", False),
            "role": role,
            "project_id": str(primary_project["id"]) if primary_project else None,
            "project_name": primary_project.get("name") if primary_project else None,
        },
        projects=projects,
    )


# ---- MFA (TOTP) endpoints ------------------------------------------------


class MfaSetupResponse(BaseModel):
    secret: str
    otpauth_uri: str
    message: str = "Scan the QR code with your authenticator app, then verify"


class MfaVerifySetupRequest(BaseModel):
    code: str


class MfaVerifySetupResponse(BaseModel):
    backup_codes: list[str]
    message: str = "MFA enabled. Store these backup codes safely."


class MfaVerifyLoginRequest(BaseModel):
    mfa_token: str
    code: str


class MfaDisableRequest(BaseModel):
    password: str
    code: str


class PasswordResetRequestBody(BaseModel):
    email: str


class PasswordResetConfirmBody(BaseModel):
    token: str
    new_password: str


class AdminResetPasswordBody(BaseModel):
    email: str


@router.post("/mfa/setup")
async def mfa_setup(
    request: Request,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    """Generate a TOTP secret for the current user. Requires session auth.

    Returns the base32 secret and otpauth:// URI for QR scanning.
    Does NOT enable MFA until /mfa/verify-setup is called.
    """
    user_id = await _require_session_user(session, authorization)

    import repositories.mfa as mfa_repo
    secret, uri = await mfa_repo.setup_totp(session, user_id)
    await session.commit()

    return MfaSetupResponse(secret=secret, otpauth_uri=uri).model_dump()


@router.post("/mfa/verify-setup")
async def mfa_verify_setup(
    body: MfaVerifySetupRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    """Verify a TOTP code and enable MFA. Returns backup codes.

    The backup codes are shown once and stored hashed. If the user
    loses their authenticator, they can use a backup code to log in.
    """
    user_id = await _require_session_user(session, authorization)

    import repositories.mfa as mfa_repo
    backup_codes = await mfa_repo.verify_and_enable_mfa(
        session, user_id, body.code,
    )
    if backup_codes is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid TOTP code or no pending MFA setup",
        )
    await session.commit()

    return MfaVerifySetupResponse(backup_codes=backup_codes).model_dump()


@router.post("/mfa/disable")
async def mfa_disable(
    body: MfaDisableRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    """Disable MFA. Requires current password + TOTP code."""
    user_id = await _require_session_user(session, authorization)

    # Verify password.
    user = await users_repo.find_by_id(session, user_id)
    if user is None or not verify_password(user.password_hash, body.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid password",
        )

    # Verify TOTP code.
    import repositories.mfa as mfa_repo
    if not mfa_repo.verify_totp_code(user.totp_secret or "", body.code):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid TOTP code",
        )

    await mfa_repo.disable_mfa(session, user_id)
    await session.commit()
    return {"message": "MFA disabled"}


@router.post("/mfa/verify")
async def mfa_verify_login(
    body: MfaVerifyLoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    """Complete MFA login. Takes the mfa_token + TOTP/backup code.

    Returns a full session token on success.
    """
    # Resolve the MFA token (short-lived session).
    sess = await sessions_repo.resolve_session_token(session, body.mfa_token)
    if sess is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired MFA token",
        )

    # Verify the TOTP or backup code.
    import repositories.mfa as mfa_repo
    valid = await mfa_repo.verify_mfa_code(session, sess.user_id, body.code)
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid MFA code",
        )

    # Invalidate the short-lived MFA session.
    await sessions_repo.delete_session(session, sess.id)

    # Create the real session.
    from config import get_settings
    _settings = get_settings()
    raw_token, _ = await sessions_repo.create_session(
        session,
        user_id=sess.user_id,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
        ttl_hours=_settings.session_ttl_hours,
    )

    user = await users_repo.find_by_id(session, sess.user_id)
    await session.commit()

    return {
        "token": raw_token,
        "user": {
            "id": str(user.id),
            "email": user.email,
            "display_name": user.display_name,
        } if user else {},
        "message": "MFA verified, logged in",
    }


# ---- Password reset endpoints --------------------------------------------


@router.post("/reset-password/request")
async def request_password_reset(
    body: PasswordResetRequestBody,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    """Request a password reset. Sends email if SMTP is configured.

    Always returns 200 regardless of whether the email exists, to
    prevent user enumeration.
    """
    import os
    smtp_host = os.environ.get("STRATHON_SMTP_HOST")

    if not smtp_host:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=(
                "Email password reset not available. Configure "
                "STRATHON_SMTP_HOST to enable, or use "
                "/v1/auth/admin-reset-password for admin-initiated resets."
            ),
        )

    import repositories.password_reset as reset_repo
    user = await reset_repo.find_user_by_email(session, body.email)

    if user is not None:
        raw_token = await reset_repo.create_reset_token(session, user.id)
        # Send email (best-effort, don't block on failures).
        try:
            _send_reset_email(user.email, raw_token, smtp_host)
        except Exception:
            logger.exception("Failed to send password reset email")

    await session.commit()

    # Always return success to prevent enumeration.
    return {
        "message": "If an account with that email exists, a reset link has been sent.",
    }


@router.post("/reset-password/confirm")
async def confirm_password_reset(
    body: PasswordResetConfirmBody,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    """Confirm a password reset with token + new password."""
    import repositories.password_reset as reset_repo

    if len(body.new_password) < 8:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password must be at least 8 characters",
        )

    user_id = await reset_repo.validate_and_consume_token(
        session, body.token,
    )
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired reset token",
        )

    new_hash = hash_password(body.new_password)
    await reset_repo.reset_password(session, user_id, new_hash)
    await session.commit()

    return {"message": "Password reset successfully. All sessions invalidated."}


@router.post("/admin-reset-password")
async def admin_reset_password(
    body: AdminResetPasswordBody,
    request: Request,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    """Admin-only: reset a user's password. Returns temporary password.

    Requires owner or admin role. No email needed.
    """
    # Require session auth with admin role.
    user_id = await _require_session_user(session, authorization)
    # Get the admin's project context (first project they're in).
    from sqlalchemy import text
    result = await session.execute(
        text("SELECT project_id, role FROM project_members WHERE user_id = :uid LIMIT 1"),
        {"uid": user_id},
    )
    row = result.mappings().first()
    if row is None or row["role"] not in ("owner", "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only owners and admins can reset other users' passwords",
        )

    import repositories.password_reset as reset_repo
    target_user = await reset_repo.find_user_by_email(session, body.email)
    if target_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    # Generate temporary password.
    import secrets
    temp_password = secrets.token_urlsafe(16)
    new_hash = hash_password(temp_password)
    await reset_repo.reset_password(session, target_user.id, new_hash)
    await session.commit()

    return {
        "temporary_password": temp_password,
        "message": (
            f"Password reset for {body.email}. "
            "All sessions invalidated. User must change password on next login."
        ),
    }


# ---- Helpers ----


async def _require_session_user(
    session: AsyncSession,
    authorization: str | None,
) -> UUID:
    """Extract user_id from a session Bearer token. Raises 401 if invalid."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session token required",
        )
    token = authorization[7:].strip()

    # Must be a session token, not an API key.
    if token.startswith("stra_"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This endpoint requires session auth, not an API key",
        )

    sess = await sessions_repo.resolve_session_token(session, token)
    if sess is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session token",
        )
    return sess.user_id


def _send_reset_email(email: str, raw_token: str, smtp_host: str) -> None:
    """Send a password reset email via SMTP."""
    import os
    import smtplib
    from email.message import EmailMessage

    smtp_port = int(os.environ.get("STRATHON_SMTP_PORT", "587"))
    smtp_user = os.environ.get("STRATHON_SMTP_USER", "")
    smtp_pass = os.environ.get("STRATHON_SMTP_PASSWORD", "")
    smtp_from = os.environ.get("STRATHON_SMTP_FROM", "noreply@getstrathon.com")

    msg = EmailMessage()
    msg["Subject"] = "Strathon Password Reset"
    msg["From"] = smtp_from
    msg["To"] = email
    msg.set_content(
        f"You requested a password reset for your Strathon account.\n\n"
        f"Reset token: {raw_token}\n\n"
        f"This token expires in 1 hour. If you didn't request this, "
        f"ignore this email.\n"
    )

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        if smtp_user:
            server.login(smtp_user, smtp_pass)
        server.send_message(msg)
    logger.info("Password reset email sent to %s", email)
