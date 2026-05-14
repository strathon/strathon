"""
Strathon Receiver
=================
FastAPI app that accepts OpenTelemetry/HTTP traces and persists them to Postgres.

Endpoints:
- GET  /health                    - liveness probe
- POST /v1/traces                 - OTLP/HTTP ingestion (protobuf)
- POST /v1/intervention/sync      - SDK polls for current budget/halt state
- POST /v1/intervention/halt      - Dashboard manually halts a trace or agent
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import asyncpg
from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue

import policies as policy_mod
from policies import PolicyExpressionError
import auth
import sampling
import retention
import metrics as metrics_mod
import logging_config

# Set up logging FIRST so any subsequent module-level logger.info()s use our format
_active_log_format = logging_config.configure_logging()

logger = logging.getLogger("strathon.receiver")
logger.info("Logging configured: format=%s", _active_log_format)


# Accept the SQLAlchemy-style URL from env but strip the driver suffix for asyncpg
_RAW_DB_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://strathon:strathon_dev@localhost:5432/strathon",
)
ASYNCPG_URL = _RAW_DB_URL.replace("postgresql+asyncpg://", "postgresql://")

# Default project for v0; will be replaced by per-API-key resolution
DEFAULT_PROJECT_SLUG = "default"


# OTel span kind enum → string mapping (matches schema CHECK constraint)
SPAN_KIND_NAMES = {
    0: "UNSPECIFIED",
    1: "INTERNAL",
    2: "SERVER",
    3: "CLIENT",
    4: "PRODUCER",
    5: "CONSUMER",
}

STATUS_CODE_NAMES = {
    0: "UNSET",
    1: "OK",
    2: "ERROR",
}


def any_value_to_python(av: AnyValue) -> Any:
    """Convert OTel AnyValue protobuf to a native Python value."""
    if av.HasField("string_value"):
        return av.string_value
    if av.HasField("bool_value"):
        return av.bool_value
    if av.HasField("int_value"):
        return av.int_value
    if av.HasField("double_value"):
        return av.double_value
    if av.HasField("array_value"):
        return [any_value_to_python(v) for v in av.array_value.values]
    if av.HasField("kvlist_value"):
        return {kv.key: any_value_to_python(kv.value) for kv in av.kvlist_value.values}
    if av.HasField("bytes_value"):
        return av.bytes_value.hex()
    return None


def attrs_to_dict(attrs) -> dict:
    """Convert a list of OTel KeyValue protobufs to a Python dict."""
    return {kv.key: any_value_to_python(kv.value) for kv in attrs}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Set up asyncpg connection pool and ensure the default project row exists."""
    logger.info("Strathon receiver starting; connecting to Postgres")
    app.state.pool = await asyncpg.create_pool(ASYNCPG_URL, min_size=2, max_size=10)

    async with app.state.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO projects (name, slug)
            VALUES ('Default', $1)
            ON CONFLICT (slug) DO UPDATE SET updated_at = NOW()
            RETURNING id
            """,
            DEFAULT_PROJECT_SLUG,
        )
        app.state.default_project_id = row["id"]
        logger.info("Default project id: %s", app.state.default_project_id)

    # Sampling config (env-driven) + counters for /metrics in C4
    app.state.sampling_config = sampling.SamplingConfig.from_env()
    app.state.sampling_counters = sampling.SamplingCounters()
    logger.info(
        "Sampling rate: %.3f (expensive LLM threshold: %d tokens)",
        app.state.sampling_config.sample_rate,
        app.state.sampling_config.expensive_llm_token_threshold,
    )

    # Prometheus metrics container — exposed at /metrics
    app.state.metrics = metrics_mod.StrathonMetrics()
    app.state.metrics.sampling_rate.set(app.state.sampling_config.sample_rate)

    # Retention background task
    app.state.retention_config = retention.RetentionConfig.from_env()
    app.state.retention_shutdown = asyncio.Event()
    retention_counters = metrics_mod.RetentionCounters(app.state.metrics)
    app.state.retention_task = asyncio.create_task(
        retention.retention_loop(
            app.state.pool,
            app.state.retention_config,
            app.state.retention_shutdown,
            metrics_counters=retention_counters,
        ),
        name="strathon.retention",
    )

    yield

    logger.info("Strathon receiver shutting down")

    # Stop the retention loop cleanly
    app.state.retention_shutdown.set()
    try:
        await asyncio.wait_for(app.state.retention_task, timeout=10)
    except asyncio.TimeoutError:
        logger.warning("retention task did not stop in 10s; cancelling")
        app.state.retention_task.cancel()
        try:
            await app.state.retention_task
        except (asyncio.CancelledError, Exception):
            pass

    await app.state.pool.close()


app = FastAPI(
    title="Strathon Receiver",
    version="0.0.1",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok", "service": "strathon-receiver", "version": "0.0.1"}


@app.get("/metrics")
async def metrics_endpoint() -> Response:
    """Prometheus exposition endpoint.

    Unauthenticated by design — Prometheus scrapers commonly run without
    credentials. If you don't want it public, restrict via network ACL or
    reverse proxy.
    """
    # Mirror the latest SamplingCounters snapshot into the Prom counters
    snapshot = app.state.sampling_counters.snapshot()
    metrics_mod.sync_sampling_counters(app.state.metrics, snapshot)
    # Keep the sampling_rate gauge accurate in case it could ever change
    app.state.metrics.sampling_rate.set(app.state.sampling_config.sample_rate)

    body, content_type = metrics_mod.render_metrics(app.state.metrics)
    return Response(content=body, media_type=content_type)


async def _authenticated(authorization: str | None) -> auth.ApiKeyContext:
    """Resolve an API key and record success / failure on the metrics counters.

    Wraps ``auth.resolve_api_key`` so every protected endpoint contributes to
    the auth_successes / auth_failures Prometheus counters. The auth module
    itself stays metrics-free (no circular import on app.state.metrics).
    """
    try:
        ctx = await auth.resolve_api_key(app.state.pool, authorization)
    except HTTPException:
        app.state.metrics.auth_failures.inc()
        raise
    app.state.metrics.auth_successes.inc()
    return ctx


@app.post("/v1/traces", status_code=status.HTTP_200_OK)
async def ingest_traces(
    request: Request,
    authorization: str | None = Header(default=None),
    content_type: str | None = Header(default=None),
) -> Response:
    """
    OTLP/HTTP trace ingestion endpoint.

    Accepts protobuf-encoded ExportTraceServiceRequest, parses spans, persists
    each span to the traces and spans tables. Returns OTLP-spec
    ExportTraceServiceResponse (empty body on success).
    """
    # Authenticate and resolve which project this batch belongs to
    auth_ctx = await _authenticated(authorization)
    project_id = auth_ctx.project_id

    body = await request.body()

    req = ExportTraceServiceRequest()
    try:
        req.ParseFromString(body)
    except Exception as exc:
        logger.warning("Failed to parse OTLP body (%d bytes): %s", len(body), exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid OTLP protobuf: {exc}",
        )

    span_count = 0
    trace_ids_seen: set[bytes] = set()

    # Pull active policies once for this batch; cheap and gives consistent
    # evaluation across all spans in a single ingest call.
    try:
        active_policies = await policy_mod.list_policies(
            app.state.pool, project_id, only_enabled=True
        )
    except Exception:
        logger.exception("failed to load policies for ingest; proceeding without policy eval")
        active_policies = []

    # Collected (policy, trace_id, span_id, outcome) tuples for audit logging
    # after the main insert transaction commits. Webhooks fire after as well.
    matches_to_record: list[dict[str, Any]] = []
    webhooks_to_fire: list[tuple[str, dict[str, Any]]] = []

    async with app.state.pool.acquire() as conn:
        async with conn.transaction():
            for resource_spans in req.resource_spans:
                resource_attrs = attrs_to_dict(resource_spans.resource.attributes)

                for scope_spans in resource_spans.scope_spans:
                    for span in scope_spans.spans:
                        trace_id = span.trace_id
                        span_id = span.span_id
                        parent_span_id = span.parent_span_id if span.parent_span_id else None

                        span_attrs = attrs_to_dict(span.attributes)
                        merged_attrs = {**resource_attrs, **span_attrs}

                        # Evaluate policies against this span. For log/alert we
                        # annotate the span attributes and record the match;
                        # block/steer policies are SDK-side and the SDK has
                        # already enforced them, but we still surface a record
                        # of the match here for visibility.
                        #
                        # We buffer per-span matches/webhooks locally so that
                        # if the sampling decision drops the span, we don't
                        # leave dangling audit rows pointing to a span that
                        # was never persisted.
                        span_matches: list[dict[str, Any]] = []
                        span_webhooks: list[tuple[str, dict[str, Any]]] = []

                        matched_policies = policy_mod.evaluate_for_span(
                            active_policies, span.name, merged_attrs
                        )
                        if matched_policies:
                            matched_ids = [p["id"] for p in matched_policies]
                            matched_actions = sorted({p["action"] for p in matched_policies})
                            merged_attrs["strathon.policy.matched_ids"] = ",".join(matched_ids)
                            merged_attrs["strathon.policy.matched_actions"] = ",".join(matched_actions)
                            for p in matched_policies:
                                outcome = {
                                    "log": "logged",
                                    "alert": "alert_queued",
                                    "block": "block_recorded",
                                    "steer": "steer_recorded",
                                }.get(p["action"], "recorded")
                                span_matches.append({
                                    "policy_id": p["id"],
                                    "trace_id": trace_id,
                                    "span_id": span_id,
                                    "action": p["action"],
                                    "outcome": outcome,
                                    "metadata": {
                                        "span_name": span.name,
                                        "policy_name": p["name"],
                                    },
                                })
                                if p["action"] == "alert":
                                    webhook_url = (p.get("action_config") or {}).get("webhook_url")
                                    if webhook_url:
                                        span_webhooks.append((webhook_url, {
                                            "policy_id": p["id"],
                                            "policy_name": p["name"],
                                            "span_name": span.name,
                                            "trace_id": trace_id.hex(),
                                            "span_id": span_id.hex(),
                                            "attrs": merged_attrs,
                                        }))

                        # ---- Sampling decision ----
                        # Made AFTER policy evaluation so the always-keep
                        # rules can see strathon.policy.* annotations.
                        status_code_name = STATUS_CODE_NAMES.get(span.status.code, "UNSET")
                        keep, force_kept = sampling.should_keep_span(
                            trace_id,
                            merged_attrs,
                            status_code_name,
                            app.state.sampling_config,
                        )
                        if not keep:
                            app.state.sampling_counters.record_dropped()
                            continue
                        app.state.sampling_counters.record_kept(force_kept=force_kept)

                        # Commit this span's matches/webhooks to the outer
                        # lists now that we know we're persisting the span.
                        matches_to_record.extend(span_matches)
                        webhooks_to_fire.extend(span_webhooks)

                        # Denormalize common gen_ai.* and strathon.agent.* fields
                        operation_name = span_attrs.get("gen_ai.operation.name")
                        provider_name = (
                            span_attrs.get("gen_ai.provider.name")
                            or span_attrs.get("gen_ai.system")
                        )
                        request_model = span_attrs.get("gen_ai.request.model")
                        response_model = span_attrs.get("gen_ai.response.model")
                        agent_name = (
                            span_attrs.get("gen_ai.agent.name")
                            or span_attrs.get("strathon.agent.name")
                        )
                        agent_id = (
                            span_attrs.get("gen_ai.agent.id")
                            or span_attrs.get("strathon.agent.id")
                        )
                        tool_name = span_attrs.get("gen_ai.tool.name")
                        workflow_name = span_attrs.get("gen_ai.workflow.name")
                        conversation_id = span_attrs.get("gen_ai.conversation.id")

                        input_tokens = span_attrs.get("gen_ai.usage.input_tokens")
                        output_tokens = span_attrs.get("gen_ai.usage.output_tokens")

                        # Upsert trace row before inserting the span (FK requirement)
                        if trace_id not in trace_ids_seen:
                            await conn.execute(
                                """
                                INSERT INTO traces (id, project_id, start_time_unix_nano, agent_name)
                                VALUES ($1, $2, $3, $4)
                                ON CONFLICT (id) DO NOTHING
                                """,
                                trace_id,
                                project_id,
                                span.start_time_unix_nano,
                                agent_name,
                            )
                            trace_ids_seen.add(trace_id)

                        await conn.execute(
                            """
                            INSERT INTO spans (
                                trace_id, span_id, parent_span_id, project_id,
                                name, kind, start_time_unix_nano, end_time_unix_nano,
                                status_code, status_message,
                                operation_name, provider_name, request_model, response_model,
                                agent_name, agent_id, tool_name, workflow_name, conversation_id,
                                input_tokens, output_tokens,
                                attributes
                            )
                            VALUES (
                                $1, $2, $3, $4,
                                $5, $6, $7, $8,
                                $9, $10,
                                $11, $12, $13, $14,
                                $15, $16, $17, $18, $19,
                                $20, $21,
                                $22::jsonb
                            )
                            ON CONFLICT (trace_id, span_id) DO UPDATE SET
                                end_time_unix_nano = EXCLUDED.end_time_unix_nano,
                                status_code = EXCLUDED.status_code,
                                status_message = EXCLUDED.status_message,
                                attributes = spans.attributes || EXCLUDED.attributes
                            """,
                            trace_id, span_id, parent_span_id, project_id,
                            span.name,
                            SPAN_KIND_NAMES.get(span.kind, "UNSPECIFIED"),
                            span.start_time_unix_nano,
                            span.end_time_unix_nano if span.end_time_unix_nano else None,
                            STATUS_CODE_NAMES.get(span.status.code, "UNSET"),
                            span.status.message or None,
                            operation_name, provider_name, request_model, response_model,
                            agent_name, agent_id, tool_name, workflow_name, conversation_id,
                            input_tokens, output_tokens,
                            json.dumps(merged_attrs),
                        )
                        span_count += 1

    logger.info(
        "Ingested %d spans across %d traces",
        span_count,
        len(trace_ids_seen),
        extra={
            "spans_ingested": span_count,
            "traces_seen": len(trace_ids_seen),
            "project_id": str(project_id),
        },
    )

    # Record policy matches in audit log (best-effort; never blocks ingest)
    for m in matches_to_record:
        await policy_mod.record_match(
            app.state.pool,
            UUID(m["policy_id"]) if not isinstance(m["policy_id"], UUID) else m["policy_id"],
            project_id,
            m["trace_id"],
            m["span_id"],
            m["action"],
            m["outcome"],
            metadata=m.get("metadata"),
        )
        app.state.metrics.policy_matches.labels(action=m["action"]).inc()

    # Fire alert webhooks in the background (don't block the response)
    for webhook_url, payload in webhooks_to_fire:
        asyncio.create_task(policy_mod.fire_webhook(webhook_url, payload))

    # OTLP spec requires returning ExportTraceServiceResponse on success
    resp = ExportTraceServiceResponse()
    return Response(
        content=resp.SerializeToString(),
        media_type="application/x-protobuf",
        status_code=status.HTTP_200_OK,
    )


# ============================================================
# Policy management API
# ============================================================
# These endpoints power runtime intervention. SDKs poll GET /v1/policies
# for client-side block/steer enforcement; humans use POST/PATCH/DELETE
# to manage rules.

def _coerce_project_id(value: str | None) -> UUID:
    """For v0 we resolve everything to the default project."""
    if value:
        try:
            return UUID(value)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"invalid project_id: {value}",
            )
    return app.state.default_project_id


async def _require_auth(
    authorization: str | None = Header(default=None),
) -> auth.ApiKeyContext:
    """FastAPI dependency that resolves the Bearer token to a project context."""
    return await _authenticated(authorization)


@app.get("/v1/policies")
async def list_policies_endpoint(
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    ctx = await _authenticated(authorization)
    policies = await policy_mod.list_policies(app.state.pool, ctx.project_id)
    return {"policies": policies}


@app.post("/v1/policies", status_code=status.HTTP_201_CREATED)
async def create_policy_endpoint(
    payload: dict[str, Any],
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    ctx = await _authenticated(authorization)
    required = {"name", "match_expression", "action"}
    missing = required - set(payload.keys())
    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"missing required fields: {sorted(missing)}",
        )
    try:
        policy = await policy_mod.create_policy(
            app.state.pool,
            ctx.project_id,
            name=payload["name"],
            description=payload.get("description"),
            match_expression=payload["match_expression"],
            action=payload["action"],
            action_config=payload.get("action_config"),
            applies_to=payload.get("applies_to"),
            enabled=payload.get("enabled", True),
            priority=payload.get("priority", 0),
        )
    except PolicyExpressionError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid match expression: {exc}",
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    return policy


@app.get("/v1/policies/{policy_id}")
async def get_policy_endpoint(
    policy_id: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    ctx = await _authenticated(authorization)
    try:
        pid_uuid = UUID(policy_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid policy_id")
    policy = await policy_mod.get_policy(app.state.pool, ctx.project_id, pid_uuid)
    if not policy:
        raise HTTPException(status_code=404, detail="policy not found")
    return policy


@app.patch("/v1/policies/{policy_id}")
async def update_policy_endpoint(
    policy_id: str,
    payload: dict[str, Any],
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    ctx = await _authenticated(authorization)
    try:
        pid_uuid = UUID(policy_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid policy_id")
    try:
        policy = await policy_mod.update_policy(
            app.state.pool, ctx.project_id, pid_uuid, **payload
        )
    except PolicyExpressionError as exc:
        raise HTTPException(status_code=400, detail=f"invalid match expression: {exc}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not policy:
        raise HTTPException(status_code=404, detail="policy not found")
    return policy


@app.delete("/v1/policies/{policy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_policy_endpoint(
    policy_id: str,
    authorization: str | None = Header(default=None),
) -> Response:
    ctx = await _authenticated(authorization)
    try:
        pid_uuid = UUID(policy_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid policy_id")
    deleted = await policy_mod.delete_policy(app.state.pool, ctx.project_id, pid_uuid)
    if not deleted:
        raise HTTPException(status_code=404, detail="policy not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ============================================================
# API key management
# ============================================================
# These endpoints are UNAUTHENTICATED in v1. Production deployments must
# put the receiver behind a reverse proxy that restricts access here, or
# add admin authentication (v2 work).

@app.get("/v1/api_keys")
async def list_api_keys_endpoint(
    project_id: str | None = None,
    include_revoked: bool = False,
) -> dict[str, Any]:
    pid = _coerce_project_id(project_id)
    keys = await auth.list_api_keys(app.state.pool, pid, include_revoked=include_revoked)
    return {"api_keys": keys}


@app.post("/v1/api_keys", status_code=status.HTTP_201_CREATED)
async def create_api_key_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    name = payload.get("name")
    if not name:
        raise HTTPException(status_code=400, detail="missing required field: name")
    pid = _coerce_project_id(payload.get("project_id"))
    row, raw_key = await auth.create_api_key(app.state.pool, pid, name=name)
    # The raw key is returned ONCE. Callers must save it; it cannot be retrieved later.
    return {**row, "key": raw_key}


@app.delete("/v1/api_keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key_endpoint(key_id: str) -> Response:
    try:
        kid_uuid = UUID(key_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid key_id")
    revoked = await auth.revoke_api_key(app.state.pool, kid_uuid)
    if not revoked:
        raise HTTPException(status_code=404, detail="api key not found or already revoked")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/v1/intervention/sync")
async def intervention_sync(payload: dict[str, Any]) -> dict[str, Any]:
    """Deprecated stub kept for SDK backward compatibility."""
    return {"halts": [], "budgets": [], "synced_at_unix_nano": 0}


@app.post("/v1/intervention/halt", status_code=status.HTTP_201_CREATED)
async def intervention_halt(payload: dict[str, Any]) -> dict[str, Any]:
    """Deprecated stub kept for SDK backward compatibility."""
    logger.info("Halt request: %s", payload)
    return {"halted": True}


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled exception", exc_info=exc)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"error": "internal_server_error"},
    )
