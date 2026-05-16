"""Prometheus metrics for the Strathon receiver.

We expose metrics in the standard Prometheus exposition format at
``/metrics``. The receiver uses the official ``prometheus_client`` library
directly rather than the ``prometheus-fastapi-instrumentator`` wrapper:
the wrapper auto-instruments HTTP metrics which is convenient but adds
middleware overhead we don't need for v1, and we want full control over
the custom domain metrics (sampling decisions, retention sweeps, policy
matches).

### Metric design

We follow Prometheus naming conventions:
    - ``_total`` suffix for monotonic counters
    - lower_snake_case
    - units in the metric name when not obvious (``_seconds``, ``_bytes``)

All metrics live in the ``strathon_receiver_`` namespace so they don't
collide with co-tenant metrics from other services scraped by the same
Prometheus.

### Pull vs push for sampling counters

Our SamplingCounters are kept on app.state as plain in-memory atomic ints
(updated synchronously inside the ingest loop). The /metrics handler reads
their snapshot and writes the values into the Prometheus registry just
before generating the exposition. This avoids double-counting if a Prom
client and our atomic counters drift.
"""

from __future__ import annotations

import logging
import threading

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    generate_latest,
)

logger = logging.getLogger("strathon.receiver.metrics")


# ============================================================
# Registry + metrics
# ============================================================
# Using a custom registry instead of the global default keeps Strathon
# metrics isolated from anything else in the process and lets tests reset
# state cleanly.


