# Runtime Intervention

Most agent observability tools are passive. Strathon is not.

Strathon evaluates policies *before* an agent's tool calls or LLM calls execute.
A policy can log, alert, steer, or **block** the action. This is the difference
between "you discover the problem in the dashboard tomorrow" and "the bad email
never leaves your servers."

## Policy expressions: CEL

Policies are written in [Common Expression Language (CEL)][cel] — the same
language Kubernetes admission policies, Envoy filters, gRPC interceptors, and
gcloud IAM conditions use. CEL is:

- Non-Turing-complete, with guaranteed termination
- Side-effect free, safe to evaluate untrusted input
- Microsecond-fast
- Recognized across the cloud-native ecosystem

Expressions are evaluated against a span context:

```python
{
    "name":  "langgraph.tool.send_email",
    "attrs": {
        "gen_ai.tool.name": "send_email",
        "strathon.tool.args": '{"to": "rival@competitor.com", ...}',
        "gen_ai.usage.total_tokens": 5000,
        # ... all OTel attrs available
    },
}
```

In CEL you access attrs with map indexing because the keys contain dots:

```
attrs["gen_ai.tool.name"] == "send_email" &&
attrs["strathon.tool.args"].contains("@competitor.com")
```

### Standard attributes set by Strathon instrumentations

These attributes are set consistently across all 10 framework integrations,
so policies written against them are portable:

| Attribute                       | Description                                  |
|---------------------------------|----------------------------------------------|
| `strathon.framework`            | Framework name (e.g. `langgraph`, `crewai`, `openai_agents`) |
| `strathon.tool.name`            | The tool's name (also mirrored to `gen_ai.tool.name`) |
| `strathon.tool.args`            | The tool's input arguments, as a JSON string |
| `gen_ai.tool.name`              | Standard OTel attribute, same as `strathon.tool.name` |
| `gen_ai.request.model`          | The model name (on LLM spans)                |
| `gen_ai.usage.total_tokens`     | Token count (on LLM spans)                   |

### Writing safe policy expressions

CEL raises an error when you index a map with a key that doesn't exist. To
write policies that work safely across span types where some attributes may
be missing, guard accesses with `has()`:

```
has(attrs["gen_ai.tool.name"]) &&
attrs["gen_ai.tool.name"] == "send_email" &&
attrs["strathon.tool.args"].contains("@competitor.com")
```

In practice, when a policy errors out the SDK treats it as a non-match (the
action is allowed), so missing-key errors fail safe — but they generate log
noise and reduce policy effectiveness. Use `has()` for any attribute that
isn't guaranteed to exist on every span.

## Actions

A policy has one of six actions:

| Action     | What happens                                                                                                                          | Where it runs |
|------------|---------------------------------------------------------------------------------------------------------------------------------------|---------------|
| `log`      | Annotate the matching span with `strathon.policy.*` attributes. Passive.                                                              | Server        |
| `alert`    | Fire a signed webhook (`action_config.webhook_url`). Durable, retried with exponential backoff, dead-lettered after exhaustion.       | Server        |
| `block`    | SDK raises `StrathonPolicyBlocked` before the tool/LLM call executes. Agent sees an error and adapts.                                 | SDK (client)  |
| `steer`    | SDK returns a corrective string (`action_config.replacement`) in place of real output. Agent self-corrects.                           | SDK (client)  |
| `throttle` | SDK consults a per-policy token bucket. Calls under the cap proceed; calls over it raise `StrathonPolicyThrottled` with `retry_after_seconds`. | SDK (client)  |
| `allow`    | SDK admits the call and short-circuits subsequent policies. Useful for carve-outs and required for allow-list mode.                   | SDK (client)  |

`block`, `steer`, `throttle`, and `allow` actually affect the call —
these are SDK-side because by the time a span reaches the server, the
action has already happened.

### Throttle action config

```json
{
  "name": "limit_expensive_tool",
  "match_expression": "name == 'tool.web_search'",
  "action": "throttle",
  "action_config": {
    "max_calls": 10,
    "window_seconds": 60,
    "scope": "agent"
  }
}
```

- `max_calls` (required, positive integer) — token-bucket capacity.
- `window_seconds` (required, positive number) — interval over which
  the bucket refills back to capacity. Combined with `max_calls`, this
  yields a sustained rate of `max_calls / window_seconds` calls per
  second per scope key.
