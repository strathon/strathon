"""
Strathon Receiver
=================
FastAPI app that accepts OpenTelemetry/HTTP traces and writes them to Postgres.

v0 endpoints:
- GET  /health                    - liveness probe
- POST /v1/traces                 - OTLP/HTTP ingestion (gen_ai.* and strathon.agent.*)
- POST /v1/intervention/sync      - SDK polls for current budget/halt state
- POST /v1/intervention/halt      - Dashboard manually halts a trace or agent

This is the initial skeleton. Real OTLP parsing, persistence, and intervention
logic land in subsequent commits.
"""

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

logger = logging.getLogger("strathon.receiver")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "info").upper(),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown hooks. DB connection pool gets set up here."""
    logger.info("Strathon receiver starting")
    # TODO: initialize asyncpg / SQLAlchemy async engine
    yield
    logger.info("Strathon receiver shutting down")


app = FastAPI(
    title="Strathon Receiver",
    version="0.0.1",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok", "service": "strathon-receiver", "version": "0.0.1"}


@app.post("/v1/traces", status_code=status.HTTP_200_OK)
async def ingest_traces(
    request: Request,
    authorization: str | None = Header(default=None),
    content_type: str | None = Header(default=None),
) -> dict[str, Any]:
    """
    OTLP/HTTP trace ingestion endpoint.

    Current: accepts payload, logs, returns 200. No persistence yet.
    Planned: parse OTLP protobuf, validate API key, persist spans to Postgres,
    apply PII redaction at ingress, compute trace-level rollups.
    """
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header. Expected: Bearer <api_key>",
        )

    body = await request.body()
    logger.info(
        "Received trace payload: content_type=%s bytes=%d",
        content_type,
        len(body),
    )

    # TODO: parse OTLP, persist to DB
    return {"received": True, "bytes": len(body)}


@app.post("/v1/intervention/sync")
async def intervention_sync(payload: dict[str, Any]) -> dict[str, Any]:
    """
    SDK polls this to sync intervention state.

    Current: returns empty state (no halts).
    Planned: query halt_state table, return active halts for given agent_id/trace_id.
    """
    return {
        "halts": [],
        "budgets": [],
        "synced_at_unix_nano": 0,
    }


@app.post("/v1/intervention/halt", status_code=status.HTTP_201_CREATED)
async def intervention_halt(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Dashboard or external monitor manually halts a trace or agent.

    Current: logs and returns success.
    Planned: write to halt_state table with WAL semantics, fan out to SDK pollers.
    """
    logger.info("Halt request: %s", payload)
    return {"halted": True}


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled exception", exc_info=exc)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"error": "internal_server_error"},
    )
