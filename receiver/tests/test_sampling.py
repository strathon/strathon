"""Unit tests for the receiver's sampling module."""

import os
import struct
import sys
import threading
from unittest.mock import patch

import pytest

sys.path.insert(0, "/home/claude/strathon/receiver")

from sampling import (
    EXPENSIVE_LLM_TOKEN_THRESHOLD,
    SamplingConfig,
    SamplingCounters,
    _is_always_kept,
    _trace_id_to_uniform,
    should_keep_span,
)


# ---- SamplingConfig.from_env ----


def test_from_env_defaults_to_1_when_missing():
    with patch.dict(os.environ, {}, clear=True):
        cfg = SamplingConfig.from_env()
    assert cfg.sample_rate == 1.0


def test_from_env_parses_valid_rate():
    with patch.dict(os.environ, {"STRATHON_SAMPLING_RATE": "0.25"}, clear=True):
        cfg = SamplingConfig.from_env()
    assert cfg.sample_rate == 0.25


def test_from_env_clamps_above_1():
    with patch.dict(os.environ, {"STRATHON_SAMPLING_RATE": "1.5"}, clear=True):
        cfg = SamplingConfig.from_env()
    assert cfg.sample_rate == 1.0


def test_from_env_clamps_below_0():
    with patch.dict(os.environ, {"STRATHON_SAMPLING_RATE": "-0.1"}, clear=True):
        cfg = SamplingConfig.from_env()
    assert cfg.sample_rate == 0.0


def test_from_env_handles_garbage_input():
    with patch.dict(os.environ, {"STRATHON_SAMPLING_RATE": "not-a-number"}, clear=True):
        cfg = SamplingConfig.from_env()
    assert cfg.sample_rate == 1.0  # defaults safely


# ---- _trace_id_to_uniform ----


def _trace_id_with_lower_8(value: int) -> bytes:
    """Build a 16-byte trace_id whose lower 8 bytes encode `value` big-endian."""
    return b"\x00" * 8 + struct.pack(">Q", value)


def test_trace_id_to_uniform_zero_lower_bytes():
    assert _trace_id_to_uniform(_trace_id_with_lower_8(0)) == 0.0


def test_trace_id_to_uniform_half():
    # After >> 11, half-range is at 2**63 -> shifted to 2**52, divided by 2**53 = 0.5
    half = 2**63
    val = _trace_id_to_uniform(_trace_id_with_lower_8(half))
    assert 0.499 < val < 0.501


def test_trace_id_to_uniform_strictly_below_1():
    """Even at the maximum trace_id, the uniform value must be < 1.0.

    Critical: if this could return 1.0, a sample_rate=1.0 span would be
    paradoxically dropped (since the comparison is strictly less than).
    """
    near_max = 2**64 - 1
    val = _trace_id_to_uniform(_trace_id_with_lower_8(near_max))
    assert val < 1.0
    assert val > 0.999


def test_trace_id_to_uniform_empty_bytes():
    """Defensive: empty/missing trace_id should NOT crash."""
    assert _trace_id_to_uniform(b"") == 0.0


def test_trace_id_to_uniform_short_bytes():
    """Shorter than 8 bytes should be left-padded."""
    val = _trace_id_to_uniform(b"\x01")
    # Value 1 -> after >> 11 -> 0; uniform = 0.0
    assert val == 0.0


# ---- _is_always_kept ----


def _cfg(rate=1.0):
    return SamplingConfig(sample_rate=rate)


def test_always_kept_policy_blocked():
    assert _is_always_kept({"strathon.policy.blocked": True}, "OK", _cfg()) is True


def test_always_kept_policy_steered():
    assert _is_always_kept({"strathon.policy.steered": True}, "OK", _cfg()) is True


def test_always_kept_policy_matched_ids():
    assert _is_always_kept({"strathon.policy.matched_ids": "abc,def"}, "OK", _cfg()) is True


def test_always_kept_status_error():
    assert _is_always_kept({}, "ERROR", _cfg()) is True


def test_always_kept_expensive_llm():
    attrs = {"gen_ai.usage.total_tokens": EXPENSIVE_LLM_TOKEN_THRESHOLD + 1}
    assert _is_always_kept(attrs, "OK", _cfg()) is True


def test_not_always_kept_cheap_llm():
    attrs = {"gen_ai.usage.total_tokens": 100}
    assert _is_always_kept(attrs, "OK", _cfg()) is False