- `scope` (optional, defaults to `"agent"`) — what the bucket is keyed
  by:
  - `"agent"`: one bucket per `(policy, agent_id)`. The most common
    semantic — "no single agent calls this tool more than N times per
    window."
  - `"global"`: one shared bucket per policy. Use this for
    project-wide caps that apply regardless of which agent invoked.

Throttle decisions raise `StrathonPolicyThrottled`, which is a subclass
of `StrathonPolicyBlocked` — existing `except StrathonPolicyBlocked`
handlers continue to catch it. Code that wants to distinguish a
throttle (and backoff-and-retry) from a hard block (and escalate) can
catch `StrathonPolicyThrottled` specifically. The exception carries
`retry_after_seconds` so a retry loop can sleep for the right amount.

When a throttle policy *admits* a call (the bucket had a token), the
SDK does not short-circuit — lower-priority `block` or `steer`
policies still get evaluated. A throttle that admits is "no opinion
on this call." A throttle that denies short-circuits with the throttle
decision.

State is per-process: each SDK replica holds its own bucket dict, so
in an N-process agent deploy the effective ceiling is
`N × max_calls`. The matching trade-off appears in the receiver's
rate limiter; see `docs/self-hosting.md` for the multi-replica note.

#### Counting SDK-side throttle decisions

The SDK doesn't expose its own Prometheus `/metrics` endpoint — that
matches LaunchDarkly's architecture, where application-embedded SDKs
emit analytics events and the server-side Relay Proxy aggregates them.
Strathon's equivalent is the intervention span: every `throttle`
decision (and every `block`, `steer`, and synthetic-deny in allow-list
mode) emits an intervention span with a boolean
`strathon.policy.<decision_kind>` attribute set to `true` — concretely
`strathon.policy.throttled`, `strathon.policy.blocked`, or
`strathon.policy.steered`, along with `strathon.policy.id` and
`strathon.policy.name`. To count SDK-side throttle decisions, query
intervention spans where `strathon.policy.throttled = true` — by
policy, by agent, by tool — over whatever window matters. The
receiver's own `strathon_receiver_rate_limit_rejections_total` counter
covers the HTTP-edge limiter only; it does not count SDK-side policy
throttles.

### Allow-list mode (default-deny)

By default, a call that doesn't match any policy is admitted. This is
the permissive posture and works well for projects that use Strathon
primarily for observability + spot-fix policies.

For environments that need the inverse — "list everything that's
permitted, deny the rest" — flip the project into allow-list mode by
setting `intervention_default_action` to `block`:

```bash
curl -X PATCH http://localhost:4318/v1/project/settings \
  -H "Authorization: Bearer $STRATHON_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"intervention_default_action": "block"}'
```

In allow-list mode, a call at the tool boundary must be admitted by an
explicit `action: "allow"` policy or it is denied. The SDK raises
`StrathonPolicyBlocked` with `policy_id=None` and a message that names
allow-list mode, so operators reading exception logs see the cause
immediately.

Example allow policy:

```json
{
  "name": "permit_safe_search",
  "match_expression": "name == 'tool.web_search' && attrs['strathon.agent.id'] == 'research-bot'",
  "action": "allow",
  "priority": 100
}
```

Evaluation rules:

- Policies evaluate in priority-descending order. The first matching
  policy whose action affects control flow (`block`, `steer`,
  `throttle` when denied, or `allow`) short-circuits.
- A higher-priority `block` beats a lower-priority `allow`. The
  priority ordering is preserved across action types — there is no
  "allow always wins" or "deny always wins" override.
- A matched `allow` short-circuits subsequent policies in BOTH modes.
  Outside allow-list mode, that lets operators carve a specific tool
  out from being affected by a broader lower-priority block.
- The default action is read from the project setting on each SDK
  refresh of `/v1/policies`. Flipping the project setting takes
  effect on the next refresh cycle (within 30s by default).

#### Contrast with IAM / Cedar / OPA

AWS IAM and Cedar enforce "explicit `deny` always wins" — an explicit
deny statement supersedes any explicit allow, regardless of priority.
OPA/Rego explicitly leaves the choice to the policy author: "neither
allow nor deny are keywords in Rego, so if you want to treat them as
contradictory you control which takes precedence." Strathon follows
OPA's posture: pure priority ordering across all action types, no
built-in "deny supersedes" rule.

