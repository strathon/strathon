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

import logging
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

import auth
from database import get_db_session

logger = logging.getLogger(__name__)


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

    # IP allowlist check: if the key has allowed_ips set, reject
    # requests from IPs not in the list.
    if ctx.allowed_ips:
        client_ip = request.client.host if request.client else None
        if client_ip and client_ip not in ctx.allowed_ips:
            metrics.auth_failures.inc()
            logger.warning(
                "IP %s not in allowlist for key %s (project %s)",
                client_ip, ctx.key_prefix, ctx.project_id,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="request IP not in key's allowed_ips",
            )

    metrics.auth_successes.inc()

    # Make ctx.project_id visible to Postgres RLS for the rest of this
    # request. is_local=true scopes it to the current transaction so it
    # can never leak to a different request that reuses this pooled
    # connection. This is a no-op in today's default self-hosted
    # deployment (DATABASE_URL connects as the table owner, which
    # bypasses RLS regardless of this setting -- see migration 021's own
    # comment). It becomes load-bearing only if DATABASE_URL is later
    # pointed at a non-owner role (e.g. strathon_app from migration 030),
    # which is what turns RLS from documented-but-inert into an active
    # second enforcement layer alongside the WHERE project_id = ... that
    # every repository query already applies. Failure here must never
    # break a request that's already been correctly authenticated and
    # scoped by the application layer, so it's best-effort.
    try:
        from sqlalchemy import text as _sql_text
        await session.execute(
            _sql_text("SELECT set_config('app.current_tenant', :pid, true)"),
            {"pid": str(ctx.project_id)},
        )
    except Exception:
        logger.debug("Strathon: could not set app.current_tenant RLS context", exc_info=True)

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


def require_instance_admin():
    """Build a dependency that requires the instance-level admin credential.

    For operations that span every tenant on the instance rather than a single
    project -- currently the audit anchor chain, which is one instance-wide
    Merkle chain over all events (audit.anchors has no project_id column, so it
    cannot be scoped per-project). Exposing it under a per-project scope leaked
    every tenant's anchor timing and volume to any project's audit:read key.

    Modeled on the instance-admin key pattern used by mature multi-tenant
    observability platforms (a single instance credential, distinct from any
    project/org key, constant-time compared). Behavior:
      - Presented as ``Authorization: Bearer <STRATHON_ADMIN_API_KEY>``.
      - Fails CLOSED: if the key is unset the endpoint returns 503, never
        falling back to per-project auth or allowing the request. This matters
        because the endpoint guards cross-tenant data -- a missing key must
        deny, not open.
      - Constant-time comparison via ``hmac.compare_digest``.

    Why 503 when unset (not 403): the credential isn't wrong, the capability is
    unconfigured on this instance, so the operator must set the key. Why 401 on
    a bad/absent token: the credential is invalid.
    """
    async def _checker(
        authorization: str | None = Header(default=None),
    ) -> None:
        await _instance_admin_check(authorization)

    return _checker


async def _instance_admin_check(authorization: str | None) -> None:
    """Validate the instance-admin credential, failing closed when unset.

    Shared by ``require_instance_admin`` and ``require_anchor_list_access`` so
    the admin-key semantics live in one place.
    """
    import hmac

    from config import get_settings

    admin_key = get_settings().admin_api_key
    if not admin_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "instance-admin operations are unavailable: "
                "STRATHON_ADMIN_API_KEY is not configured"
            ),
        )
    scheme, _, token = (authorization or "").partition(" ")
    if scheme != "Bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="instance-admin credential required",
        )
    if not hmac.compare_digest(token.encode(), admin_key.encode()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid instance-admin credential",
        )


async def require_anchor_list_access(
    request: "Request",
    authorization: str | None = Header(default=None),
    x_project_id: str | None = Header(default=None, alias="X-Project-Id"),
    session: "AsyncSession" = Depends(get_db_session),
) -> None:
    """Gate the full audit anchor chain listing, correctly for each mode.

    The anchor chain is one instance-wide Merkle chain over every event
    (audit.anchors has no project_id column). What guarding it requires depends
    on whether the instance is multi-tenant:

      - self-hosted (single tenant): the whole instance is one organization, so
        the chain only ever covers that organization's events. There is no
        cross-tenant data to protect, so a normal ``audit:read`` key is
        sufficient -- the scope the endpoint used before. Requiring a separate
        admin key here would lock a single operator out of their own integrity
        chain for no security gain.
      - cloud (multi-tenant): the chain spans every tenant, so a project's
        ``audit:read`` key must not see it. It requires the instance-admin
        credential (STRATHON_ADMIN_API_KEY), failing closed when unset.

    The per-project ``/anchors/status`` endpoint stays on ``audit:read`` in both
    modes; only this full-chain listing is mode-gated.

    This is a plain request-time dependency, not a factory. Choosing the mode
    here (rather than in a factory used inside ``Depends(...)``) keeps the
    decision at request time; doing it at import time would call get_settings()
    -- and so require DATABASE_URL -- merely to import the module, breaking
    ``import main`` in any environment without a configured database.
    """
    from config import get_settings

    if get_settings().is_cloud:
        await _instance_admin_check(authorization)
        return
    # Self-hosted: single tenant, so audit:read is the correct scope.
    ctx = await _authenticated(request, session, authorization, x_project_id)
    if not auth.key_has_scope(ctx.scopes, auth.SCOPE_AUDIT_READ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"missing required scope: {auth.SCOPE_AUDIT_READ}",
        )


async def enforce_reauth(request: "Request", ctx: "auth.ApiKeyContext", session) -> None:
    """Enforce step-up re-authentication for a high-impact operation.

    Call this at the top of endpoints where a stolen session cookie must not be
    sufficient on its own: creating API keys, deleting projects, disabling MFA,
    changing the password. The user must re-confirm with their current password
    (X-Confirm-Password header) or a fresh MFA code (X-Confirm-MFA). Modeled on
    OWASP ASVS V3.3.4 (re-authentication before sensitive transactions).

    No-op for API-key auth (already capability-scoped); only session (dashboard)
    auth is challenged. Raises 403 if the confirmation is missing or wrong.
    """
    await auth.require_reauth(
        ctx,
        session,
        confirm_password=request.headers.get("X-Confirm-Password"),
        confirm_mfa=request.headers.get("X-Confirm-MFA"),
    )


def coerce_project_id(
    request: Request,
    value: str | None,
    ctx: "auth.ApiKeyContext | None" = None,
) -> UUID:
    """Resolve the project context for a request.

    When an authenticated context is supplied, its project_id is
    authoritative: for session auth it is the X-Project-Id the user selected,
    already validated against their membership; for API-key auth it is the
    key's own project. This prevents an API key from reaching another
    project by spoofing a header.

    Falls back to an explicit value, then the app default, for callers that
    pre-date context threading.
    """
    if ctx is not None and ctx.project_id is not None:
        return ctx.project_id
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
        actor_type="human" if ctx.auth_method == "session" else "service_account",
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
