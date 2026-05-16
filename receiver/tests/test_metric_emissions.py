"""Tests that the halt, budget-monitor, and cost counters fire at the
right emission sites.

These are integration tests rather than unit tests against a mocked
metrics object: they exercise the real emission paths (POST /v1/halts,
DELETE /v1/halts, evaluate_one_budget, the OTLP cost computation) and
read counter values back through the prometheus_client registry. That
way they catch wiring regressions — a forgotten labels() call, a typo
in a metric name, a path that doesn't actually run because of an early
return — that a mock-based test would miss.

Counter values are read via ``registry.get_sample_value()`` which is the
official prometheus_client API for reading a counter under a specific
label set. Each test snapshots a baseline before triggering, then
asserts the delta.
"""

from __future__ import annotations

import os
import sys
import uuid
from decimal import Decimal

import pytest

_RECEIVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _RECEIVER_DIR)


DEV_KEY = "stra_dev_local_default_project_do_not_use_in_production"
DEFAULT_PROJECT_ID = "00000000-0000-0000-0000-000000000001"
DEFAULT_DB_URL = "postgresql://strathon:strathon_dev@127.0.0.1:5432/strathon"


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _counter_value(registry, name: str, **labels) -> float:
    """Read a counter from the registry, defaulting to 0 if unset.

    prometheus_client returns None if the labels combination has never
    been observed. We treat None as 0 so tests can compute a delta
    without conditional logic.
    """
    v = registry.get_sample_value(name, labels=labels)
    return v if v is not None else 0.0


@pytest.fixture(scope="module")
def client():
    db_url = os.getenv("DATABASE_URL", DEFAULT_DB_URL)
    os.environ["DATABASE_URL"] = db_url
    try:
        import psycopg
        conn = psycopg.connect(db_url, autocommit=True)
        conn.close()
    except Exception:
        pytest.skip("Postgres not reachable")

    from config import get_settings
    from database import get_engine, get_session_maker
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_maker.cache_clear()

    from fastapi.testclient import TestClient
    import main
    with TestClient(main.app) as c:
        yield c


# ---- Halt-API emission sites ----------------------------------------------


def test_post_halt_increments_halts_created_counter(client):
    """POST /v1/halts emits halts_created{scope, actor='user'}."""
    import main
    registry = main.app.state.metrics.registry

    before = _counter_value(
        registry, "strathon_receiver_halts_created_total",
        scope="agent", actor="user",
    )

    r = client.post(
        "/v1/halts",
        headers=_auth(DEV_KEY),
        json={
            "scope": "agent",
            "scope_value": f"emit-test-{uuid.uuid4().hex[:8]}",
            "reason": "metric emission test",
        },
    )
    assert r.status_code == 201, r.text
    halt_id = r.json()["halt"]["id"]

    after = _counter_value(
        registry, "strathon_receiver_halts_created_total",
        scope="agent", actor="user",
    )
    assert after - before == 1.0, f"expected +1 increment; before={before} after={after}"

    # Cleanup
    import psycopg
    conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
    try:
        conn.execute("DELETE FROM halt_state WHERE id = %s", (halt_id,))
    finally:
        conn.close()


def test_post_project_halt_uses_project_scope_label(client):
    """Project-scope halt should produce halts_created{scope='project'}."""
    import main
    registry = main.app.state.metrics.registry

    before = _counter_value(
        registry, "strathon_receiver_halts_created_total",
        scope="project", actor="user",
    )

    r = client.post(
        "/v1/halts",
        headers=_auth(DEV_KEY),
        json={"scope": "project", "reason": "project halt metric test"},
    )
    assert r.status_code == 201, r.text
    halt_id = r.json()["halt"]["id"]

    after = _counter_value(
        registry, "strathon_receiver_halts_created_total",
        scope="project", actor="user",
    )
    assert after - before == 1.0

    import psycopg
    conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
    try:
        conn.execute("DELETE FROM halt_state WHERE id = %s", (halt_id,))
    finally:
        conn.close()


