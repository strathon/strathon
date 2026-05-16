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

These attributes are set consistently across all three framework integrations
(LangGraph, CrewAI, OpenAI Agents SDK), so policies written against them are
portable:

| Attribute                       | Description                                  |
|---------------------------------|----------------------------------------------|
| `strathon.framework`            | One of `langgraph`, `crewai`, `agents`       |
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

A policy has one of four actions:

| Action  | What happens                                                                                                  | Where it runs |
|---------|---------------------------------------------------------------------------------------------------------------|---------------|
| `log`   | Annotate the matching span with `strathon.policy.*` attributes. Passive.                                      | Server        |
| `alert` | Fire a signed webhook (`action_config.webhook_url`). Durable, retried with exponential backoff, dead-lettered after exhaustion. | Server        |
| `block` | SDK raises `StrathonPolicyBlocked` before the tool/LLM call executes. Agent sees an error and adapts.         | SDK (client)  |
| `steer` | SDK returns a corrective string (`action_config.replacement`) in place of real output. Agent self-corrects.   | SDK (client)  |

`block` and `steer` actually prevent the action — these are SDK-side because
by the time a span reaches the server, the action has already happened.

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

| Framework            | Block (auto)        | Steer (opt-in) | Steer opt-in call                                       |
|----------------------|---------------------|----------------|---------------------------------------------------------|
| LangGraph (LangChain)| `instrument(client)`| Per-tool       | `from strathon.policy import enforce_steer; enforce_steer(tool, client)` |
| CrewAI               | `instrument(client)`| Per-tool       | `enforce_steer(tool, client)` (same helper)             |
| OpenAI Agents SDK    | `instrument(client)`| Per-agent      | `from strathon.instrumentation.openai_agents import attach_strathon_guardrails; attach_strathon_guardrails(agent, client)` |

CrewAI's `instrument(client)` already enforces *both* block and steer
globally (its class patch sits at the right boundary for both), so the
per-tool `enforce_steer` call on CrewAI is optional — it's there for
parity with LangGraph and for users who want explicit per-tool control.

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
