"""Unit tests for the metrics module."""

import sys

import pytest

sys.path.insert(0, "/home/claude/strathon/receiver")

from metrics import (
    RetentionCounters,
    StrathonMetrics,
    render_metrics,
    sync_sampling_counters,
)


def test_strathon_metrics_has_all_expected_collectors():
    m = StrathonMetrics()
    body, content_type = render_metrics(m)
    text = body.decode("utf-8")

    # Each registered counter / gauge should produce at least its HELP line
    expected_metrics = [
        "strathon_receiver_sampling_spans_kept_total",
        "strathon_receiver_sampling_spans_dropped_total",
        "strathon_receiver_sampling_spans_force_kept_total",
        "strathon_receiver_sampling_rate",
        "strathon_receiver_retention_traces_deleted_total",
        "strathon_receiver_retention_sweeps_total",
        "strathon_receiver_retention_sweep_errors_total",
        "strathon_receiver_policy_matches_total",
        "strathon_receiver_auth_failures_total",
        "strathon_receiver_auth_successes_total",
    ]
    for name in expected_metrics:
        assert name in text, f"metric {name} missing from /metrics body"


def test_render_metrics_returns_prom_content_type():
    m = StrathonMetrics()
    _, content_type = render_metrics(m)
    # text/plain; version=0.0.4; charset=utf-8 — the Prometheus exposition format
    assert "text/plain" in content_type
    assert "version=" in content_type


def test_sync_sampling_counters_first_call_records_full_value():
    """First sync should treat all counts as deltas (since baseline is 0)."""
    m = StrathonMetrics()
    sync_sampling_counters(m, {
        "spans_kept_total": 10,
        "spans_dropped_total": 5,
        "spans_force_kept_total": 2,
    })
    # Read back via the exposition
    body, _ = render_metrics(m)
    text = body.decode("utf-8")
    assert "strathon_receiver_sampling_spans_kept_total 10.0" in text
    assert "strathon_receiver_sampling_spans_dropped_total 5.0" in text
    assert "strathon_receiver_sampling_spans_force_kept_total 2.0" in text


def test_sync_sampling_counters_applies_delta_not_absolute():
    """Subsequent syncs should add only the delta, not re-add the snapshot."""
    m = StrathonMetrics()
    sync_sampling_counters(m, {
        "spans_kept_total": 10,
        "spans_dropped_total": 0,
        "spans_force_kept_total": 0,
    })
    sync_sampling_counters(m, {
        "spans_kept_total": 25,  # +15 since last sync
        "spans_dropped_total": 0,
        "spans_force_kept_total": 0,
    })

    body, _ = render_metrics(m)
    text = body.decode("utf-8")
    assert "strathon_receiver_sampling_spans_kept_total 25.0" in text


def test_sync_sampling_counters_does_not_decrement_on_reset():
    """If the snapshot somehow regresses (shouldn't happen but be defensive),
    we don't try to decrement the Prom counter (which would crash)."""
    m = StrathonMetrics()
    sync_sampling_counters(m, {
        "spans_kept_total": 100,
        "spans_dropped_total": 50,
        "spans_force_kept_total": 10,
    })
    # Lower values -> delta would be negative
    sync_sampling_counters(m, {
        "spans_kept_total": 50,
        "spans_dropped_total": 20,
        "spans_force_kept_total": 5,
    })
    # If we got here without raising, defensiveness works. The counter
    # stays at 100/50/10 since negative deltas are ignored.
    body, _ = render_metrics(m)
    text = body.decode("utf-8")
    assert "strathon_receiver_sampling_spans_kept_total 100.0" in text


def test_retention_counters_increment():
    m = StrathonMetrics()
    rc = RetentionCounters(m)
    rc.record_sweep(projects_scanned=3, traces_deleted=10)
    rc.record_sweep(projects_scanned=3, traces_deleted=5)
    rc.record_sweep_error()

    body, _ = render_metrics(m)
    text = body.decode("utf-8")
    assert "strathon_receiver_retention_sweeps_total 2.0" in text
    assert "strathon_receiver_retention_traces_deleted_total 15.0" in text
    assert "strathon_receiver_retention_sweep_errors_total 1.0" in text


def test_retention_counters_skip_zero_deletes():
    """A sweep that deleted 0 traces should still count as a sweep."""
    m = StrathonMetrics()
    rc = RetentionCounters(m)
    rc.record_sweep(projects_scanned=1, traces_deleted=0)
    rc.record_sweep(projects_scanned=1, traces_deleted=0)

    body, _ = render_metrics(m)
    text = body.decode("utf-8")
    assert "strathon_receiver_retention_sweeps_total 2.0" in text
    assert "strathon_receiver_retention_traces_deleted_total 0.0" in text


def test_policy_matches_counter_uses_labels():
    m = StrathonMetrics()
    m.policy_matches.labels(action="block").inc()
    m.policy_matches.labels(action="block").inc()
    m.policy_matches.labels(action="log").inc()
    m.policy_matches.labels(action="alert").inc()

    body, _ = render_metrics(m)
    text = body.decode("utf-8")
    assert 'strathon_receiver_policy_matches_total{action="block"} 2.0' in text
    assert 'strathon_receiver_policy_matches_total{action="log"} 1.0' in text
    assert 'strathon_receiver_policy_matches_total{action="alert"} 1.0' in text


def test_sampling_rate_gauge_can_be_set():
    m = StrathonMetrics()
    m.sampling_rate.set(0.25)
    body, _ = render_metrics(m)
    text = body.decode("utf-8")
    assert "strathon_receiver_sampling_rate 0.25" in text