class StrathonMetrics:
    """Container for all receiver Prometheus metrics.

    Held on app.state as a single object so handlers can update gauges /
    increment counters via dotted access without globals.
    """

    def __init__(self) -> None:
        self.registry = CollectorRegistry()

        # ---- Sampling (mirrored from SamplingCounters on each scrape) ----
        self.sampling_spans_kept = Counter(
            "strathon_receiver_sampling_spans_kept_total",
            "Total spans persisted after sampling decision",
            registry=self.registry,
        )
        self.sampling_spans_dropped = Counter(
            "strathon_receiver_sampling_spans_dropped_total",
            "Total spans dropped by sampling at ingest",
            registry=self.registry,
        )
        self.sampling_spans_force_kept = Counter(
            "strathon_receiver_sampling_spans_force_kept_total",
            "Spans that would have been dropped but were kept by an always-keep rule "
            "(policy match, error, expensive LLM call)",
            registry=self.registry,
        )

        # ---- Sampling configuration ----
        self.sampling_rate = Gauge(
            "strathon_receiver_sampling_rate",
            "Configured probabilistic sampling rate in [0.0, 1.0]",
            registry=self.registry,
        )

        # ---- Retention ----
        self.retention_traces_deleted = Counter(
            "strathon_receiver_retention_traces_deleted_total",
            "Total traces removed by retention sweeps (spans cascade-delete)",
            registry=self.registry,
        )
        self.retention_sweeps = Counter(
            "strathon_receiver_retention_sweeps_total",
            "Total retention sweeps completed (excluding errors)",
            registry=self.registry,
        )
        self.retention_sweep_errors = Counter(
            "strathon_receiver_retention_sweep_errors_total",
            "Retention sweeps that raised an exception",
            registry=self.registry,
        )

        # ---- Policy enforcement (counted at ingest by action) ----
        self.policy_matches = Counter(
            "strathon_receiver_policy_matches_total",
            "Policy matches recorded at ingest, by action",
            ["action"],  # log | alert | block | steer
            registry=self.registry,
        )

        # ---- Auth ----
        self.auth_failures = Counter(
            "strathon_receiver_auth_failures_total",
            "Requests rejected with 401 due to missing or invalid API key",
            registry=self.registry,
        )
        self.auth_successes = Counter(
            "strathon_receiver_auth_successes_total",
            "Successful API-key authentications",
            registry=self.registry,
        )

        # ---- Webhook delivery (C3) ----
        # Counted at the actor on every send classification. The
        # outcome label is the row's final status for that attempt:
        # succeeded | abandoned | failed_retrying | dlq.
        self.webhook_sends = Counter(
            "strathon_receiver_webhook_sends_total",
            "Webhook delivery attempts that ran (one increment per actor "
            "invocation that issued an HTTP request).",
            ["outcome"],
            registry=self.registry,
        )
        # The dispatch counter mirrors enqueue_delivery() — counts the
        # number of webhook_deliveries rows we've inserted, regardless
        # of whether they later succeeded.
        self.webhook_dispatched = Counter(
            "strathon_receiver_webhook_dispatched_total",
            "Webhook delivery rows enqueued via enqueue_delivery()",
            registry=self.registry,
        )
        # DLQ landings — useful as an alerting target.
        self.webhook_dlq = Counter(
            "strathon_receiver_webhook_dlq_total",
            "Webhook deliveries that exhausted retries and landed in DLQ",
            registry=self.registry,
        )
        # Sweeper tick counters.
        self.webhook_sweeper_runs = Counter(
            "strathon_receiver_webhook_sweeper_runs_total",
            "Webhook sweeper ticks completed (excluding errors)",
            registry=self.registry,
        )
        self.webhook_sweeper_reclaimed = Counter(
            "strathon_receiver_webhook_sweeper_reclaimed_total",
            "Orphan pending delivery rows re-dispatched by the sweeper",
            registry=self.registry,
        )
        self.webhook_sweeper_errors = Counter(
            "strathon_receiver_webhook_sweeper_errors_total",
            "Sweeper ticks that raised an exception",
            registry=self.registry,
        )

        # ---- Halts (operator and budget-monitor created) ----
        # scope: project | agent. actor: user | budget_monitor.
        # A new time series per (scope, actor) pair gives the four
        # combinations operators actually care about distinguishing.
        self.halts_created = Counter(
            "strathon_receiver_halts_created_total",
            "Halts created, by scope and actor",
            ["scope", "actor"],
            registry=self.registry,
        )
        # actor: user | budget_monitor.
        # reason: operator_request | under_threshold.
        # Lets operators tell apart "operator cleared a halt" from
        # "budget came back under threshold and the monitor cleared its
        # own halt."
        self.halts_cleared = Counter(
            "strathon_receiver_halts_cleared_total",
            "Halts cleared, by actor and reason",
            ["actor", "reason"],
            registry=self.registry,
        )

        # ---- Budget monitor ----
        # outcome: ran | skipped_no_lock. The skipped variant fires on
        # replicas that lost the advisory lock to another replica this
        # tick. A healthy multi-replica deploy shows one replica
        # incrementing "ran" while the others increment "skipped_no_lock".
        self.budget_monitor_ticks = Counter(
            "strathon_receiver_budget_monitor_ticks_total",
            "Budget monitor ticks, by outcome",
            ["outcome"],
            registry=self.registry,
        )
        self.budget_monitor_tick_errors = Counter(
            "strathon_receiver_budget_monitor_tick_errors_total",
            "Budget monitor ticks that raised an exception",
            registry=self.registry,
        )
        self.budget_evaluations = Counter(
            "strathon_receiver_budget_evaluations_total",
            "Individual budgets evaluated successfully across all ticks",
            registry=self.registry,
        )
        self.budget_evaluation_errors = Counter(
            "strathon_receiver_budget_evaluation_errors_total",
            "Per-budget evaluations that raised an exception (the tick "
            "continues; one bad budget doesn't poison the others)",
            registry=self.registry,
        )
        # kind: cost | iteration. Increments only on a NEW violation
        # (over threshold and no existing halt from this budget yet),
        # not on every tick a halted budget is re-evaluated. Pairs with
        # halts_created{actor="budget_monitor"} which fires on the same
        # transition.
        self.budget_violations = Counter(
            "strathon_receiver_budget_violations_total",
            "Budgets that crossed threshold and produced a new halt, by kind",
            ["kind"],
            registry=self.registry,
        )

        # ---- Cost tracking (LLM span ingest) ----
        # The Counter is incremented by the per-span USD cost as a
        # float. rate() over this counter gives $/second; sum() across
        # all label series gives lifetime spend. The model label is
        # bounded by the catalog (~20 models) plus per-project
        # overrides, so cardinality stays in the dozens for typical
        # deployments.
        self.cost_tracked_usd = Counter(
            "strathon_receiver_cost_tracked_usd_total",
            "Cumulative USD cost tracked for ingested LLM spans, by model",
            ["model"],
            registry=self.registry,
        )
        # Counts LLM spans (had a model name and at least one token
        # count) where the catalog lookup returned no price. A non-zero
        # rate here means operators are losing cost visibility for some
        # of their traffic and should add a per-project override.
        self.cost_spans_with_unknown_model = Counter(
            "strathon_receiver_cost_spans_with_unknown_model_total",
            "LLM spans whose model isn't in the pricing catalog or overrides",
            registry=self.registry,
        )

        # Internal tracking — Prometheus Counters only support .inc(),
        # so we mirror the SamplingCounters delta-by-delta each scrape.
        self._lock = threading.Lock()
        self._last_sampling_snapshot = {
            "spans_kept_total": 0,
            "spans_dropped_total": 0,
            "spans_force_kept_total": 0,
        }


