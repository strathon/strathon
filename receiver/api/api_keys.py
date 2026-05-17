"""API key management endpoints.

Every endpoint here is scope-protected:
  - GET    /v1/api_keys         requires api_keys:read
  - POST   /v1/api_keys         requires api_keys:write
  - DELETE /v1/api_keys/{id}    requires api_keys:write

The seeded development key (migration 003) holds the '*' wildcard
(applied by migration 004), so the out-of-box demo flow continues to
work. To rotate to production:

  1. Use the dev key to call POST /v1/api_keys with the scopes the new
     key needs (typically ['traces:write', 'policies:read'] for an SDK
     key, or ['*'] for a replacement admin key).
  2. Revoke the seeded dev key via DELETE /v1/api_keys/<dev-key-id>.

Until step 2, the dev key remains a known-cleartext credential. The
banner printed at startup keeps reminding operators of this.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

import auth as auth_mod
import repositories.audit as audit_repo
import repositories.auth as auth_repo
from audit.actions import (
    API_KEY_CREATE,
    API_KEY_REVOKE,
    CATEGORY_API_KEY,
)
from database import get_db_session

from ._deps import build_audit_context, coerce_project_id, require_scope


router = APIRouter(prefix="/v1/api_keys", tags=["api_keys"])


@router.get("")
async def list_api_keys_endpoint(
    request: Request,
    project_id: str | None = None,
    include_revoked: bool = False,
    ctx: auth_mod.ApiKeyContext = Depends(  # noqa: ARG001 - dependency runs auth+scope check
        require_scope(auth_mod.SCOPE_API_KEYS_READ)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    pid = coerce_project_id(request, project_id)
    keys = await auth_repo.list_api_keys(session, pid, include_revoked=include_revoked)
    return {"api_keys": [k.model_dump(mode="json") for k in keys]}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_api_key_endpoint(
    payload: dict[str, Any],
    request: Request,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_API_KEYS_WRITE)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    name = payload.get("name")
    if not name:
        raise HTTPException(status_code=400, detail="missing required field: name")

    # Validate scopes if the caller provided them. Unknown scope strings
    # silently granting nothing would be hostile to debug; reject with a
    # clear message listing what's valid.
    requested_scopes = payload.get("scopes")
    if requested_scopes is not None:
        if not isinstance(requested_scopes, list):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="scopes must be a list of strings",
            )
        try:
            auth_mod.validate_scopes(requested_scopes)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            )

    pid = coerce_project_id(request, payload.get("project_id"))
    response = await auth_repo.create_api_key(
        session, pid, name=name, scopes=requested_scopes
    )
    # The raw key is returned ONCE. Callers must save it; it cannot be
    # retrieved later. Response shape: api_key fields flattened at the
    # top level, plus "key" for the raw secret.
    #
    # Audit: log creation, but NEVER include the raw key bytes. The
    # redaction module would strip "key" / "value" anyway, but excluding
    # at the source is the defensive path. We record the prefix (a
    # public identifier that appears in DB and logs) and the scope set.
    api_key_dict = response.api_key.model_dump(mode="json")
    await audit_repo.emit(
        session,
        build_audit_context(request, ctx),
        API_KEY_CREATE,
        CATEGORY_API_KEY,
        resource_type="api_key",
        resource_id=str(response.api_key.id),
        after_state={
            "id": api_key_dict.get("id"),
            "name": api_key_dict.get("name"),
            "key_prefix": api_key_dict.get("key_prefix"),
            "scopes": api_key_dict.get("scopes"),
            "project_id": api_key_dict.get("project_id"),
        },
    )
    return {**api_key_dict, "key": response.raw_key}


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key_endpoint(
    key_id: str,
    request: Request,
    ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_API_KEYS_WRITE)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    try:
        kid_uuid = UUID(key_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid key_id")
    revoked = await auth_repo.revoke_api_key(session, kid_uuid)
    if not revoked:
        raise HTTPException(status_code=404, detail="api key not found or already revoked")
    await audit_repo.emit(
        session,
        build_audit_context(request, ctx),
        API_KEY_REVOKE,
        CATEGORY_API_KEY,
        resource_type="api_key",
        resource_id=str(kid_uuid),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
