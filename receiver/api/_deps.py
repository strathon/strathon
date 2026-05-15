"""Shared FastAPI dependencies for the router modules.

Why a separate file:
    main.py used to own `_authenticated` and `_require_auth` directly, but
    that meant every router would have to import from main.py — which is
    fragile (main.py is the entrypoint, importing from it sets up a
    circular dependency the moment we want main.py to import any router).

    Putting these here keeps the import graph one-directional:
        api/<router>.py  ──>  api/_deps.py
                              api/_deps.py reads from request.app.state

    Routers never import from main.py. main.py imports from api/. Done.

Why `request: Request` instead of module-level globals:
    The metrics container and default_project_id live on `app.state` (set
    by the lifespan). Routers grab them via `request.app.state.X` so we
    don't have to thread the app object around or rely on import-time
    state that doesn't exist yet during module loading.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

import auth
from database import get_db_session


async def _authenticated(
    request: Request,
    session: AsyncSession,
    authorization: str | None,
) -> auth.ApiKeyContext:
    """Resolve a Bearer token and bump the auth Prometheus counters.

    Wraps `auth.resolve_api_key` so every authed endpoint contributes to
    auth_successes / auth_failures regardless of whether it uses the
    `require_auth` dependency wrapper or calls _authenticated directly.
    """
    metrics = request.app.state.metrics
    try:
        ctx = await auth.resolve_api_key(session, authorization)
    except HTTPException:
        metrics.auth_failures.inc()
        raise
    metrics.auth_successes.inc()
    return ctx


async def require_auth(
    request: Request,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> auth.ApiKeyContext:
    """FastAPI dependency that resolves the Bearer token to a project context.

    Endpoints that need the ApiKeyContext as a parameter can write
        ctx: auth.ApiKeyContext = Depends(require_auth)
    and skip the manual `_authenticated(...)` plumbing.
    """
    return await _authenticated(request, session, authorization)


def coerce_project_id(request: Request, value: str | None) -> UUID:
    """For v0 we resolve everything to the default project.

    Once per-API-key project resolution lands, this helper can be deleted
    in favor of pulling the project_id off the ApiKeyContext directly.
    """
    if value:
        try:
            return UUID(value)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"invalid project_id: {value}",
            )
    return request.app.state.default_project_id
