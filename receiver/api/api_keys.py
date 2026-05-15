"""API key management endpoints.

NOTE: these endpoints currently DO NOT authenticate the caller. Anyone
who can reach the receiver can list/create/revoke keys. This is a known
gap (predates the api/ restructure — preserved here as-is to keep stage
6a behavior-preserving) and is on the v1 backlog to fix. The intended
fix is capability scopes on api_keys plus a require_scope dependency.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

import repositories.auth as auth_repo
from database import get_db_session

from ._deps import coerce_project_id


router = APIRouter(prefix="/v1/api_keys", tags=["api_keys"])


@router.get("")
async def list_api_keys_endpoint(
    request: Request,
    project_id: str | None = None,
    include_revoked: bool = False,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    pid = coerce_project_id(request, project_id)
    keys = await auth_repo.list_api_keys(session, pid, include_revoked=include_revoked)
    # mode="json" coerces UUIDs to strings and datetimes to ISO strings,
    # preserving the response shape the previous asyncpg-based serializer
    # produced.
    return {"api_keys": [k.model_dump(mode="json") for k in keys]}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_api_key_endpoint(
    payload: dict[str, Any],
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    name = payload.get("name")
    if not name:
        raise HTTPException(status_code=400, detail="missing required field: name")
    pid = coerce_project_id(request, payload.get("project_id"))
    response = await auth_repo.create_api_key(session, pid, name=name)
    # The raw key is returned ONCE. Callers must save it; it cannot be
    # retrieved later. Response shape matches the previous asyncpg-based
    # endpoint: api_key fields flattened at the top level, plus "key" for
    # the raw secret.
    return {**response.api_key.model_dump(mode="json"), "key": response.raw_key}


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key_endpoint(
    key_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    try:
        kid_uuid = UUID(key_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid key_id")
    revoked = await auth_repo.revoke_api_key(session, kid_uuid)
    if not revoked:
        raise HTTPException(status_code=404, detail="api key not found or already revoked")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
