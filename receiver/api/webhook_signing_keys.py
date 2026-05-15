"""Webhook signing-key management endpoints.

REST surface for operator key lifecycle:

  GET    /v1/webhook_signing_keys              list
  POST   /v1/webhook_signing_keys              create (plaintext returned ONCE)
  DELETE /v1/webhook_signing_keys/{id}         revoke immediately

Scopes:

  webhook_signing_keys:read   GET endpoints
  webhook_signing_keys:write  POST + DELETE

The create endpoint follows the same single-disclosure pattern Stripe,
Svix, GitHub, OpenAI, and the Standard Webhooks reference all use:
the plaintext signing secret is returned in the POST response body and
never visible to any subsequent endpoint. If the operator loses it,
they create a new key and revoke the lost one — there is no recovery.

The keystore (in-process plaintext cache) is updated atomically with
the DB write:

  * POST: the new plaintext goes into the keystore for the project so
          the next delivery signs with it (in addition to any other
          active keys, supporting Stripe-style overlapping rotation).
  * DELETE: the revoked plaintext is removed from the keystore for the
          project, so no further delivery signs with it.

If the receiver process restarts, the keystore is empty until the
operator re-supplies plaintexts via STRATHON_WEBHOOK_SIGNING_SECRETS or
creates new keys. Signed delivery does not survive process restart
without operator action — by design, because we don't persist plaintext.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

import auth as auth_mod
import repositories.webhook_signing_keys as keys_repo
from database import get_db_session
from webhooks.keystore import forget_secret_by_id, remember_secret

from ._deps import coerce_project_id, require_scope


router = APIRouter(prefix="/v1/webhook_signing_keys", tags=["webhook_signing_keys"])


@router.get("")
async def list_webhook_signing_keys(
    request: Request,
    project_id: str | None = None,
    include_revoked: bool = False,
    ctx: auth_mod.ApiKeyContext = Depends(  # noqa: ARG001
        require_scope(auth_mod.SCOPE_WEBHOOK_SIGNING_KEYS_READ)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """List webhook signing keys for the project.

    Returns id, prefix, created_at, revoked_at. The secret hash is
    never returned — even the auditor can only see the four-character
    prefix that identifies which key is which.
    """
    pid = coerce_project_id(request, project_id)
    rows = await keys_repo.list_keys(session, pid, include_revoked=include_revoked)
    return {"webhook_signing_keys": [r.to_json() for r in rows]}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_webhook_signing_key(
    payload: dict[str, Any] | None = None,
    *,
    request: Request,
    ctx: auth_mod.ApiKeyContext = Depends(  # noqa: ARG001
        require_scope(auth_mod.SCOPE_WEBHOOK_SIGNING_KEYS_WRITE)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Create a new signing key for the project.

    Response body fields (returned ONCE):

        id          uuid of the new key (use this to revoke it later)
        prefix      4-char public identifier
        secret      the plaintext whsec_* value — SAVE THIS NOW
        created_at  iso timestamp

    The secret is shown exactly once. There is no endpoint to retrieve
    it again. To rotate, create a second key (deliveries will sign with
    both), update consumers to accept the new key, then DELETE the old
    one. During the overlap window every webhook carries both signatures
    space-delimited in the webhook-signature header.
    """
    body = payload or {}
    pid = coerce_project_id(request, body.get("project_id"))

    result = await keys_repo.create_key(session, pid)

    # Push the plaintext into the in-process keystore so the next
    # delivery picks it up. Keyed by row id so DELETE can drop just
    # this entry cleanly without affecting other active keys.
    remember_secret(pid, result.plaintext, key_id=result.row.id)

    response = result.row.to_json()
    response["secret"] = result.plaintext
    return response


@router.delete("/{key_id}", status_code=status.HTTP_200_OK)
async def revoke_webhook_signing_key(
    key_id: str,
    request: Request,
    ctx: auth_mod.ApiKeyContext = Depends(  # noqa: ARG001
        require_scope(auth_mod.SCOPE_WEBHOOK_SIGNING_KEYS_WRITE)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Revoke a signing key.

    Sets revoked_at = NOW() in the database. The keystore entry for
    this key is also removed so the next delivery does NOT sign with
    it. Already-revoked keys return their existing state unchanged
    (idempotent).

    Returns the updated row (with revoked_at populated) for confirmation
    in operator dashboards. We don't return 204 here because operators
    want to see the timestamp the revocation took effect; an empty
    body would make them do another GET to confirm.

    The plaintext is NOT taken as input — that's the whole point of
    revoking by id. If an operator loses the plaintext and wants to
    invalidate it, the prefix in the GET listing tells them which row
    to delete.
    """
    try:
        kid_uuid = UUID(key_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid key_id")

    pid = coerce_project_id(request, None)
    revoked = await keys_repo.revoke_key(session, kid_uuid, pid)
    if revoked is None:
        raise HTTPException(
            status_code=404,
            detail="signing key not found or not owned by this project",
        )

    # Drop the plaintext from the keystore so the very next delivery
    # does NOT sign with this key. Other active keys for the project
    # remain untouched, so a rotation cutover (new key was created
    # first, this old key is being retired) keeps signing seamlessly.
    forget_secret_by_id(pid, kid_uuid)

    return revoked.to_json()


__all__ = ["router"]