def test_not_always_kept_routine_span():
    assert _is_always_kept({}, "OK", _cfg()) is False


def test_always_kept_handles_garbage_token_count():
    """Non-numeric total_tokens shouldn't crash."""
    attrs = {"gen_ai.usage.total_tokens": "not-an-int"}
    assert _is_always_kept(attrs, "OK", _cfg()) is False


# ---- should_keep_span ----


def test_keeps_everything_at_rate_1():
    trace_id = _trace_id_with_lower_8(2**63)  # uniform ~0.5
    keep, force_kept = should_keep_span(trace_id, {}, "OK", _cfg(rate=1.0))
    assert keep is True
    assert force_kept is False


def test_drops_everything_at_rate_0_except_always_kept():
    trace_id = _trace_id_with_lower_8(2**63)
    # Routine span
    keep, force_kept = should_keep_span(trace_id, {}, "OK", _cfg(rate=0.0))
    assert keep is False
    # Error span -> force kept
    keep, force_kept = should_keep_span(trace_id, {}, "ERROR", _cfg(rate=0.0))
    assert keep is True
    assert force_kept is True


def test_force_kept_only_reported_when_sampling_would_drop():
    """At rate 1.0, an always-keep span shouldn't report force_kept."""
    trace_id = _trace_id_with_lower_8(2**63)
    keep, force_kept = should_keep_span(
        trace_id, {"strathon.policy.blocked": True}, "OK", _cfg(rate=1.0)
    )
    assert keep is True
    assert force_kept is False  # rate 1.0 would have kept this anyway


def test_force_kept_reported_when_sampling_would_drop():
    """At rate 0.0, an always-keep span should report force_kept=True."""
    trace_id = _trace_id_with_lower_8(2**63)
    keep, force_kept = should_keep_span(
        trace_id, {"strathon.policy.blocked": True}, "OK", _cfg(rate=0.0)
    )
    assert keep is True
    assert force_kept is True


def test_trace_level_coherence():
    """All spans of a given trace should get the same probabilistic decision."""
    cfg = _cfg(rate=0.3)
    trace_id = _trace_id_with_lower_8(123456789)
    # 100 calls with same trace_id but different attrs should give same result
    results = {should_keep_span(trace_id, {"i": i}, "OK", cfg)[0] for i in range(100)}
    assert len(results) == 1, "Decision must be deterministic per trace_id"


def test_different_traces_get_different_decisions_at_partial_rate():
    """At rate=0.5, a sample of trace_ids should split roughly half/half.

    We use full 16-byte random-looking trace_ids (via SHA-256 of the
    iterator) so they actually spread across the [0, 1) uniform range.
    Multiplying small ints by a prime stays near zero in the uniform
    mapping and doesn't exercise the partitioning at all.
    """
    import hashlib

    cfg = _cfg(rate=0.5)
    kept = 0
    n = 1000
    for i in range(n):
        # SHA-256 gives 32 bytes; take first 16 for a trace_id-shaped value
        trace_id = hashlib.sha256(str(i).encode()).digest()[:16]
        keep, _ = should_keep_span(trace_id, {}, "OK", cfg)
        if keep:
            kept += 1
    # With 1000 samples at p=0.5, expect ~500. Wide tolerance to absorb
    # statistical noise without making the test flaky.
    assert 400 < kept < 600, f"Expected ~500 kept; got {kept}"


# ---- SamplingCounters ----


def test_counters_initial_state():
    c = SamplingCounters()
    snap = c.snapshot()
    assert snap == {
        "spans_kept_total": 0,
        "spans_dropped_total": 0,
        "spans_force_kept_total": 0,
    }


def test_counters_record_kept_and_dropped():
    c = SamplingCounters()
    c.record_kept()
    c.record_kept(force_kept=True)
    c.record_dropped()
    snap = c.snapshot()
    assert snap["spans_kept_total"] == 2
    assert snap["spans_force_kept_total"] == 1
    assert snap["spans_dropped_total"] == 1


def test_counters_thread_safety():
    """Hammered from multiple threads, counts must add up exactly."""
    c = SamplingCounters()
    per_thread = 1000
    threads = []

    def hammer():
        for _ in range(per_thread):
            c.record_kept()
            c.record_dropped()

    for _ in range(10):
        threads.append(threading.Thread(target=hammer))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    snap = c.snapshot()
    assert snap["spans_kept_total"] == 10 * per_thread
    assert snap["spans_dropped_total"] == 10 * per_thread