To get IAM-style "explicit deny always wins" behavior in Strathon,
give your block policies a higher `priority` than any allow policy.
A common pattern is to reserve `priority >= 1000` for blocks and
keep allows at `priority < 1000`. Then no allow can ever override a
block, regardless of ordering changes to the rest of the rule set.

Reading the current setting:

```bash
curl http://localhost:4318/v1/project/settings \
  -H "Authorization: Bearer $STRATHON_API_KEY"
```

The endpoint requires the `project_settings:read` scope (and
`project_settings:write` for PATCH). The dev key has `*` so it can
read and write both; production keys should be scoped narrowly.

### Time-based policies

Every policy match expression is evaluated against a context that
includes `now` — a CEL `timestamp` of the current UTC time at the
moment of evaluation. Operators write time conditions using the
standard CEL timestamp methods, the same surface that gcloud IAM,
Envoy, and Kubernetes admission policies expose. Methods include:

| Method                          | Returns                       |
|---------------------------------|-------------------------------|
| `now.getFullYear()`             | 4-digit year                  |
| `now.getMonth()`                | 0-11 (January = 0)            |
| `now.getDate()`                 | day-of-month, 1-31            |
| `now.getDayOfMonth()`           | day-of-month, 0-30 (zero-based) |
| `now.getDayOfWeek()`            | **0 = Sunday, ..., 6 = Saturday** |
| `now.getDayOfYear()`            | 0-365 (zero-based)            |
| `now.getHours()`                | 0-23                          |
| `now.getMinutes()`              | 0-59                          |
| `now.getSeconds()`              | 0-59                          |

All of these accept an optional IANA timezone string —
`now.getHours("America/Los_Angeles")` — so operators in non-UTC
deployments can write policies in local terms without doing offset
math themselves.

Note the day-of-week convention: cel-spec uses **Sunday = 0**, which
differs from Python's `datetime.weekday()` (Monday = 0). The policy
sees the cel-spec value.

Time arithmetic also works: `timestamp("...")` and `duration("...")`
are CEL constructors and the standard `+` / `-` / comparison operators
combine them.

#### Example: block tool use on weekends (UTC)

```json
{
  "name": "no_weekend_tools",
  "match_expression": "now.getDayOfWeek() == 0 || now.getDayOfWeek() == 6",
  "action": "block",
  "action_config": {"message": "No agent operations on weekends."}
}
```

#### Example: restrict expensive tools to Pacific business hours

```json
{
  "name": "business_hours_only",
  "match_expression": "name.startsWith('tool.expensive_') && (now.getDayOfWeek(\"America/Los_Angeles\") == 0 || now.getDayOfWeek(\"America/Los_Angeles\") == 6 || now.getHours(\"America/Los_Angeles\") < 9 || now.getHours(\"America/Los_Angeles\") >= 17)",
  "action": "block"
}
```

#### Example: rate-limit a slow rollout via a time gate

Combine time conditions with `throttle` to phase in a new tool:
allow only N calls/minute until a flag day passes.

```json
{
  "name": "phased_rollout",
  "match_expression": "name == 'tool.new_feature' && now < timestamp(\"2026-07-01T00:00:00Z\")",
  "action": "throttle",
  "action_config": {"max_calls": 5, "window_seconds": 60, "scope": "agent"}
}
```

After the flag date the match expression returns false and the
policy stops applying; the SDK doesn't need a separate "disable"
action to retire the rule.

## Policy version history

Every policy mutation (create, update, delete) captures a versioned
snapshot in the `policy_versions` table. Sequential numbering per
policy. The audit log also captures before/after state, but the
versions table provides faster queries, structured version numbers,
and works independently of audit configuration.

### Listing versions

```http
GET /v1/policies/{policy_id}/versions
Authorization: Bearer stra_…
```

Returns versions newest-first. Each entry includes the full policy
snapshot (name, match_expression, action, action_config, applies_to,
enabled, priority) plus `change_type` (`create`, `update`, `delete`)
and `changed_at`.

### Getting a specific version

```http
GET /v1/policies/{policy_id}/versions/{version_number}
Authorization: Bearer stra_…
```

Returns the exact policy state at that version number.

Requires `policies:read` scope.

## Operator kill-switches: halts

Policies are *conditional* — they fire when a CEL expression matches. Halts
are *unconditional*: they stop an agent regardless of what it's trying to do.
This is the "something's clearly wrong, stop everything" lever.

A halt is a row in `halt_state` with a scope (`agent` or `project`), an
optional reason, and an `actor` recording who created it (`user` for an
operator, `budget_monitor` for the automatic kind — see the budgets
section below).

