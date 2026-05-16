"""SDK polling endpoint for active halts.

The SDK polls this endpoint periodically (default once per second) to
learn about halts that should stop subsequent tool/LLM calls. The
response shape is preserved from the original dead-stub design so
older SDK versions continue to work:

    POST /v1/intervention/sync
    -> {
         "halts": [
           {"id": 1, "scope": "agent", "scope_value": "agent-7",
            "state": "halted", "reason": "killswitch"},
           ...
         ],
         "budgets": [],
         "synced_at_unix_nano": 1715800000000000000
       }

``budgets`` stays empty in this commit; it'll be populated once
server-side budget rollup ships. The SDK is supposed to tolerate the
empty list and we want to preserve the wire shape so the SDK doesn't
need a coordinated update when budgets arrive.

The POST verb is preserved from the old design even though the
operation is read-only — the SDK historically sent its own state in
the body (current loop counts, spent cost) for the server to roll
up. v1 ignores the body but doesn't reject it, so old SDK versions
keep working without surprise.

Scope: ``halts:read``. Old SDK keys built before this commit have
``traces:write`` and ``policies:read`` only, so they'll get a 403
once the SDK is upgraded to actually use the halts they fetch. The
upgrade procedure is documented in docs/intervention.md (adding
``halts:read`` to the key, or rotating to a fresh key with default
scopes that include it). Operators with the wildcard dev key keep
working without intervention.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

import auth as auth_mod
import repositories.halts as halts_repo
from database import get_db_session

from ._deps import coerce_project_id, require_scope


logger = logging.getLogger("strathon.receiver.intervention")


router = APIRouter(prefix="/v1/intervention", tags=["intervention"])


@router.post("/sync")
async def intervention_sync(
    payload: dict[str, Any],
    request: Request,
    ctx: auth_mod.ApiKeyContext = Depends(  # noqa: ARG001
        require_scope(auth_mod.SCOPE_HALTS_READ)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Return active halts for the project.

    The body is currently ignored — earlier designs had the SDK push
    its current loop count / spent cost up here for the server to
    roll up. v1 reads, doesn't roll up. Future commits may consume
    body fields; the shape is preserved so the SDK doesn't need a
    coordinated update.
    """
    pid = coerce_project_id(request, None)
    halts = await halts_repo.get_active_halts_for_sync(session, pid)
    return {
        "halts": halts,
        "budgets": [],
        "synced_at_unix_nano": time.time_ns(),
    }


@router.post("/halt", status_code=status.HTTP_201_CREATED)
async def intervention_halt(
    payload: dict[str, Any],
    ctx: auth_mod.ApiKeyContext = Depends(  # noqa: ARG001
        require_scope(auth_mod.SCOPE_TRACES_WRITE)
    ),
) -> dict[str, Any]:
    """Deprecated stub kept for SDK backward compatibility.

    Old SDK versions called this to push a halt event UP to the
    receiver. The current design is the inverse: operators / server-
    side actors create halts via POST /v1/halts, the SDK reads them
    via POST /v1/intervention/sync. This endpoint stays so old SDKs
    don't 404; we log the call for visibility and return success.
    """
    logger.info("Deprecated /v1/intervention/halt called: %s", payload)
    return {"halted": True}
