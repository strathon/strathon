# Budgets

Strathon enforces per-project cost and iteration caps on agent activity.
When a budget is exceeded, the receiver writes a halt to `halt_state`
and every SDK polling `/v1/intervention/sync` sees it within one poll
cycle. The next tool call raises `StrathonHaltExceeded` at the tool
boundary; no agent restart is needed.

When the budget's window rolls over to a fresh one, the halt
automatically clears.

## How it works

A background task in the receiver process (the **budget monitor**)
ticks every 5 seconds by default. On each tick it:

1. Iterates every active budget.
2. Aggregates the relevant spans table data over the active window.
   * For **cost budgets**: `SUM(cost_usd)` over spans matching the
     budget's scope, where `end_time >= window_start`.
   * For **iteration budgets**: `COUNT(*)` of tool spans matching
     the scope in the rolling last-N-seconds window.
3. Compares the value to the budget's threshold.
4. If over threshold and no `budget_monitor`-actor halt exists for
   this budget: creates one.
5. If under threshold and a `budget_monitor` halt exists: clears it.
6. If a cost budget's `budget_reset_at` is in the past: advances it
   forward one duration and re-evaluates against the new (empty)
   window. If the monitor was down for multiple windows, advance
   repeatedly until reset_at is in the future.

**Operator halts (`actor=user`) are never auto-cleared by the
monitor.** Only the halts the monitor itself produced get reconciled.

## Window semantics

Cost budgets use **fixed-window** reset, the same shape LiteLLM and
every other LLM gateway uses for cost caps. A budget with
`budget_duration='30d'` created at 3:17pm on May 1 resets at 3:17pm on
May 31, then 3:17pm on June 30, and so on. There's no calendar
alignment — operators who want "resets at midnight UTC" can create
the budget at midnight.

Iteration budgets use a **rolling** window (last N seconds). Loop
detection is inherently "in the last N seconds"; a fixed window
would let a runaway loop survive a boundary reset.

## Why cost is on the span (not a counter)

The naive design — `UPDATE budgets SET spent_usd = spent_usd + cost`
on every span ingest — serializes every concurrent ingest on the
same project on one row. At scale this becomes the bottleneck.

Strathon writes the cost on each span (the `spans.cost_usd` column,
populated by the ingest path from `gen_ai.usage.input_tokens` and
the model price). Budget evaluation is a `SUM(cost_usd) WHERE ...`
aggregation over an indexed partial range. Ingest has no contention,
and you get per-trace/per-agent/per-model cost rollups from the
same data without schema changes.

This follows the industry-standard pattern for LLM cost tracking.

## Configuration

| Env var                                       | Default | Meaning                                       |
|-----------------------------------------------|---------|-----------------------------------------------|
| `STRATHON_BUDGET_EVAL_INTERVAL_SECONDS`       | `5.0`   | Seconds between monitor ticks.                |
| `STRATHON_BUDGET_MAX_PER_TICK`                | `500`   | Max budgets evaluated per replica per tick.   |
| `STRATHON_MODEL_PRICES_PATH`                  | unset   | Override the vendored model price JSON path.  |

## REST API

All endpoints require an API key with the appropriate scope.

### Create a cost budget

```
POST /v1/budgets
Authorization: Bearer <key with budgets:write>

{
  "name": "monthly cap",
  "scope": "project",
  "max_spend_usd": "100",
  "budget_duration": "30d"
}
```

Returns `201` with the created budget. `budget_reset_at` is computed
as `now + budget_duration`.

### Create an iteration-limit budget

```
POST /v1/budgets

{
  "name": "loop guard",
  "scope": "agent",
  "scope_value": "research-agent",
  "max_repeated_calls": 50,
  "loop_window_seconds": "60"
}
```

The `scope_value` field must be set for `agent` and `model` scopes;
omitted for `project` scope.

### Scope dimensions

| Scope     | scope_value     | Meaning                                      |
|-----------|-----------------|----------------------------------------------|
| `project` | (must be null)  | Every span in the project counts.            |
| `agent`   | the `agent_id`  | Only spans with this `gen_ai.agent.id`.      |
| `model`   | the model name  | Only spans with this `gen_ai.request.model`. |

