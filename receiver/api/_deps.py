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

Authentication vs authorization:
    `require_auth` resolves the Bearer token and returns the
    ApiKeyContext. Authentication only — no capability check. Endpoints
    that don't care about scopes (or scope-check internally) use this.

    `require_scope("scope:name")` is the recommended form. It calls
    require_auth AND checks that the resolved key has the named scope.
    Returns HTTP 403 (not 401) when the key is valid but unscoped — the
    distinction matters because a 401 tells the caller their token is
    bad, while a 403 tells them their token is fine but lacks permission.
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
    x_project_id: str | None = None,
) -> auth.ApiKeyContext:
    """Resolve a Bearer token and bump the auth Prometheus counters.

    Wraps `auth.resolve_api_key` so every authed endpoint contributes to
    auth_successes / auth_failures regardless of whether it uses the
    `require_auth` dependency wrapper or calls _authenticated directly.

    For session-based auth, x_project_id (from the X-Project-Id header)
    provides the project context. API key auth ignores it.
    """
    metrics = request.app.state.metrics

    # Parse X-Project-Id into UUID if provided
    project_id_override: UUID | None = None
    if x_project_id:
        try:
            project_id_override = UUID(x_project_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid X-Project-Id header: {x_project_id}",
            )

    try:
        ctx = await auth.resolve_api_key(session, authorization, project_id_override)
    except HTTPException:
        metrics.auth_failures.inc()
        raise
    metrics.auth_successes.inc()
    return ctx


async def require_auth(
    request: Request,
    authorization: str | None = Header(default=None),
    x_project_id: str | None = Header(default=None, alias="X-Project-Id"),
    session: AsyncSession = Depends(get_db_session),
) -> auth.ApiKeyContext:
    """FastAPI dependency that resolves the Bearer token to a project context.

    Authentication only. Use `require_scope(...)` for endpoints that need
    a specific capability.
    """
    return await _authenticated(request, session, authorization, x_project_id)


def require_scope(scope: str):
    """Build a FastAPI dependency that requires a specific capability scope.

    Usage:
        @router.post("/v1/policies")
        async def create_policy(
            ctx: ApiKeyContext = Depends(require_scope("policies:write")),
            ...
        ):
            ...

    Behavior:
      - Resolves the Bearer token (same as require_auth)
      - Looks at ctx.scopes; allows if '*' in scopes or `scope` in scopes
      - Otherwise raises HTTP 403

    Why a factory: FastAPI dependencies are functions, not parameterized
    classes. Returning a closure that captures `scope` lets each endpoint
    declare its required capability statically while sharing one body of
    auth + scope-check logic.

    Why 403 not 401: the credential is valid; the capability is not.
    A 401 would mislead the caller into rotating a token that's fine.
    """
    async def _checker(
        request: Request,
        authorization: str | None = Header(default=None),
        x_project_id: str | None = Header(default=None, alias="X-Project-Id"),
        session: AsyncSession = Depends(get_db_session),
    ) -> auth.ApiKeyContext:
        ctx = await _authenticated(request, session, authorization, x_project_id)
        if not auth.key_has_scope(ctx.scopes, scope):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"missing required scope: {scope}",
            )
        return ctx

    return _checker


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


def build_audit_context(
    request: Request,
    ctx: auth.ApiKeyContext,
):
    """Build an :class:`EmitContext` from the request envelope.

    Used by every mutation endpoint to record an audit event in the
    same transaction as the mutation. Centralizing this here means
    endpoint code stays a one-liner — ``audit.emit(session,
    build_audit_context(request, ctx), ...)`` — and we get consistent
    actor/source_ip/user_agent capture without each endpoint
    re-implementing the same boilerplate.

    The actor is the API key holder. When orgs+users ship, this
    helper expands to thread the human-user identity through; the
    API of build_audit_context stays the same.
    """
    import uuid as _uuid
    from repositories.audit import EmitContext

    # request.client may be None in some test transports; degrade
    # gracefully rather than raise. The source_ip column is INET,
    # which rejects non-IP strings (Starlette's TestClient supplies
    # the literal "testclient" as host); we drop any non-parseable
    # value so the audit insert always succeeds.
    import ipaddress as _ipaddress
    client = request.client
    source_ip: str | None = None
    if client is not None and client.host:
        try:
            _ipaddress.ip_address(client.host)
            source_ip = client.host
        except ValueError:
            source_ip = None
    user_agent = request.headers.get("user-agent")
    # Prefer an upstream-supplied request-id (load balancer / proxy)
    # so audit events correlate to ingest logs; mint one if missing.
    request_id_header = request.headers.get("x-request-id")
    try:
        request_id = _uuid.UUID(request_id_header) if request_id_header else _uuid.uuid4()
    except ValueError:
        request_id = _uuid.uuid4()

    return EmitContext(
        actor_type="user" if ctx.auth_method == "session" else "service_account",
        actor_id=str(ctx.user_id or ctx.key_id),
        actor_display=ctx.key_prefix,
        project_id=ctx.project_id,
        request_id=request_id,
        source_ip=source_ip,
        user_agent=user_agent,
        api_key_id=str(ctx.key_id) if ctx.auth_method == "apikey" else None,
        auth_method=ctx.auth_method,
    )


def require_role(*allowed_roles: str):
    """Build a FastAPI dependency that requires session auth with a specific role.

    Usage:
        @router.post("/v1/projects/{slug}/members")
        async def add_member(
            ctx: ApiKeyContext = Depends(require_role("owner", "admin")),
            ...
        ):
            ...

    Only works with session-based auth. API keys don't have roles.
    Returns HTTP 403 if the user's role is not in allowed_roles.
    """
    async def _checker(
        request: Request,
        authorization: str | None = Header(default=None),
        x_project_id: str | None = Header(default=None, alias="X-Project-Id"),
        session: AsyncSession = Depends(get_db_session),
    ) -> auth.ApiKeyContext:
        ctx = await _authenticated(request, session, authorization, x_project_id)
        # API keys with wildcard scope can also access role-gated endpoints
        if ctx.auth_method == "apikey" and auth.key_has_scope(ctx.scopes, auth.SCOPE_WILDCARD):
            return ctx
        if ctx.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"requires role: {' or '.join(allowed_roles)}",
            )
        return ctx

    return _checker
