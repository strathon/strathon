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
) -> AuthResponse:
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

    user = await users_repo.find_by_email(session, email)

    if user is None or not user.password_hash:
        # Dummy hash to prevent timing-based enumeration
        verify_password(hash_password("dummy"), body.password)
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
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    # Transparent parameter upgrade on successful login
    if check_needs_rehash(user.password_hash):
        new_hash = hash_password(body.password)
        await users_repo.update_password_hash(session, user.id, new_hash)
        logger.info("rehashed password for user %s (params upgraded)", user.id)

    # Update last_login_at
    await users_repo.touch_last_login(session, user.id)

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

    return AuthResponse(
        token=raw_token,
        user={
            "id": str(user.id),
            "email": user.email,
            "display_name": user.display_name,
        },
        message="logged in",
    )


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

    return MeResponse(
        user={
            "id": str(user.id),
            "email": user.email,
            "display_name": user.display_name,
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        },
        projects=projects,
    )