def test_delete_halt_increments_halts_cleared_counter(client):
    """DELETE /v1/halts/{id} emits halts_cleared{actor='user', reason='operator_request'}."""
    import main
    registry = main.app.state.metrics.registry

    # Create one to delete
    r = client.post(
        "/v1/halts",
        headers=_auth(DEV_KEY),
        json={
            "scope": "agent",
            "scope_value": f"clear-test-{uuid.uuid4().hex[:8]}",
            "reason": "cleanup target",
        },
    )
    assert r.status_code == 201
    halt_id = r.json()["halt"]["id"]

    before = _counter_value(
        registry, "strathon_receiver_halts_cleared_total",
        actor="user", reason="operator_request",
    )

    r = client.delete(f"/v1/halts/{halt_id}", headers=_auth(DEV_KEY))
    assert r.status_code == 200, r.text

    after = _counter_value(
        registry, "strathon_receiver_halts_cleared_total",
        actor="user", reason="operator_request",
    )
    assert after - before == 1.0


def test_post_halt_validation_failure_does_not_increment_counter(client):
    """400 from bad request body must not touch the counter."""
    import main
    registry = main.app.state.metrics.registry
    before = _counter_value(
        registry, "strathon_receiver_halts_created_total",
        scope="agent", actor="user",
    )

    r = client.post(
        "/v1/halts",
        headers=_auth(DEV_KEY),
        # scope=agent requires scope_value — this should 400
        json={"scope": "agent", "reason": "missing scope_value"},
    )
    assert r.status_code == 400

    after = _counter_value(
        registry, "strathon_receiver_halts_created_total",
        scope="agent", actor="user",
    )
    assert after == before


def test_delete_halt_not_found_does_not_increment_counter(client):
    """404 from unknown halt id must not touch the counter."""
    import main
    registry = main.app.state.metrics.registry
    before = _counter_value(
        registry, "strathon_receiver_halts_cleared_total",
        actor="user", reason="operator_request",
    )

    r = client.delete("/v1/halts/999999999", headers=_auth(DEV_KEY))
    assert r.status_code == 404

    after = _counter_value(
        registry, "strathon_receiver_halts_cleared_total",
        actor="user", reason="operator_request",
    )
    assert after == before


# ---- Budget-monitor emission sites ----------------------------------------


@pytest.mark.asyncio
async def test_evaluate_one_budget_emits_violation_and_halts_created_on_breach(client):
    """A budget that crosses threshold for the first time should increment
    budget_violations{kind} AND halts_created{scope='project',
    actor='budget_monitor'} both by 1.
    """
    import main
    import budget_monitor
    import repositories.budgets as budgets_repo
    from datetime import datetime, timedelta, timezone
    from database import get_session_maker
    from sqlalchemy import insert
    from models.intervention import Budget
    from models.traces import Span, Trace

    metrics = main.app.state.metrics
    registry = metrics.registry
    session_maker = get_session_maker()
    project_id = uuid.UUID(DEFAULT_PROJECT_ID)

    # Insert a cost budget with a tiny threshold and one over-threshold
    # span so the evaluator decides "over."
    trace_id = uuid.uuid4().bytes
    span_id = uuid.uuid4().bytes[:8]
    agent_id = f"metric-test-agent-{uuid.uuid4().hex[:8]}"
    budget_id_holder: list[uuid.UUID] = []

    # end_time slightly in the past so it falls inside the budget's
    # active window (which extends from budget_reset_at - duration up
    # to budget_reset_at).
    now_utc = datetime.now(timezone.utc)
    span_end_ns = int(now_utc.timestamp() * 1_000_000_000)

    async with session_maker() as session:
        # Trace first (FK).
        await session.execute(
            insert(Trace).values(
                id=trace_id,
                project_id=project_id,
                start_time_unix_nano=span_end_ns - 1_000_000_000,  # 1s before
                agent_name=agent_id,
            )
        )
        # One span with cost above the budget threshold.
        await session.execute(
            insert(Span).values(
                trace_id=trace_id,
                span_id=span_id,
                project_id=project_id,
                name="llm.test",
                kind="INTERNAL",
                start_time_unix_nano=span_end_ns - 1_000_000_000,
                end_time_unix_nano=span_end_ns,
                agent_id=agent_id,
                cost_usd=Decimal("1.00"),
            )
        )
        # Budget capped at $0.10 — single $1 span trivially exceeds it.
        # budget_reset_at is set just past "now" so the active window
        # (reset_at - 30d to reset_at) includes the span we just wrote.
        row = await session.execute(
            insert(Budget).values(
                project_id=project_id,
                name=f"metric-test-budget-{uuid.uuid4().hex[:8]}",
                scope="agent",
                scope_value=agent_id,
                max_spend_usd=Decimal("0.10"),
                budget_duration="30d",
                budget_reset_at=now_utc + timedelta(days=1),
                is_active=True,
            ).returning(Budget.id)
        )
        budget_id_holder.append(row.scalar_one())
        await session.commit()

    budget_id = budget_id_holder[0]

    before_violations = _counter_value(
        registry, "strathon_receiver_budget_violations_total", kind="cost",
    )
    before_halts = _counter_value(
        registry, "strathon_receiver_halts_created_total",
        scope="project", actor="budget_monitor",
    )

    # Fetch the inserted budget row and evaluate.
    async with session_maker() as session:
        budgets = await budgets_repo.list_active_budgets_for_monitor(session, limit=100)
        target = next(b for b in budgets if b.id == budget_id)

    async with session_maker() as session:
        await budget_monitor.evaluate_one_budget(
            session, target, now=datetime.now(timezone.utc), metrics=metrics,
        )
        await session.commit()

    after_violations = _counter_value(
        registry, "strathon_receiver_budget_violations_total", kind="cost",
    )
    after_halts = _counter_value(
        registry, "strathon_receiver_halts_created_total",
        scope="project", actor="budget_monitor",
    )

    try:
        assert after_violations - before_violations == 1.0
        assert after_halts - before_halts == 1.0
    finally:
        # Cleanup.
        import psycopg
        conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
        try:
            conn.execute("DELETE FROM halt_state WHERE budget_id = %s", (str(budget_id),))
            conn.execute("DELETE FROM budgets WHERE id = %s", (str(budget_id),))
            conn.execute("DELETE FROM spans WHERE trace_id = %s", (trace_id,))
            conn.execute("DELETE FROM traces WHERE id = %s", (trace_id,))
        finally:
            conn.close()


