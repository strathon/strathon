# Span search

Strathon stores every OTel span agents emit. The span search API
gives operators a filtered, paginated view into what agents actually
did, when, with which tools, and at what cost.

## Endpoints

| Method | Path                           | Scope         |
|--------|--------------------------------|---------------|
| GET    | `/v1/spans`                    | `traces:read` |
| GET    | `/v1/spans/{trace_id}/{span_id}` | `traces:read` |

## Searching spans

`GET /v1/spans` returns spans for the caller's project, newest first.
Every parameter is optional; omitting all of them returns the most
recent spans up to the page limit.

### Time range

Use `start_after` and `start_before` to bound the search window.
Both accept nanosecond unix timestamps or ISO 8601 strings:

```
GET /v1/spans?start_after=2026-05-01T00:00:00Z&start_before=2026-05-17T23:59:59Z
GET /v1/spans?start_after=1714521600000000000&start_before=1715990399000000000
```

### Column filters

The most common span fields are denormalized into indexed columns.
Filter them by name as query parameters:

```
GET /v1/spans?agent_name=research-bot
GET /v1/spans?tool_name=web_search&kind=CLIENT
GET /v1/spans?request_model=gpt-4o&status_code=ERROR
GET /v1/spans?intervention_state=blocked
```

Available column filters: `agent_name`, `agent_id`, `tool_name`,
`operation_name`, `request_model`, `response_model`, `kind`,
`status_code`, `intervention_state`, `workflow_name`,
`conversation_id`, `provider_name`.

All column filters are equality checks. Combine them to narrow
results (they AND together).

### Attribute containment

Arbitrary span attributes stored in the JSONB `attributes` column
can be searched via the `attr.` prefix:

```
GET /v1/spans?attr.gen_ai.tool.name=calculator
GET /v1/spans?attr.custom.department=finance
```

This compiles to a Postgres `attributes @> '{"key": "value"}'::jsonb`
containment check, which is backed by the GIN index from migration
011. Multiple `attr.*` params AND together.

Values are matched as strings. For boolean or numeric matching,
store the value as a string in the span attributes at ingest time.

### Pagination

Responses include `next_cursor` when more rows are available:

```
GET /v1/spans?limit=50
→ {"data": [...], "next_cursor": "eyJ0IjoxNzE1..."}

GET /v1/spans?limit=50&cursor=eyJ0IjoxNzE1...
→ {"data": [...], "next_cursor": null}
```

Pagination uses keyset cursors over
`(start_time_unix_nano DESC, trace_id, span_id)` which is stable
across concurrent inserts. Hard cap per page is 1000.

## Single span detail

`GET /v1/spans/{trace_id}/{span_id}` returns one span plus its
events and links. IDs are hex-encoded (32 chars for trace_id,
16 chars for span_id):

```
GET /v1/spans/4bf92f3577b34da6a3ce929d0e0e4736/00f067aa0ba902b7
```

The response includes the same fields as the list endpoint plus
`events` (OTel span events) and `links` (OTel span links) arrays.

## Response shape

Each span in the response carries:

- `trace_id`, `span_id`, `parent_span_id` (hex-encoded)
- `name`, `kind`, `status_code`, `status_message`
- `start_time`, `end_time` (ISO 8601)
- Denormalized gen_ai fields: `operation_name`, `provider_name`,
  `request_model`, `response_model`, `agent_name`, `agent_id`,
  `tool_name`, `workflow_name`, `conversation_id`
- `tokens` object: `input_tokens`, `output_tokens`,
  `reasoning_tokens`, `cache_read_tokens`, `cache_creation_tokens`
- `cost` object: `cost_usd`, `cost_cumulative_usd`,
  `cost_subtree_usd` (string-encoded decimals to preserve precision)
- Strathon agent fields: `agent_depth`, `spawn_parent_agent_id`,
  `spawn_reason`, `intervention_state`, `halt_reason`
- `attributes` (full JSONB dict)

## Indexing

The GIN index on `attributes` (migration 011) uses the
`jsonb_path_ops` operator class. This produces a compact index that
supports `@>` containment queries over nested JSONB structures.

For production hot-adds on large tables, use CONCURRENTLY:

```sql
CREATE INDEX CONCURRENTLY idx_spans_attributes_gin
    ON spans USING GIN (attributes jsonb_path_ops);
```

The denormalized column filters use the B-tree indexes from
migration 001 (partial indexes on agent_name, tool_name,
operation_name where the column is NOT NULL).

## Partitioned storage

The spans table (along with span_events and span_links) is RANGE-
partitioned on `start_time_unix_nano` with monthly granularity.
The PK is `(start_time_unix_nano, trace_id, span_id)`. Children
are co-partitioned with composite FK. No default partition.

A background worker (spans_worker.py) premakes 3 months of
partitions ahead and drops those older than 12 months, advisory-
lock-guarded for multi-replica safety. Partition naming follows
`spans_yYYYYmMM`.

Span search queries use `SET LOCAL plan_cache_mode = 'force_custom_plan'`
to ensure Postgres uses plan-time partition pruning rather than
switching to a generic plan after the 6th prepared-statement
execution.
