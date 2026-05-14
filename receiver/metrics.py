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


__all__ = [
    "RetentionCounters",
    "StrathonMetrics",
    "render_metrics",
    "sync_sampling_counters",
]