@pytest.mark.asyncio
async def test_run_one_tick_emits_skipped_no_lock_when_lock_held(client):
    """If another session already holds the advisory lock, run_one_tick
    returns 0 and emits budget_monitor_ticks{outcome='skipped_no_lock'}."""
    import main
    import budget_monitor
    from database import get_session_maker
    from sqlalchemy import text

    metrics = main.app.state.metrics
    registry = metrics.registry
    session_maker = get_session_maker()

    before = _counter_value(
        registry, "strathon_receiver_budget_monitor_ticks_total",
        outcome="skipped_no_lock",
    )

    # Hold the lock from a separate connection.
    async with session_maker() as holder:
        held = await holder.scalar(
            text("SELECT pg_try_advisory_lock(:k)").bindparams(
                k=budget_monitor.MONITOR_LOCK_ID,
            )
        )
        assert held is True
        try:
            count = await budget_monitor.run_one_tick(
                session_maker, budget_monitor.MonitorConfig(), metrics=metrics,
            )
            assert count == 0
        finally:
            await holder.execute(
                text("SELECT pg_advisory_unlock(:k)").bindparams(
                    k=budget_monitor.MONITOR_LOCK_ID,
                )
            )
            await holder.commit()

    after = _counter_value(
        registry, "strathon_receiver_budget_monitor_ticks_total",
        outcome="skipped_no_lock",
    )
    assert after - before == 1.0


@pytest.mark.asyncio
async def test_run_one_tick_emits_ran_outcome_on_normal_tick(client):
    """A normal tick (lock acquired) increments outcome='ran'."""
    import main
    import budget_monitor
    from database import get_session_maker

    metrics = main.app.state.metrics
    registry = metrics.registry
    session_maker = get_session_maker()

    before = _counter_value(
        registry, "strathon_receiver_budget_monitor_ticks_total", outcome="ran",
    )

    await budget_monitor.run_one_tick(
        session_maker, budget_monitor.MonitorConfig(), metrics=metrics,
    )

    after = _counter_value(
        registry, "strathon_receiver_budget_monitor_ticks_total", outcome="ran",
    )
    assert after - before == 1.0


# ---- Cost emission sites --------------------------------------------------


