# Sampling

Strathon supports server-side sampling at ingest time to control storage
cost without losing audit-critical spans. The sampling decision is made
per-span as it arrives at the receiver, after the SDK has already enforced
any block/steer policies — so policy enforcement is unaffected by sampling.

## Configuration

A single environment variable controls the sample rate:

```
STRATHON_SAMPLING_RATE   (float in [0.0, 1.0], default 1.0)
```

| Value | Behavior                                                          |
|-------|-------------------------------------------------------------------|
| `1.0` | Keep every span. Default. Backward compatible with v0 deployments.|
| `0.5` | Keep ~50% of routine traces (deterministic per trace_id).         |
| `0.1` | Keep ~10% of routine traces. Common production setting.           |
| `0.0` | Drop all routine traces. Only the "always keep" rules apply.      |

Values outside `[0.0, 1.0]` are clamped silently. Non-numeric values fall
back to the default of `1.0` (with a warning logged at startup).

The receiver logs its effective rate at startup:

```
Sampling rate: 0.100 (expensive LLM threshold: 5000 tokens)
```

## What's always kept (bypasses sampling)

These spans are persisted regardless of the configured rate because they
carry outsized audit / debugging value:

1. **Policy-annotated spans.** Any span with one of these attributes is
   considered audit-critical and always kept:
    - `strathon.policy.blocked`
    - `strathon.policy.steered`
    - `strathon.policy.steer_attempted`
    - `strathon.policy.matched_ids`

2. **Errors.** Any span with `status_code = ERROR`.

3. **Expensive LLM calls.** Any span where `gen_ai.usage.total_tokens` is
   above the configured threshold (default: 5000 tokens). These are the
   calls operators most want to inspect when investigating cost spikes.

If you set `STRATHON_SAMPLING_RATE=0.0` and an agent triggers a blocked
tool call, the receiver will still persist that span and its policy_matches
audit row. Only the routine spans around it (LLM calls, workflow steps,
tools that didn't trigger policy) get dropped.

## Trace-level coherence

Routine spans are sampled deterministically by hashing the OTel `trace_id`
to a uniform `[0, 1)` value (the standard `TraceIdRatioBased` approach,
using the upper 53 bits of the trace_id's lower 8 bytes — exactly
representable in IEEE-754 doubles).

**All spans of a given trace get the same keep/drop decision.** This means
you never end up with partial traces in storage — either the whole trace
is kept or none of it. The receiver doesn't need to buffer trace state to
guarantee this; the hash-based decision is stable across spans of the same
trace.

## When to use which rate

- **Local development / staging:** keep `1.0` so every trace is
  inspectable.
- **Production with moderate volume:** `0.1` to `0.5` gives you headroom
  on storage while preserving all incidents and policy matches.
- **Production with high volume:** `0.01` or lower; the always-keep rules
  ensure you don't lose anything that matters.

## Why per-span at ingest, not collector-style tail sampling

A full OpenTelemetry Collector tail sampler buffers all spans of a trace,
waits for trace completion, then evaluates policies against the assembled
trace. That works but requires memory, completion detection, and edge cases
under load.

Strathon doesn't need that complexity in v1: each span already carries
enough metadata in its attributes (policy annotations, status, token
counts) for a standalone keep/drop decision. Trace-level coherence is
preserved by hashing the `trace_id` rather than by buffering. Memory
footprint stays constant regardless of trace duration or fan-out.

## Monitoring

The receiver maintains in-memory counters for sampling decisions:

- `spans_kept_total`
- `spans_dropped_total`
- `spans_force_kept_total` (kept by an always-keep rule that overrode a
  would-be-drop decision)

These are exposed via the `/metrics` Prometheus endpoint, and are also
accessible via the FastAPI app state for debugging.
