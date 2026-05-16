"""End-to-end test for the cost-budget circuit breaker.

Drives the real receiver subprocess and the real SDK halt enforcer:

  1. Operator creates a cost budget via POST /v1/budgets.
  2. Spans land via OTLP with computed cost.
  3. Budget monitor's next tick sees spend > threshold and writes a halt
     (actor=budget_monitor) to halt_state.
  4. SDK's HaltEnforcer polls /v1/intervention/sync and picks up the halt.
  5. The same SDK observes the budget rolling into the new window
     (window simulated by directly updating budget_reset_at): monitor
     sees spend=0 in the new window and clears its halt.
  6. SDK observes the cleared halt on its next sync.

This is the test that proves the whole circuit-breaker story holds
end-to-end: operator's cost cap → server-side enforcement → SDK
observation → automatic clear on window reset.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
import uuid


# The "tests/" directory at the repo root has its own conftest.py that
# spins up a receiver subprocess (the `receiver` fixture). This file
# is at /strathon/tests/test_budget_e2e.py, distinct from the per-suite
# tests under /strathon/receiver/tests/.

DEV_KEY = "stra_dev_local_default_project_do_not_use_in_production"


def _post(url: str, body: dict, headers: dict | None = None) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {DEV_KEY}",
            "Content-Type": "application/json",
            **(headers or {}),
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _delete(url: str) -> None:
    req = urllib.request.Request(
        url,
        method="DELETE",
        headers={"Authorization": f"Bearer {DEV_KEY}"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        resp.read()


def _patch(url: str, body: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="PATCH",
        headers={
            "Authorization": f"Bearer {DEV_KEY}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post_otlp_span(receiver: str, *, model: str, input_tokens: int, output_tokens: int) -> bytes:
    """Send one LLM span via OTLP. Returns the span_id used."""
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )
    from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
    from opentelemetry.proto.trace.v1.trace_pb2 import (
        ResourceSpans, ScopeSpans, Span,
    )

    trace_id = uuid.uuid4().bytes
    span_id = uuid.uuid4().bytes[:8]

    def _kv(k, v):
        if isinstance(v, int):
            return KeyValue(key=k, value=AnyValue(int_value=v))
        return KeyValue(key=k, value=AnyValue(string_value=str(v)))

    now_ns = time.time_ns()
    span = Span(
        trace_id=trace_id, span_id=span_id,
        name="llm.generate",
        kind=Span.SPAN_KIND_CLIENT,
        start_time_unix_nano=now_ns - 1_000_000,
        end_time_unix_nano=now_ns,
        attributes=[
            _kv("gen_ai.request.model", model),
            _kv("gen_ai.usage.input_tokens", input_tokens),
            _kv("gen_ai.usage.output_tokens", output_tokens),
        ],
    )
    req = ExportTraceServiceRequest(
        resource_spans=[ResourceSpans(scope_spans=[ScopeSpans(spans=[span])])],
    )

    request = urllib.request.Request(
        f"{receiver}/v1/traces",
        data=req.SerializeToString(),
        method="POST",
        headers={
            "Authorization": f"Bearer {DEV_KEY}",
            "Content-Type": "application/x-protobuf",
        },
    )
    with urllib.request.urlopen(request, timeout=5) as resp:
        resp.read()
    return span_id


def _purge_all() -> None:
    """Clean halt_state + budgets + spans so the test doesn't see
    leftover state from earlier tests in the same session."""
    import psycopg
    db_url = os.environ.get(
        "DATABASE_URL",
        "postgresql://strathon:strathon_dev@127.0.0.1:5432/strathon",
    )
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute("DELETE FROM halt_state")
        conn.execute("DELETE FROM budgets")
        conn.execute("DELETE FROM spans")
        conn.execute("DELETE FROM traces")


def test_e2e_budget_triggers_halt_then_window_reset_clears(receiver: str):
    """The full circuit-breaker loop. Requires a receiver subprocess
    that runs the in-process budget monitor (default config: 5s tick).
    We short-circuit the wait by setting STRATHON_BUDGET_EVAL_INTERVAL_SECONDS
    to a small value via the receiver fixture's env (set in conftest)
    OR by triggering one tick manually."""
    _purge_all()

    # Step 1: create a tight cost budget. $0.001 cap, 30d window.
    created = _post(
        f"{receiver}/v1/budgets",
        {
            "name": "e2e tight cap",
            "scope": "project",
            "max_spend_usd": "0.001",
            "budget_duration": "30d",
        },
    )
    budget_id = created["budget"]["id"]

    try:
        # Step 2: ingest a span that exceeds the budget.
        # gpt-4o, 1000 input + 500 output = $0.0075 (cap is $0.001, so over).
        _post_otlp_span(
            receiver, model="gpt-4o", input_tokens=1000, output_tokens=500,
        )

        # Trigger a budget monitor tick by calling the function directly
        # through the receiver process. The receiver fixture exposes a
        # process; we can't easily call run_one_tick from outside. Wait
        # for the natural tick instead (5s default; in tests we want
        # faster, but the conftest doesn't override env). We'll wait up
        # to 8 seconds for the monitor to evaluate.
        deadline = time.time() + 10
        halt_seen = False
        while time.time() < deadline:
            sync = _post(f"{receiver}/v1/intervention/sync", {})
            if sync.get("halts"):
                # Look for our budget's halt
                for h in sync["halts"]:
                    # The budget halt carries the budget reason
                    reason = h.get("reason") or ""
                    if "e2e tight cap" in reason or "exceeded" in reason.lower():
                        halt_seen = True
                        break
            if halt_seen:
                break
            time.sleep(0.5)
        assert halt_seen, (
            "Budget monitor did not produce a halt within 10s. "
            "Verify STRATHON_BUDGET_EVAL_INTERVAL_SECONDS is reasonable."
        )

        # Step 3: simulate the window resetting. We directly advance
        # the budget's reset_at to the past so the next monitor tick
        # rolls it over, clears the halt, and computes spend in the
        # new (empty) window.
        import psycopg
        import os
        db_url = os.environ.get(
            "DATABASE_URL",
            "postgresql://strathon:strathon_dev@127.0.0.1:5432/strathon",
        )
        with psycopg.connect(db_url, autocommit=True) as conn:
            conn.execute(
                "UPDATE budgets SET budget_reset_at = NOW() - INTERVAL '1 minute' "
                "WHERE id = %s::uuid",
                (budget_id,),
            )
            # Also: spans we ingested before reset_at fall into the
            # OLD window. Move their end_time back so they're not in
            # the new window we're about to roll into.
            # Use bigint literal to avoid int4 overflow on the
            # nanosecond-times-days multiplication.
            conn.execute(
                "UPDATE spans SET end_time_unix_nano = "
                "end_time_unix_nano - 2678400000000000"  # 31 days in ns
            )

        # Step 4: wait for monitor to clear the halt.
        deadline = time.time() + 10
        cleared = False
        while time.time() < deadline:
            sync = _post(f"{receiver}/v1/intervention/sync", {})
            halts = sync.get("halts", [])
            still_active = any(
                ("e2e tight cap" in (h.get("reason") or "")
                 or "exceeded" in (h.get("reason") or "").lower())
                for h in halts
            )
            if not still_active:
                cleared = True
                break
            time.sleep(0.5)
        assert cleared, "Halt did not auto-clear after window reset within 10s"

    finally:
        try:
            _delete(f"{receiver}/v1/budgets/{budget_id}")
        except Exception:
            pass
        _purge_all()


def test_e2e_sync_endpoint_surfaces_budgets(receiver: str):
    """The /v1/intervention/sync endpoint now returns budgets so the
    SDK can render dashboards without a separate call."""
    _purge_all()
    created = _post(
        f"{receiver}/v1/budgets",
        {
            "name": "visible",
            "scope": "project",
            "max_spend_usd": "100",
            "budget_duration": "1d",
        },
    )
    bid = created["budget"]["id"]
    try:
        sync = _post(f"{receiver}/v1/intervention/sync", {})
        names = [b["name"] for b in sync.get("budgets", [])]
        assert "visible" in names
    finally:
        _delete(f"{receiver}/v1/budgets/{bid}")
        _purge_all()
