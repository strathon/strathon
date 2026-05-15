"""Health and metrics endpoints.

Both are unauthenticated by design: Prometheus scrapers and liveness
probes commonly run without credentials. Operators who want to restrict
them should do so at the network layer (ACL, reverse proxy).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

import metrics as metrics_mod


router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok", "service": "strathon-receiver", "version": "0.0.1"}


@router.get("/metrics")
async def metrics_endpoint(request: Request) -> Response:
    """Prometheus exposition endpoint."""
    state = request.app.state
    # Mirror the latest SamplingCounters snapshot into the Prom counters
    snapshot = state.sampling_counters.snapshot()
    metrics_mod.sync_sampling_counters(state.metrics, snapshot)
    # Keep the sampling_rate gauge accurate in case it could ever change
    state.metrics.sampling_rate.set(state.sampling_config.sample_rate)

    body, content_type = metrics_mod.render_metrics(state.metrics)
    return Response(content=body, media_type=content_type)