### Creating a halt

```http
POST /v1/halts
Authorization: Bearer <key with halts:write>

{
  "scope": "agent",
  "scope_value": "research-agent",
  "reason": "investigating runaway tool calls"
}
```

Returns `201` with the halt row. From this moment on, any SDK whose
client is polling `/v1/intervention/sync` (default cadence: 1s) sees the
halt and starts raising `StrathonHaltExceeded` at every tool-call boundary
for the matching scope.

Project-scope halts (`scope=project`, no `scope_value`) match every agent
in the project — the "kill the whole product" lever for cases where you
don't know which agent's misbehaving. Agent-scope halts only match calls
whose `gen_ai.agent.id` (or `strathon.agent.id`) equals `scope_value`.

### Clearing a halt

```http
DELETE /v1/halts/{id}
```

The halt's `cleared_at` is stamped to `now`; the next SDK poll observes
an empty halt list and tool calls resume.

### How the SDK sees it

The SDK's `HaltEnforcer` polls in the background. On each call to a
tool, `check_halt(span_context)` consults the in-process halt cache; on
a match, the dispatcher raises `StrathonHaltExceeded` with the halt's id,
scope, scope_value, and reason. The user's tool function body never
executes.

A halt check failing (network blip during refresh) is fail-open by
default — the SDK uses its last-known halt cache rather than blocking
every call. Operators who prefer safer-but-noisier semantics can opt
into fail-closed mode by passing `fail_closed=True` on the client:

```python
client = Client(
    api_key="stra_...",
    endpoint="http://localhost:4318",
    fail_closed=True,
    fail_closed_max_staleness_sec=60.0,  # default; tune for your refresh cadence
)
```

When fail-closed is on, both the halt enforcer and the policy
enforcer raise `StrathonReceiverUnreachable` at the tool boundary
whenever their cached state is older than
`fail_closed_max_staleness_sec`. The default 60s threshold is well
above the 1s halt refresh and 30s policy refresh intervals, so brief
receiver hiccups don't trip it; a sustained outage does.

`StrathonReceiverUnreachable` is distinct from `StrathonHaltExceeded`
and `StrathonPolicyBlocked` so callers handling the three cases
differently (e.g. page on-call vs retry vs surface to the user) can
branch on the exception type. The exception carries `subsystem`
(`halt_enforcer` or `policy_enforcer`), `staleness_seconds`, and
`max_staleness_seconds` attributes for diagnostic logging.

### Required scopes (halts)

`halts:read` for `GET /v1/halts` and the active-halt list in
`/v1/intervention/sync`. `halts:write` for `POST` and `DELETE`. The
seeded dev key has `*`; production keys should be scoped narrowly.

### Audit trail

Every halt row is preserved on clear — the `cleared_at` column is
stamped rather than deleting the row, so the history of who created
and cleared a halt remains queryable for as long as the row exists.
Retention sweeps don't touch `halt_state` today; if you need bounded
storage on the audit table, that's a separate cleanup job to add.

## Cost and iteration budgets

Halts can also be produced automatically by the receiver when a per-
project budget is exceeded. The budget monitor evaluates active budgets
on a tick, sums LLM cost or tool-call counts over the configured
window, and writes a halt (with `actor='budget_monitor'`) when over
threshold. When the budget's window resets, the halt auto-clears.

This is the cost-circuit-breaker story: configure a `$100/day` cap and
the agents stop on their own when it's exceeded, without an operator
having to notice or intervene. Full details, REST surface, scope
semantics, and pricing-source documentation in [`budgets.md`](budgets.md).

## Webhook delivery for alert policies

Alert policies POST to `action_config.webhook_url` whenever a matching
span is ingested. The delivery layer follows the patterns serious
webhook senders (Stripe, GitHub, OpenAI, Svix) converged on:

