# Analytics

Strathon exposes three read endpoints for operator analytics. These are
the APIs that dashboards, Grafana integrations, and enterprise tooling
consume.

## Trace list

```http
GET /v1/traces?limit=50&agent_name=my-bot&intervention_state=blocked
Authorization: Bearer stra_…
```

Lists traces for the caller's project, newest first. Supports:

- `start_after` / `start_before` — nanosecond unix or ISO 8601
- `agent_name` — exact match
- `intervention_state` — exact match (e.g. `blocked`, `steered`)
- `cursor` — opaque string from `next_cursor` in the previous response
- `limit` — 1–1000, default 50

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

- `trace` — trace-level metadata (timestamps, agent, cost, span count)
- `root` — the root span node with nested `children` arrays
- `span_count` — total spans in the trace

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

- `span_count` — number of spans in the group
- `total_cost_usd` — sum of cost_usd
- `total_input_tokens` — sum of input_tokens
- `total_output_tokens` — sum of output_tokens

Parameters:

- `group_by` — one of: `agent_name`, `tool_name`, `operation_name`,
  `request_model`, `provider_name`, `kind`, `status_code`,
  `intervention_state`
- `time_bucket` — optional: `1h`, `6h`, `1d`, `7d`, `30d`. Adds a
  `bucket` field (nanosecond timestamp of the bucket start)
- `start_after` / `start_before` — time range filter
- `limit` — 1–1000, default 100

Requires `traces:read` scope.
