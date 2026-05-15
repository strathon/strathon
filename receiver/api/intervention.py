"""Intervention endpoint stubs kept for SDK backward compatibility.

These were part of the early SDK-pull-based intervention design. The
current architecture uses server-side policy evaluation on ingest plus
SDK-side block/steer at tool boundaries, so these endpoints are dead
weight for new deployments — but old SDK versions in the wild still
poll them. Keep returning the empty/no-op shape until SDK telemetry
shows zero callers.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, status


logger = logging.getLogger("strathon.receiver.intervention")


router = APIRouter(prefix="/v1/intervention", tags=["intervention"])


@router.post("/sync")
async def intervention_sync(payload: dict[str, Any]) -> dict[str, Any]:
    """Deprecated stub kept for SDK backward compatibility."""
    return {"halts": [], "budgets": [], "synced_at_unix_nano": 0}


@router.post("/halt", status_code=status.HTTP_201_CREATED)
async def intervention_halt(payload: dict[str, Any]) -> dict[str, Any]:
    """Deprecated stub kept for SDK backward compatibility."""
    logger.info("Halt request: %s", payload)
    return {"halted": True}