- **Standard Webhooks signing**. Every delivery carries three headers
  per the [Standard Webhooks spec](https://github.com/standard-webhooks/standard-webhooks):
  `webhook-id` (stable across retries, usable as an idempotency key),
  `webhook-timestamp`, and `webhook-signature` (HMAC-SHA256 of
  `{id}.{timestamp}.{body}`, base64-encoded, with `v1,` prefix).
  Consumers verify with any of seven off-the-shelf libraries; we use
  the official `standardwebhooks` reference library on the sending side.

- **Durable retries**. Each delivery is a row in `webhook_deliveries`
  with a status (`pending`/`succeeded`/`failed_retrying`/`dlq`/
  `abandoned`). Failed sends retry with exponential backoff
  (defaults: 8 attempts, 1s base, 6h cap — total ~24h window). 5xx,
  timeouts, connection errors, and 429s retry. 4xx (other than 429),
  3xx, and malformed URLs are marked `abandoned`. Exhausted retries
  move to `dlq` for operator review.

- **Atomicity with ingest**. The delivery row is inserted in the same
  database transaction as the matching `policy_matches` row, so a
  rolled-back ingest produces zero phantom deliveries.

### Creating a signing key

Sign deliveries by creating a signing key for your project:

```bash
curl -X POST http://localhost:4318/v1/webhook_signing_keys \
  -H "Authorization: Bearer $STRATHON_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{}'
```

The response includes a `secret` field starting with `whsec_`. **Save
it now** — the plaintext is shown exactly once and is not recoverable
from any subsequent endpoint. Only its SHA-256 hash is persisted.

Persist the plaintext into `STRATHON_WEBHOOK_SIGNING_SECRETS`
(comma-separated `whsec_*` values) so signing survives receiver restarts.
On boot, each plaintext is hashed and matched against the active rows
in `webhook_signing_keys`; matches are loaded into the in-memory cache.

### Rotating a signing key

Stripe-style zero-downtime rotation:

1. POST a new signing key. The response gives you the new plaintext.
2. Add the new plaintext to your consumer's verifier alongside the old
   one. Both signatures travel space-delimited in `webhook-signature`.
3. Once every consumer accepts the new key, DELETE the old key:

```bash
curl -X DELETE http://localhost:4318/v1/webhook_signing_keys/{key_id} \
  -H "Authorization: Bearer $STRATHON_API_KEY"
```

The old plaintext is immediately removed from the in-memory keystore.
The next delivery signs only with the remaining active key(s).

### Listing keys

```bash
# Active keys only (default)
curl http://localhost:4318/v1/webhook_signing_keys \
  -H "Authorization: Bearer $STRATHON_API_KEY"

# Including revoked
curl "http://localhost:4318/v1/webhook_signing_keys?include_revoked=true" \
  -H "Authorization: Bearer $STRATHON_API_KEY"
```

Returns `id`, `prefix` (4-char public handle), `created_at`,
`revoked_at`. No secret material — list endpoints never reveal
plaintext or hash.

### Required scopes

| Endpoint                                | Scope                            |
|----------------------------------------|----------------------------------|
| `GET /v1/webhook_signing_keys`         | `webhook_signing_keys:read`      |
| `POST /v1/webhook_signing_keys`        | `webhook_signing_keys:write`     |
| `DELETE /v1/webhook_signing_keys/{id}` | `webhook_signing_keys:write`     |

### Inspecting deliveries

Every alert delivery is a row in `webhook_deliveries` with a status
(`pending`, `succeeded`, `failed_retrying`, `dlq`, `abandoned`). The
REST surface lets operators see what happened to any delivery:

```bash
# List recent deliveries, newest first
curl http://localhost:4318/v1/webhook_deliveries \
  -H "Authorization: Bearer $STRATHON_API_KEY"

# Filter by status — "show me the failures"
curl "http://localhost:4318/v1/webhook_deliveries?status_filter=dlq" \
  -H "Authorization: Bearer $STRATHON_API_KEY"

# Narrow to one policy
curl "http://localhost:4318/v1/webhook_deliveries?policy_id=$POLICY_ID" \
  -H "Authorization: Bearer $STRATHON_API_KEY"

# Single delivery, includes payload
curl http://localhost:4318/v1/webhook_deliveries/$DELIVERY_ID \
  -H "Authorization: Bearer $STRATHON_API_KEY"
```

The list endpoint paginates with an opaque cursor: when there's a next
page, the response includes `next_cursor`; pass it back on the next
request via `?cursor=...`. Default page size 50, hard cap 200.

### Replaying failed deliveries

When a delivery sits in `dlq` (exhausted retries) or `abandoned` (4xx
or bad URL) and the operator wants to retry it — say the consumer was
down longer than the 24h retry window — POST replay:

```bash
curl -X POST http://localhost:4318/v1/webhook_deliveries/$DELIVERY_ID/replay \
  -H "Authorization: Bearer $STRATHON_API_KEY"
```

Returns 202 Accepted with the row reset to `pending`, `attempts=0`,
`last_error` cleared. A new Dramatiq message is dispatched after the
DB commit so the actor picks it up immediately. The replay is
asynchronous; the 202 is not evidence of consumer success — GET the
delivery again to check its new status.

Replay only works on `dlq` and `abandoned` deliveries. Replaying a
`succeeded` delivery returns 409 (re-delivering a success is a future
feature; for now operators reach into the policy and re-trigger via
their normal flow). Replaying a `pending` delivery returns 409 too —
the retry middleware is already going to fire.

### Sweeper

A background loop ("the sweeper") scans `pending` deliveries whose
`next_attempt_at` is older than a threshold (default 5 minutes). These
are deliveries whose Dramatiq message never landed — Redis was
unreachable during dispatch, the receiver crashed between row insert
and message send, the worker died before consuming the message. The
sweeper re-dispatches each one.

Configuration via environment variables (defaults shown):

| Variable                                  | Default | Purpose |
|-------------------------------------------|---------|---------|
| `STRATHON_WEBHOOK_SWEEPER_ENABLED`        | `true`  | Disable the loop entirely |
| `STRATHON_WEBHOOK_SWEEPER_INTERVAL_SEC`   | `60`    | Tick interval |
| `STRATHON_WEBHOOK_SWEEPER_THRESHOLD_SEC`  | `300`   | Orphan age before re-dispatch |
| `STRATHON_WEBHOOK_SWEEPER_BATCH`          | `100`   | Max rows per tick |

Re-dispatch is safe — the actor's first action is a status check on the
row, and a row that's no longer `pending` is a clean no-op. Aggressive
thresholds (small `THRESHOLD_SEC`) waste queue capacity on legitimate
in-flight messages; conservative thresholds extend the outage recovery
window.

### Metrics

Five Prometheus counters live at `/metrics` for webhook delivery
observability:

| Metric                                                  | Type    | Label    | What it counts |
|---------------------------------------------------------|---------|----------|----------------|
| `strathon_receiver_webhook_dispatched_total`            | Counter | —        | Rows enqueued via `enqueue_delivery` |
| `strathon_receiver_webhook_sends_total`                 | Counter | `outcome` | Actor invocations by outcome (succeeded/abandoned/failed_retrying/dlq) |
| `strathon_receiver_webhook_dlq_total`                   | Counter | —        | Deliveries that landed in DLQ |
| `strathon_receiver_webhook_sweeper_runs_total`          | Counter | —        | Sweeper ticks completed |
| `strathon_receiver_webhook_sweeper_reclaimed_total`     | Counter | —        | Orphan rows the sweeper re-dispatched |

The standard alerting target is `strathon_receiver_webhook_dlq_total` —
any non-zero increase means an alert hit DLQ and an operator should
investigate via the REST endpoints above.

### Required scopes (deliveries)

| Endpoint                                          | Scope                          |
|---------------------------------------------------|--------------------------------|
| `GET /v1/webhook_deliveries`                      | `webhook_deliveries:read`      |
| `GET /v1/webhook_deliveries/{id}`                 | `webhook_deliveries:read`      |
| `POST /v1/webhook_deliveries/{id}/replay`         | `webhook_deliveries:write`     |

## Scoping with `applies_to`

By default a policy is evaluated against every span. To scope a policy to
specific spans, set `applies_to` to a list of dot-segment-path tokens:

```json
{
  "name": "redact_pii_from_tool_calls",
  "match_expression": "attrs[\"strathon.tool.args\"].contains(\"@\")",
  "action": "steer",
  "applies_to": ["langgraph.tool", "crewai.tool"]
}
```

A token matches a span name if and only if it aligns with one or more
whole dot-separated segments of the name. So `"tool"` matches
`"langgraph.tool.send_email"` because `tool` is one of the segments,
but does **not** match `"langgraph.pool.X"` — the substring `tool`
appearing inside `pool` is not a segment-aligned match. Multi-segment
tokens work too: `"langgraph.tool"` matches LangGraph tool spans but
not CrewAI tool spans.

The list is OR'd: a span matches the filter if any token aligns.
Empty list (the default) means "every span." The same rule runs on
both the server (at ingest time, gating `policy_matches` rows) and
in the SDK (gating in-process block/steer enforcement) so the two
layers always agree on which spans a policy applies to.

## Creating a policy

```bash
curl -X POST http://localhost:4318/v1/policies \
  -H "Authorization: Bearer $STRATHON_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "block_competitor_email",
    "description": "Prevent agents from emailing competitor addresses",
    "match_expression": "attrs[\"gen_ai.tool.name\"] == \"send_email\" && attrs[\"strathon.tool.args\"].contains(\"@competitor.com\")",
    "action": "block",
    "action_config": {"message": "Cannot email a competitor address."},
    "applies_to": ["langgraph.tool", "crewai.tool", "agents.tool"],
    "priority": 100
  }'
```

## Framework support

Strathon enforces policies at the tool-call boundary on every supported
framework. Block enforcement is zero-code-change: `instrument(client)` is
all the user does. Steer enforcement requires one extra line per tool
(or per agent) — replacing a tool's return value is a bigger contract
change than refusing to call, so we ask the user to opt in explicitly.

| Framework            | Block (auto)        | Steer            | Steer opt-in call (callback frameworks only)            |
|----------------------|---------------------|------------------|---------------------------------------------------------|
| LangGraph (LangChain)| `instrument(client)`| Per-tool opt-in  | `from strathon.policy import enforce_steer; enforce_steer(tool, client)` |
| CrewAI               | `instrument(client)`| Auto (per-tool optional) | `enforce_steer(tool, client)` (optional; class patch already covers steer) |
| OpenAI Agents SDK    | `instrument(client)`| Per-agent opt-in | `from strathon.instrumentation.openai_agents import attach_strathon_guardrails; attach_strathon_guardrails(agent, client)` |
| Pydantic AI          | `instrument(client)`| Auto             | not needed — plugin substitutes the tool result directly |
| Google ADK           | `instrument(client)`| Auto             | not needed — plugin short-circuits with the replacement |
| AutoGen              | `instrument(client)`| Auto             | not needed — plugin substitutes the tool result directly |
| Claude Agent SDK     | `instrument(client)`| Auto             | not needed — plugin substitutes the tool result directly |

Why the difference: the plugin-based frameworks (Pydantic AI, Google ADK,
AutoGen, Claude Agent SDK) expose a hook that can replace a tool's return
value, so block, throttle, and steer all work with just `instrument(client)`.
The callback-based frameworks (LangGraph/LangChain) can hard-*block* from the
callback but cannot substitute a return value there, so full steer (returning a
replacement in place of the tool body) needs the one-line per-tool
`enforce_steer` opt-in. CrewAI's class patch sits at a boundary that covers
both, so its per-tool call is optional.

The raw model-SDK integrations (OpenAI, Anthropic, LangChain core) are
**observe-only**: they emit spans for visibility but do not enforce, because at
the raw model-call layer there is no tool call to intercept. Enforcement
happens at the tool-call boundary on the agent frameworks above. If you drive
tools yourself on top of a raw model SDK, add enforcement at your own tool
dispatch.

## Enforcing in your agent code

For most framework integrations, just instrument the client. The SDK pulls
policies from the receiver every 30 seconds in the background:

```python
from strathon import Client
from strathon.instrumentation.langgraph import instrument

client = Client(api_key="...", endpoint="http://localhost:4318")
handler = instrument(client)

# Use the handler in your graph invocations
graph.invoke(input, config={"callbacks": [handler]})
# Tool calls that match a block policy raise StrathonPolicyBlocked.
```

For custom tools or non-instrumented call paths, call `check_policy` directly:

```python
from strathon.policy import StrathonPolicyBlocked

decision = client.check_policy({
    "name": "myapp.action.send_money",
    "attrs": {"amount": 50000, "destination": "..."},
})
if decision.is_block:
    raise StrathonPolicyBlocked(decision.message)
if decision.is_steer:
    return decision.replacement
```

## CRUD endpoints

| Method | Path                       | Purpose                                  |
|--------|----------------------------|------------------------------------------|
| GET    | /v1/policies               | List policies (SDKs poll this every 30s) |
| POST   | /v1/policies               | Create a policy                          |
| GET    | /v1/policies/{id}          | Read one                                 |
| PATCH  | /v1/policies/{id}          | Partial update (enable/disable, change action, etc.) |
| DELETE | /v1/policies/{id}          | Delete                                   |

## Audit trail

Every match is recorded in the `policy_matches` table with the policy id,
trace id, span id, action, outcome, and timestamp. Query it directly in
Postgres or expose it through the dashboard.

[cel]: https://cel.dev/
