"""OTLP/HTTP trace ingestion endpoint.

This is the hot path. Every span the SDK emits arrives here. The
endpoint does five things, in order:

  1. Authenticate the request and pick a project_id
  2. Parse the OTLP protobuf payload
  3. Load active policies for the project (one query for the whole batch)
  4. For each span: evaluate policies, decide sampling, upsert trace+span
  5. Best-effort audit log + webhook fan-out after the writes settle

Transaction model:
    The FastAPI session (get_db_session) is the transaction. Everything
    persisted via `session` here commits atomically on a clean response
    or rolls back together on any raised exception. There is NO outer
    asyncpg pool transaction wrapping this — that pattern was removed in
    stage 5 of the ORM refactor.

Why parse OTLP protobuf manually rather than using opentelemetry-proto's
generated readers fully: speed and footprint. The protobuf classes are
fine; we just walk them by hand because the per-span hot path benefits
from staying close to the data.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import Response
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue
from sqlalchemy.ext.asyncio import AsyncSession

import auth as auth_mod
import policies as policy_mod
import pricing as pricing_mod
import redaction as redaction_mod
import repositories.policies as policies_repo
import repositories.project_settings as project_settings_repo
import repositories.traces as traces_repo
import sampling
import webhooks.dispatch as webhooks_dispatch
from database import get_db_session

from ._deps import require_scope


logger = logging.getLogger("strathon.receiver.traces")


router = APIRouter(tags=["traces"])


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


@router.post("/v1/traces", status_code=status.HTTP_200_OK)
async def ingest_traces(
    request: Request,
    content_type: str | None = Header(default=None),  # noqa: ARG001 - kept for OTLP clients that include it
    auth_ctx: auth_mod.ApiKeyContext = Depends(
        require_scope(auth_mod.SCOPE_TRACES_WRITE)
    ),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    """
    OTLP/HTTP trace ingestion endpoint.

    Accepts protobuf-encoded ExportTraceServiceRequest, parses spans,
    persists each span to the traces and spans tables. Returns the
    OTLP-spec ExportTraceServiceResponse (empty body on success).
    """
    state = request.app.state
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
    # evaluation across all spans in a single ingest call. The policy
    # repository returns Pydantic models — convert to dicts so the legacy
    # evaluate_for_span call sites (which accept the raw shape) keep working.
    try:
        policy_models = await policies_repo.list_policies(
            session, project_id, only_enabled=True
        )
        active_policies = [
            {**p.model_dump(mode="python"), "id": str(p.id)}
            for p in policy_models
        ]
    except Exception:
        logger.exception("failed to load policies for ingest; proceeding without policy eval")
        active_policies = []

    # Load the project's PII redaction config once for the whole batch.
    # Redaction is applied AFTER policy evaluation but BEFORE persistence
    # and webhook payload assembly, so that:
    #   * match expressions can reference unredacted content
    #     ("contains @competitor.com" still fires)
    #   * neither the spans table nor the webhook payload ever carries
    #     the raw PII downstream
    # A failure here logs and degrades to "no redaction" — same pattern
    # as policies above: ingest is never blocked by config issues.
    try:
        redaction_config = await project_settings_repo.load_redaction_config(
            session, project_id,
        )
    except Exception:
        logger.exception(
            "failed to load redaction config for project %s; proceeding without redaction",
            project_id,
        )
        redaction_config = redaction_mod.RedactionConfig.disabled()

    # Load the model pricing catalog (process-level cache after first call)
    # and the project's per-model overrides (one DB read per ingest batch).
    # The catalog drives per-span cost computation; spans get a cost_usd
    # column populated at insert time, which the budget monitor's
    # aggregation query later sums over a window.
    #
    # We DO NOT update any budget counter at ingest. The earlier counter
    # design serialized every span ingest on a single budgets row, which
    # becomes the bottleneck at scale. The write-once-aggregate-later
    # pattern that mature LLM observability backends use has zero
    # contention on the ingest path.
    pricing_catalog = pricing_mod.get_default_catalog()
    try:
        pricing_overrides = await pricing_mod.get_project_overrides(
            session, project_id,
        )
    except Exception:
        logger.exception(
            "failed to load price overrides for project %s; using catalog only",
            project_id,
        )
        pricing_overrides = {}

    # Collected (policy, trace_id, span_id, outcome) tuples for audit logging
    # after the main insert transaction commits. Webhooks fire after as well.
    matches_to_record: list[dict[str, Any]] = []
    # (url, payload, policy_id) — policy_id needed so the durable
    # webhook_deliveries row can foreign-key to the policy that triggered it.
    webhooks_to_fire: list[tuple[str, dict[str, Any], str]] = []

    # The FastAPI session is already the transaction. Everything written
    # via `session` here commits atomically when get_db_session's success
    # path fires, or rolls back together on any raised exception.
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
                span_webhooks: list[tuple[str, dict[str, Any], str]] = []

                matched_policies = policy_mod.evaluate_for_span(
                    active_policies, span.name, merged_attrs
                )
                if matched_policies:
                    matched_ids = [p["id"] for p in matched_policies]
                    matched_actions = sorted({p["action"] for p in matched_policies})
                    merged_attrs["strathon.policy.matched_ids"] = ",".join(matched_ids)
                    merged_attrs["strathon.policy.matched_actions"] = ",".join(matched_actions)
                    for p in matched_policies:
                        is_shadow = p.get("shadow", False)
                        # Shadow policies record matches but don't enforce.
                        # Log and alert actions still fire for shadow policies.
                        if is_shadow and p["action"] in ("block", "steer", "throttle"):
                            outcome = "shadow_recorded"
                        else:
                            outcome = {
                                "log": "logged",
                                "alert": "alert_queued",
                                "block": "block_recorded",
                                "steer": "steer_recorded",
                                "require_approval": "approval_requested",
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
                                "shadow": is_shadow,
                            },
                        })

                # ---- PII redaction (P1) ----
                # Runs after policy evaluation so match_expressions see
                # the raw content, but before webhook payload assembly
                # and span persistence so neither downstream consumer
                # ever sees the original PII. The function returns a
                # NEW dict; the original merged_attrs is preserved here
                # in case later code in the loop wants it.
                persisted_attrs = redaction_mod.redact_attributes(
                    merged_attrs, redaction_config,
                )

                if matched_policies:
                    for p in matched_policies:
                        is_shadow = p.get("shadow", False)
                        if p["action"] == "alert":
                            webhook_url = (p.get("action_config") or {}).get("webhook_url")
                            if webhook_url:
                                span_webhooks.append((webhook_url, {
                                    "policy_id": p["id"],
                                    "policy_name": p["name"],
                                    "span_name": span.name,
                                    "trace_id": trace_id.hex(),
                                    "span_id": span_id.hex(),
                                    "attrs": persisted_attrs,
                                }, p["id"]))
                        elif p["action"] == "require_approval" and not is_shadow:
                            # Create a pending approval record.
                            try:
                                import repositories.approvals as approvals_repo
                                timeout = (p.get("action_config") or {}).get(
                                    "timeout_seconds", 300
                                )
                                approvers_req = (p.get("action_config") or {}).get(
                                    "approvers_required", 1
                                )
                                approval = await approvals_repo.create_approval(
                                    session,
                                    project_id,
                                    policy_id=UUID(p["id"]),
                                    trace_id=trace_id,
                                    span_id=span_id,
                                    span_name=span.name,
                                    tool_name=merged_attrs.get("gen_ai.tool.name"),
                                    tool_args=merged_attrs.get("strathon.tool.args"),
                                    policy_name=p["name"],
                                    timeout_seconds=int(timeout),
                                    approvers_required=int(approvers_req),
                                )
                                merged_attrs["strathon.approval.id"] = str(approval.id)
                                merged_attrs["strathon.approval.status"] = "pending"
                                # Fire webhook if configured.
                                webhook_url = (p.get("action_config") or {}).get("webhook_url")
                                if webhook_url:
                                    span_webhooks.append((webhook_url, {
                                        "event": "approval_requested",
                                        "approval_id": str(approval.id),
                                        "approval_url": f"/v1/approvals/{approval.id}",
                                        "policy_id": p["id"],
                                        "policy_name": p["name"],
                                        "tool_name": merged_attrs.get("gen_ai.tool.name"),
                                        "span_name": span.name,
                                        "trace_id": trace_id.hex(),
                                        "span_id": span_id.hex(),
                                        "timeout_seconds": int(timeout),
                                    }, p["id"]))
                            except Exception:
                                logger.exception(
                                    "failed to create approval for policy %s", p["id"]
                                )

                # ---- Sampling decision ----
                # Made AFTER policy evaluation so the always-keep
                # rules can see strathon.policy.* annotations.
                status_code_name = STATUS_CODE_NAMES.get(span.status.code, "UNSET")
                keep, force_kept = sampling.should_keep_span(
                    trace_id,
                    merged_attrs,
                    status_code_name,
                    state.sampling_config,
                )
                if not keep:
                    state.sampling_counters.record_dropped()
                    continue
                state.sampling_counters.record_kept(force_kept=force_kept)

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

                # Compute per-span LLM cost. Returns None for non-LLM
                # spans (tool calls, generic ops) and for unknown
                # models — see pricing.compute_cost_usd docstring for
                # why None vs 0. The model name we look up is the
                # REQUEST model, not the response model: providers
                # sometimes return the actual model in response_model
                # (e.g. "gpt-4o-2024-08-06" when you asked for "gpt-4o"),
                # but pricing is charged against what you asked for.
                # If the SDK already wrote strathon.agent.cost.usd we
                # trust it — caller-supplied cost takes precedence over
                # our catalog lookup (the SDK may have access to
                # provider-specific pricing we don't, like cache-hit
                # discounts).
                cost_usd = span_attrs.get("strathon.agent.cost.usd")
                if cost_usd is None:
                    cost_usd = pricing_mod.compute_cost_usd(
                        model_name=request_model,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        catalog=pricing_catalog,
                        overrides=pricing_overrides,
                    )

                # Cost telemetry. Two metrics, both feed an operator
                # dashboard answer to "what am I actually being charged
                # for, and where am I blind?":
                #
                #   * cost_tracked_usd_total{model} accumulates the per-
                #     span dollar cost. rate() gives $/s per model;
                #     sum() across model labels gives lifetime spend.
                #   * cost_spans_with_unknown_model_total counts spans
                #     that looked like LLM calls (had a model name and
                #     non-zero tokens) but had no matching catalog
                #     entry. A non-zero rate means cost dashboards are
                #     under-counting and the operator should add a
                #     per-project price override.
                if cost_usd is not None and cost_usd > 0 and request_model:
                    state.metrics.cost_tracked_usd.labels(
                        model=request_model,
                    ).inc(float(cost_usd))
                elif (
                    cost_usd is None
                    and request_model
                    and ((input_tokens or 0) > 0 or (output_tokens or 0) > 0)
                ):
                    state.metrics.cost_spans_with_unknown_model.inc()

                # Upsert trace row before inserting the span (FK requirement).
                # Idempotent at the trace level — only the first span in a
                # trace actually inserts.
                if trace_id not in trace_ids_seen:
                    await traces_repo.upsert_trace(
                        session,
                        trace_id=trace_id,
                        project_id=project_id,
                        start_time_unix_nano=span.start_time_unix_nano,
                        agent_name=agent_name,
                    )
                    trace_ids_seen.add(trace_id)

                await traces_repo.upsert_span(
                    session,
                    trace_id=trace_id,
                    span_id=span_id,
                    parent_span_id=parent_span_id,
                    project_id=project_id,
                    name=span.name,
                    kind=SPAN_KIND_NAMES.get(span.kind, "UNSPECIFIED"),
                    start_time_unix_nano=span.start_time_unix_nano,
                    end_time_unix_nano=(
                        span.end_time_unix_nano if span.end_time_unix_nano else None
                    ),
                    status_code=STATUS_CODE_NAMES.get(span.status.code, "UNSET"),
                    status_message=span.status.message or None,
                    operation_name=operation_name,
                    provider_name=provider_name,
                    request_model=request_model,
                    response_model=response_model,
                    agent_name=agent_name,
                    agent_id=agent_id,
                    tool_name=tool_name,
                    workflow_name=workflow_name,
                    conversation_id=conversation_id,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost_usd,
                    attributes=persisted_attrs,
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
        await policies_repo.record_match(
            session,
            UUID(m["policy_id"]) if not isinstance(m["policy_id"], UUID) else m["policy_id"],
            project_id,
            m["trace_id"],
            m["span_id"],
            m["action"],
            m["outcome"],
            metadata=m.get("metadata"),
        )
        state.metrics.policy_matches.labels(action=m["action"]).inc()

    # Alert webhook delivery (commit C1: durable + retried + signed).
    #
    # For each matched alert policy, insert a webhook_deliveries row
    # inside this request's transaction. The row is the durable record
    # of "we owe this consumer one delivery." enqueue_delivery() also
    # registers a SQLAlchemy after_commit hook that dispatches the
    # Dramatiq message once the transaction is durable, so a rolled-back
    # ingest produces no phantom send. If Redis is unreachable at
    # dispatch time, the row stays `pending` and a sweeper task reclaims
    # it later.
    for webhook_url, payload, policy_id in webhooks_to_fire:
        try:
            from uuid import UUID as _UUID
            policy_uuid = (
                policy_id if isinstance(policy_id, _UUID) else _UUID(policy_id)
            )
            await webhooks_dispatch.enqueue_delivery(
                session,
                project_id=project_id,
                policy_id=policy_uuid,
                url=webhook_url,
                payload=payload,
            )
        except Exception:
            logger.exception(
                "failed to enqueue webhook delivery for policy %s; "
                "alert will not fire", policy_id,
            )
            # Don't fail ingest just because one webhook row didn't
            # insert; other matches are still recorded.

    # OTLP spec requires returning ExportTraceServiceResponse on success
    resp = ExportTraceServiceResponse()
    return Response(
        content=resp.SerializeToString(),
        media_type="application/x-protobuf",
        status_code=status.HTTP_200_OK,
    )
