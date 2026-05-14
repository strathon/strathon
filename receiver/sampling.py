"""Server-side sampling for the Strathon receiver.

We make a keep/drop decision for each span at ingest time, before writing
to the database. This is a tail-based-per-span approach: we read the span's
attributes (already populated by the SDK) and decide whether to store it.

### What's always kept (no sampling)

Some spans have outsized audit / debugging value and are kept regardless of
the configured sampling rate:

- Spans with any ``strathon.policy.*`` annotation (matched / blocked /
  steered). These ARE the audit trail.
- Spans with ``status_code == "ERROR"``.
- LLM spans with token usage above a configured threshold (expensive calls
  that an operator probably wants to inspect).

### Probabilistic sampling for routine spans

For everything else we sample deterministically by trace_id. This is the
OTel-standard "TraceIDRatioBased" approach:

- Hash the trace_id to a uniform [0, 1) float.
- Keep the span if that float is < sample_rate.

Hashing trace_id (not span_id) means every span belonging to a given trace
gets the same keep/drop decision. Avoids incomplete traces in storage.

### Configuration

A single environment variable controls behavior:

    STRATHON_SAMPLING_RATE   (float, default 1.0)

    1.0 = keep all routine spans (no sampling, backward compatible)
    0.1 = keep 10% of routine spans
    0.0 = drop all routine spans (only the "always keep" rules apply)

Values outside [0, 1] are clamped. The default of 1.0 means existing
deployments see no behavior change when this module is enabled.

### Why per-span and not per-trace at the collector

A full collector-style tail sampler buffers all spans of a trace and decides
at trace completion. That requires memory, completion detection, and edge
cases for partial traces under load. For v1 we don't need that complexity:
each span has enough metadata in its attributes to decide standalone, and
trace-level coherence is preserved by hashing trace_id.
"""

from __future__ import annotations

import logging
import os
import struct
import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger("strathon.receiver.sampling")


# Token-count threshold above which an LLM call is always kept regardless of
# sampling rate. Operators investigating cost will want these.
EXPENSIVE_LLM_TOKEN_THRESHOLD = 5000


@dataclass(frozen=True)
class SamplingConfig:
    """Effective sampling configuration for the receiver."""

    sample_rate: float
    expensive_llm_token_threshold: int = EXPENSIVE_LLM_TOKEN_THRESHOLD

    @classmethod
    def from_env(cls) -> "SamplingConfig":
        raw = os.getenv("STRATHON_SAMPLING_RATE")
        rate = 1.0
        if raw is not None:
            try:
                rate = float(raw)
            except ValueError:
                logger.warning(
                    "STRATHON_SAMPLING_RATE=%r is not a number; defaulting to 1.0", raw
                )
                rate = 1.0
        rate = max(0.0, min(1.0, rate))
        return cls(sample_rate=rate)


class SamplingCounters:
    """Thread-safe counters for sampling decisions.

    Held on app.state; will be exposed via the /metrics endpoint in C4.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.spans_kept_total: int = 0
        self.spans_dropped_total: int = 0
        self.spans_force_kept_total: int = 0

    def record_kept(self, force_kept: bool = False) -> None:
        with self._lock:
            self.spans_kept_total += 1
            if force_kept:
                self.spans_force_kept_total += 1

    def record_dropped(self) -> None:
        with self._lock:
            self.spans_dropped_total += 1

    def snapshot(self) -> Dict[str, int]:
        with self._lock:
            return {
                "spans_kept_total": self.spans_kept_total,
                "spans_dropped_total": self.spans_dropped_total,
                "spans_force_kept_total": self.spans_force_kept_total,
            }


def _trace_id_to_uniform(trace_id: bytes) -> float:
    """Map an OTel trace_id (16 bytes) to a uniform float in [0, 1).

    Mirrors the OTel ``TraceIdRatioBased`` sampler: take the lower 8 bytes,
    interpret as a 64-bit unsigned int. We then drop the 11 lowest bits and
    divide by 2**53, which fits cleanly in an IEEE-754 double's mantissa
    and guarantees the result is strictly less than 1.0 (a naive division
    by 2**64 can round up to 1.0 for large values, which would let
    sample_rate=1.0 paradoxically drop a span).
    """
    if not trace_id:
        # No trace_id -> deterministically place in the kept bucket so we
        # don't drop orphan spans by accident.
        return 0.0
    # Take 8 bytes from a stable position (the lower 8 bytes of the 16-byte
    # trace_id, which is the convention OTel SDKs use).
    chunk = trace_id[-8:] if len(trace_id) >= 8 else trace_id.rjust(8, b"\x00")
    (n,) = struct.unpack(">Q", chunk)
    # Drop 11 low bits -> 53-bit integer in [0, 2**53). Divide by 2**53 for
    # uniform [0, 1) — exactly representable, no rounding to 1.0.
    return (n >> 11) / 2**53


def _is_always_kept(
    attrs: Dict[str, Any],
    status_code: Optional[str],
    config: SamplingConfig,
) -> bool:
    """Return True if this span should bypass sampling and always be stored."""
    # Any Strathon policy annotation makes the span audit-critical
    if attrs.get("strathon.policy.blocked"):
        return True
    if attrs.get("strathon.policy.steered"):
        return True
    if attrs.get("strathon.policy.steer_attempted"):
        return True
    if attrs.get("strathon.policy.matched_ids"):
        return True

    # Errors are always interesting
    if status_code == "ERROR":
        return True

    # Expensive LLM calls: keep for cost analysis
    total_tokens = attrs.get("gen_ai.usage.total_tokens")
    if total_tokens is not None:
        try:
            if int(total_tokens) > config.expensive_llm_token_threshold:
                return True
        except (TypeError, ValueError):
            pass

    return False


def should_keep_span(
    trace_id: bytes,
    attrs: Dict[str, Any],
    status_code: Optional[str],
    config: SamplingConfig,
) -> tuple[bool, bool]:
    """Decide whether to keep a span and whether the decision was forced.

    Returns ``(keep, force_kept)``:

    - ``keep`` is True if the span should be stored.
    - ``force_kept`` is True if the span was kept by an "always keep" rule
      (i.e. would have been dropped by the sample rate alone). Reported via
      counters so operators can see how much the safety rules saved.
    """
    if _is_always_kept(attrs, status_code, config):
        # If sample rate is 1.0 we wouldn't have dropped this either, so it
        # isn't "force kept" in any meaningful sense. Only report force_kept
        # when probabilistic sampling would have dropped it.
        threshold = _trace_id_to_uniform(trace_id)
        would_drop = threshold >= config.sample_rate
        return True, would_drop

    # Routine span: trace-level deterministic sample
    threshold = _trace_id_to_uniform(trace_id)
    keep = threshold < config.sample_rate
    return keep, False


__all__ = [
    "EXPENSIVE_LLM_TOKEN_THRESHOLD",
    "SamplingConfig",
    "SamplingCounters",
    "should_keep_span",
]
