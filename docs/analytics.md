# Analytics

Know what your agents are doing and what it costs. Strathon exposes three
read endpoints for operator analytics: the same APIs the dashboard, Grafana
integrations, and enterprise tooling consume.

## Trace list

```http
GET /v1/traces?limit=50&agent_name=my-bot&intervention_state=blocked
Authorization: Bearer stra_…
```

Lists traces for the caller's project, newest first. Supports:

- `start_after` / `start_before`: nanosecond unix or ISO 8601
- `agent_name`: exact match
- `intervention_state`: exact match (e.g. `blocked`, `steered`)
- `cursor`: opaque string from `next_cursor` in the previous response
- `limit`: 1–1000, default 50

Each trace in the response includes trace_id, start/end timestamps,
agent_name, workflow_name, total_cost_usd, span_count, and
intervention_state.

Requires `traces:read` scope.

## Trace tree

```http
GET /v1/traces/{trace_id}/tree
Authorization: Bearer stra_…
```

Reconstructs the full span hierarchy for a single trace. Returns:

- `trace`: trace-level metadata (timestamps, agent, cost, span count)
- `root`: the root span node with nested `children` arrays
- `span_count`: total spans in the trace

Each span node carries name, kind, start/end time, duration_ms, cost,
tokens, model, agent, tool, intervention_state, and a `children` array
of child span nodes.

If the trace has multiple root spans (no parent_span_id), `root` is an
array instead of a single object.

Requires `traces:read` scope.

## Span aggregation

```http
GET /v1/spans/aggregate?group_by=request_model&time_bucket=1d
Authorization: Bearer stra_…
```

Groups spans by a dimension and returns aggregate metrics:

- `span_count`: number of spans in the group
- `total_cost_usd`: sum of cost_usd
- `total_input_tokens`: sum of input_tokens
- `total_output_tokens`: sum of output_tokens

Parameters:

- `group_by`: one of: `agent_name`, `tool_name`, `operation_name`,
  `request_model`, `provider_name`, `kind`, `status_code`,
  `intervention_state`
- `time_bucket`: optional: `1h`, `6h`, `1d`, `7d`, `30d`. Adds a
  `bucket` field (nanosecond timestamp of the bucket start)
- `start_after` / `start_before`: time range filter
- `limit`: 1–1000, default 100

Requires `traces:read` scope.

## Behavioral drift detection (Vigil)

Vigil watches each agent's behavior against its own learned baseline and
fires an alert when the behavior shifts. It runs as a background task in the
receiver (60-second tick) and requires no configuration to start; it
calibrates itself from production traffic.

Four signals are tracked per agent, computed over a trailing 5-minute window:

| Signal | Definition | Alert severity |
|--------|------------|----------------|
| `deny_rate` | Fraction of spans blocked or denied by policy | high |
| `error_rate` | Fraction of spans with `ERROR` status | high |
| `tool_call_rate` | Spans per minute | medium |
| `cost_rate` | USD per minute | medium |

### How it works

EWMA (exponentially weighted moving average) establishes the baseline;
CUSUM (cumulative sum) detects sustained shifts away from it. Together they
catch both a sudden spike and a slow drift that no single threshold would.

Each agent×signal baseline calibrates independently: Vigil accumulates one
observation per tick in which the agent was active, and starts alerting only
after 100 observations (configurable). Until then it learns silently; a new
agent never alerts on its first day of normal behavior. After an alert fires,
the CUSUM accumulators reset so one sustained shift produces one alert, not a
storm.

Baselines are held in receiver memory and rebuild from live traffic after a
restart (the calibration window starts over).

### Alerts

A drift alert is dispatched through the notification system as a
`behavioral_drift` event, over the same channels (Slack, Discord, webhook) used
for policy and approval events. The payload carries the agent, the signal,
the current value, the baseline EWMA, a severity, and a human-readable
message:

```json
{
  "type": "behavioral_drift",
  "agent_name": "support-agent",
  "metric": "deny_rate",
  "current_value": 0.42,
  "baseline_ewma": 0.03,
  "severity": "high",
  "message": "Agent 'support-agent' deny_rate drifted significantly (current: 0.4200, baseline: 0.0300)"
}
```

A rising `deny_rate` on a previously quiet agent is the classic signature of
prompt injection or goal hijack: the agent starts attempting calls your
policies exist to stop. Drift detection surfaces the pattern; the policies
themselves remain the enforcement.

### Configuration

| Env var | Default | Meaning |
|---------|---------|---------|
| `STRATHON_VIGIL_MIN_SPANS` | `100` | Observations before a baseline calibrates and can alert |
| `STRATHON_VIGIL_EWMA_ALPHA` | `0.3` | EWMA smoothing factor (higher = baseline adapts faster) |
| `STRATHON_VIGIL_CUSUM_THRESHOLD` | `5.0` | CUSUM breach level that fires an alert |
| `STRATHON_VIGIL_CUSUM_DRIFT` | `0.5` | CUSUM slack: drift smaller than this is absorbed as noise |

## Related

- [Span search](spans.md): the raw data these analytics aggregate
- [Budgets](budgets.md): turn cost visibility into enforced caps
- [Metrics](metrics.md): Prometheus counters for the receiver itself