def _otlp_protobuf_with_llm_span(model: str, input_tokens: int, output_tokens: int) -> bytes:
    """Build an OTLP/HTTP protobuf payload carrying one LLM span.

    Mirrors the helper in test_ingest_cost.py: the receiver's /v1/traces
    endpoint expects ExportTraceServiceRequest as protobuf bytes, so we
    construct one with the gen_ai.* attributes the cost computation
    reads.
    """
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )
    from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
    from opentelemetry.proto.trace.v1.trace_pb2 import (
        ResourceSpans, ScopeSpans, Span,
    )

    def _kv(k: str, v):
        if isinstance(v, bool):
            return KeyValue(key=k, value=AnyValue(bool_value=v))
        if isinstance(v, int):
            return KeyValue(key=k, value=AnyValue(int_value=v))
        if isinstance(v, str):
            return KeyValue(key=k, value=AnyValue(string_value=v))
        raise TypeError(f"unsupported attr type for key {k}: {type(v)}")

    span = Span(
        trace_id=uuid.uuid4().bytes,
        span_id=uuid.uuid4().bytes[:8],
        name="llm.generate",
        kind=Span.SPAN_KIND_CLIENT,
        start_time_unix_nano=1_700_000_000_000_000_000,
        end_time_unix_nano=1_700_000_001_000_000_000,
        attributes=[
            _kv("gen_ai.request.model", model),
            _kv("gen_ai.usage.input_tokens", input_tokens),
            _kv("gen_ai.usage.output_tokens", output_tokens),
            _kv("strathon.agent.id", f"metric-test-{uuid.uuid4().hex[:8]}"),
        ],
    )
    req = ExportTraceServiceRequest(
        resource_spans=[ResourceSpans(scope_spans=[ScopeSpans(spans=[span])])],
    )
    return req.SerializeToString()


def test_cost_tracked_usd_increments_for_known_model(client):
    """An LLM span with a model in the catalog and non-zero tokens
    should increment cost_tracked_usd_total{model=<m>}."""
    import main
    registry = main.app.state.metrics.registry

    # gpt-4o-mini is a known cheap model in the vendored catalog.
    model = "gpt-4o-mini"
    before = _counter_value(
        registry, "strathon_receiver_cost_tracked_usd_total", model=model,
    )

    body = _otlp_protobuf_with_llm_span(model=model, input_tokens=1000, output_tokens=500)
    r = client.post(
        "/v1/traces",
        headers={**_auth(DEV_KEY), "Content-Type": "application/x-protobuf"},
        content=body,
    )
    assert r.status_code in (200, 202), r.text

    after = _counter_value(
        registry, "strathon_receiver_cost_tracked_usd_total", model=model,
    )
    # Counter is a float increment by USD. We don't assert the exact
    # value (pricing might change) — just that it went up.
    assert after > before, f"cost_tracked_usd did not increment: {before} -> {after}"


def test_cost_unknown_model_counter_increments_for_unrecognized_model(client):
    """A model name absent from the catalog should increment
    cost_spans_with_unknown_model_total, and NOT increment cost_tracked_usd
    for that label series."""
    import main
    registry = main.app.state.metrics.registry

    bogus_model = f"definitely-not-in-catalog-{uuid.uuid4().hex[:8]}"
    before_unknown = _counter_value(
        registry, "strathon_receiver_cost_spans_with_unknown_model_total",
    )
    before_tracked = _counter_value(
        registry, "strathon_receiver_cost_tracked_usd_total", model=bogus_model,
    )

    body = _otlp_protobuf_with_llm_span(
        model=bogus_model, input_tokens=100, output_tokens=50,
    )
    r = client.post(
        "/v1/traces",
        headers={**_auth(DEV_KEY), "Content-Type": "application/x-protobuf"},
        content=body,
    )
    assert r.status_code in (200, 202), r.text

    after_unknown = _counter_value(
        registry, "strathon_receiver_cost_spans_with_unknown_model_total",
    )
    after_tracked = _counter_value(
        registry, "strathon_receiver_cost_tracked_usd_total", model=bogus_model,
    )
    assert after_unknown - before_unknown == 1.0, (
        f"unknown-model counter didn't increment: {before_unknown} -> {after_unknown}"
    )
    # Bogus model has no catalog entry, so no cost is tracked for it.
    assert after_tracked == before_tracked