Tag-based budgets (LiteLLM's cost-center pattern) are deferred to a
future commit; the schema's `scope` column is `TEXT` so adding new
scope kinds doesn't require a migration.

### Read live spend

```
GET /v1/budgets/{id}/spend
```

Returns the **live** aggregation — runs the SUM query, not the
cached snapshot the monitor writes. For dashboards that need
authoritative numbers vs the few-second staleness of the cached
value.

### Update / delete

```
PATCH  /v1/budgets/{id}
DELETE /v1/budgets/{id}
```

PATCH cannot change `scope` or `budget_duration` — changing these
invalidates the existing spend tracking. Create a new budget if you
need a different scope or window.

## Model price overrides

The receiver ships a vendored model price catalog at
`receiver/data/model_prices.json`, sourced from LiteLLM's upstream
`model_prices_and_context_window.json` (MIT-licensed). 20 of the
most-used models are bundled in v1; the file gets refreshed
periodically.

Operators who've negotiated a discount with their provider, or who
run self-hosted/fine-tuned models the catalog doesn't know, set
per-project overrides:

```
POST /v1/model_prices
Authorization: Bearer <key with model_prices:write>

{
  "model_name": "gpt-4o",
  "input_cost_per_token": "0.000002",
  "output_cost_per_token": "0.000008"
}
```

The override applies only to the calling key's project. The cost
calculator at ingest time checks per-project overrides first, then
falls back to the vendored catalog.

A span whose model is in neither the override nor the catalog gets
`cost_usd = NULL` — not zero. Returning NULL surfaces "unknown" in
dashboards rather than silently misattributing spend.

## Scopes

| Scope                 | Endpoints                                      |
|-----------------------|------------------------------------------------|
| `budgets:read`        | `GET /v1/budgets`, `GET /v1/budgets/{id}`, `GET /v1/budgets/{id}/spend` |
| `budgets:write`       | `POST /v1/budgets`, `PATCH /v1/budgets/{id}`, `DELETE /v1/budgets/{id}` |
| `model_prices:read`   | `GET /v1/model_prices`                         |
| `model_prices:write`  | `POST /v1/model_prices`, `DELETE /v1/model_prices/{name}` |

## Composition with operator halts

The halt mechanism is shared with operator kill-switches (see
[intervention.md](intervention.md)). An active halt — whether from
the budget monitor or from an operator — raises
`StrathonHaltExceeded` at the SDK's tool boundary.

If both an operator halt and a budget halt are active, either is
sufficient to stop the agent. Clearing one does not clear the other.

## Multi-replica deployments

The monitor uses a Postgres advisory lock (`pg_try_advisory_lock`) so
that only one replica's monitor evaluates a given tick. Other
replicas' ticks see the lock held, skip the work, and try again on
their next tick. No extra coordination service required.

**Caveat**: If you run PgBouncer in transaction-pooling mode
(`pool_mode=transaction`), advisory locks don't work correctly —
connections are recycled between transactions, releasing the lock
unexpectedly. Either use session pooling (`pool_mode=session`) for
the receiver's connections, or run the receiver against Postgres
directly. See [self-hosting.md](self-hosting.md).

## Circuit breakers

Budgets contain *spend*; circuit breakers track *failure*. A circuit breaker
exists per agent and per tool, and follows the standard
CLOSED → OPEN → HALF-OPEN pattern: it trips when an entity accumulates too
many error-status spans inside a sliding window, holds OPEN for a cooldown,
then probes recovery through HALF-OPEN before closing again. Breakers
require no setup — they are created automatically the first time an agent or
tool reports activity.

### What a tripped breaker does

Be precise about the semantics, because they differ from halts:

- While a breaker is OPEN (or HALF-OPEN), every span ingested for that
  entity is annotated with `strathon.circuit_breaker.state` and
  `strathon.circuit_breaker.entity`.
- Open breakers are visible in the dashboard and via the API (below).
- The breaker does **not** itself stop the agent's calls. Call-stopping
  enforcement lives in the SDK policy engine and in halts. A breaker is the
  automatic, self-recovering *signal* that an agent or tool is repeatedly
  failing; to turn that signal into a hard stop, create a halt on the agent
  (manually, or from your alerting automation) — `StrathonHaltExceeded` then
  stops the agent at the tool boundary, exactly as with budget halts above.

State is held in receiver memory and resets on restart (breakers re-learn
from live traffic).

### API

```
GET  /v1/circuit-breakers          # list all breakers + state summary
POST /v1/circuit-breakers/reset    # body: {"entity_id": "...", "entity_type": "agent" | "tool"}
```

`GET` requires `traces:read`; `reset` requires `policies:write`. The list
response includes per-breaker `state`, `errors_in_window`, `total_trips`,
and the active thresholds.

### Configuration

| Env var | Default | Meaning |
|---------|---------|---------|
| `STRATHON_CB_ERROR_THRESHOLD` | `10` | Errors inside the window that trip the breaker |
| `STRATHON_CB_WINDOW_SECONDS` | `300` | Sliding error-counting window |
| `STRATHON_CB_COOLDOWN_SECONDS` | `60` | How long OPEN holds before HALF-OPEN probing |
| `STRATHON_CB_HALF_OPEN_MAX` | `3` | Successful probes required to close again (a single failure during probing re-opens) |