class RetentionCounters:
    """Lightweight stats container passed to retention_loop().

    Increments the underlying Prometheus counters on each sweep.
    """

    def __init__(self, metrics: StrathonMetrics) -> None:
        self._metrics = metrics

    def record_sweep(self, projects_scanned: int, traces_deleted: int) -> None:
        self._metrics.retention_sweeps.inc()
        if traces_deleted > 0:
            self._metrics.retention_traces_deleted.inc(traces_deleted)

    def record_sweep_error(self) -> None:
        self._metrics.retention_sweep_errors.inc()


def sync_sampling_counters(metrics: StrathonMetrics, snapshot: dict[str, int]) -> None:
    """Apply the delta from a SamplingCounters snapshot to the Prom counters.

    Called from the /metrics handler just before generating the exposition.
    Prometheus Counters are monotonic — we add the delta since the last
    sync, not the absolute value.
    """
    with metrics._lock:
        last = metrics._last_sampling_snapshot
        kept_delta = snapshot["spans_kept_total"] - last["spans_kept_total"]
        dropped_delta = snapshot["spans_dropped_total"] - last["spans_dropped_total"]
        force_delta = snapshot["spans_force_kept_total"] - last["spans_force_kept_total"]

        if kept_delta > 0:
            metrics.sampling_spans_kept.inc(kept_delta)
        if dropped_delta > 0:
            metrics.sampling_spans_dropped.inc(dropped_delta)
        if force_delta > 0:
            metrics.sampling_spans_force_kept.inc(force_delta)

        metrics._last_sampling_snapshot = dict(snapshot)


def render_metrics(metrics: StrathonMetrics) -> tuple[bytes, str]:
    """Return (body, content_type) for the /metrics response."""
    body = generate_latest(metrics.registry)
    return body, CONTENT_TYPE_LATEST


# ---- Global accessor for non-request-scoped code ------------------------
#
# The Dramatiq actor runs in a worker that doesn't see the FastAPI app
# instance. The sweeper loop runs as a background task and could pass
# the metrics object explicitly, but the actor cannot. We expose a
# module-level singleton accessor: main.py's lifespan calls
# ``set_global_metrics`` after creating the StrathonMetrics; the actor
# calls ``get_global_metrics`` and emits to it.
#
# Returns None if no instance has been set (importable for tests, used
# guarded — the actor's branch is "if metrics: metrics.x.inc()").

_global_metrics: StrathonMetrics | None = None


def set_global_metrics(metrics: StrathonMetrics) -> None:
    global _global_metrics
    _global_metrics = metrics


def get_global_metrics() -> StrathonMetrics | None:
    return _global_metrics


def reset_global_metrics_for_testing() -> None:
    """Tests use this to clear the singleton between cases."""
    global _global_metrics
    _global_metrics = None


__all__ = [
    "RetentionCounters",
    "StrathonMetrics",
    "get_global_metrics",
    "render_metrics",
    "reset_global_metrics_for_testing",
    "set_global_metrics",
    "sync_sampling_counters",
]
