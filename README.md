# Strathon

[![CI](https://github.com/strathon/strathon/actions/workflows/ci.yml/badge.svg)](https://github.com/strathon/strathon/actions/workflows/ci.yml)
[![License: MIT (receiver)](https://img.shields.io/badge/receiver-MIT-blue.svg)](receiver/LICENSE)
[![License: Apache 2.0 (sdk)](https://img.shields.io/badge/sdk-Apache%202.0-blue.svg)](sdk/LICENSE)

**An open-source firewall for AI agents.** Write a rule once, and Strathon
blocks the tool call before it runs — in LangGraph, CrewAI, or the OpenAI
Agents SDK.

---

## Why Strathon

An agent is about to email a competitor. Your customer-support bot is about
to refund 100% of a transaction. A research agent is about to fetch an
internal URL. You don't want to find out after it happened.

Strathon is a rule engine that sits at the tool-call boundary in your
agent's process. You write rules in CEL — the same expression language
Kubernetes admission controllers use — and Strathon evaluates them before
the tool body runs:

```http
POST /v1/policies
Content-Type: application/json
Authorization: Bearer stra_…

{
  "name":   "no_competitor_email",
  "action": "block",
  "match_expression":
    "attrs[\"gen_ai.tool.name\"] == \"send_email\" && attrs[\"strathon.tool.args\"].contains(\"@competitor.com\")"
}
```

```python
# Your agent code is unchanged. Strathon's instrumentation hooks the
# framework's tool-call lifecycle and raises before the function runs.
from strathon import Client
from strathon.instrumentation.langgraph import instrument

client = Client(api_key="stra_…", endpoint="http://localhost:4318")
instrument(client)

# When the agent calls send_email(to="sales@competitor.com", ...)
# → strathon.policy.StrathonPolicyBlocked is raised
# → send_email's function body never executes
```

That's the whole product loop. The receiver stores the policies, the SDK
pulls them, and the framework instrumentation enforces them. No proxy in
front of your LLM provider, no separate Kubernetes sidecar, no DSL beyond
CEL. Self-host it on one container.

---

## Quick start

Requires Docker.

```bash
git clone https://github.com/strathon/strathon.git
cd strathon
docker compose up
```

On first run the receiver applies its migrations, seeds a development API
key, and prints a banner:

```
============================================================
  Strathon receiver ready
============================================================
  Endpoint:   http://localhost:4318
  Dev API key (rotate before production!):
      stra_dev_local_default_project_do_not_use_in_production

  Quick test:
      curl -H "Authorization: Bearer stra_dev_…" \
           http://localhost:4318/v1/policies

  Run the intervention demo:
      python examples/intervention_demo.py
============================================================
```

Then in another terminal:

```bash
pip install strathon langchain-core cel-python
python examples/intervention_demo.py
```

The demo installs the policy above, calls `send_email` twice (once to a
competitor, once to an internal address), and shows you exactly one email
making it through. The competitor email is blocked before its function body
runs.

Configuration lives in `.env.example`: Postgres password, log level, log
format, sampling rate, retention. Copy it to `.env` and edit what you need.

---

## What's built today

The bits below are end-to-end, tested in CI, and ready to use:

- **Block actions.** A matched policy raises `StrathonPolicyBlocked` before
  the tool body runs. Works in LangGraph (via LangChain's tool callback
  protocol), CrewAI, and the OpenAI Agents SDK via per-framework
  instrumentation modules.
- **Steer actions.** A matched policy returns a corrective string in
  place of the real tool output, so the agent self-corrects on its next
  step instead of seeing an error. Same framework coverage as block.
- **Throttle actions.** A matched policy enforces a per-`(policy,
  agent)` (or `global`) token bucket; calls under the cap proceed,
  calls over it raise `StrathonPolicyThrottled` with
  `retry_after_seconds` so caller code can back off and retry.
  Configure with `action_config: {max_calls, window_seconds, scope}`.
- **Allow-list mode.** Per-project `intervention_default_action`
  toggle. Default `allow` keeps the historical permissive posture;
  set `block` and the SDK denies any tool call that no `allow` policy
  explicitly admits. Toggle via `PATCH /v1/project/settings`. Priority
  ordering is preserved across action types — a higher-priority block
  still beats a lower-priority allow.
- **Log and alert actions.** A matched policy records a match record and
  fires an outbound webhook without interrupting the agent.
- **Durable webhook delivery.** Alert webhooks are signed (HMAC-SHA256),
  retried with exponential backoff, dead-lettered after exhaustion, and
  replayable via REST. A background sweeper recovers anything the
  in-process queue dropped during a crash.
- **Operator kill-switches.** `POST /v1/halts` writes a halt that the
  SDK observes within one poll cycle (~1s) and raises
  `StrathonHaltExceeded` at the tool boundary. Project-scope or
  agent-scope. No agent restart needed. Clears via `DELETE`.
  See [`docs/intervention.md`](docs/intervention.md).
- **Cost and iteration budgets.** Per-project caps on USD spend (with
  fixed-window reset like `1d` / `30d`) or on tool-call count (rolling
  window for loop detection). The receiver's budget monitor evaluates
  active budgets every few seconds and writes an auto-clearing halt
  when a threshold is crossed. Pricing comes from a vendored model
  catalog (LiteLLM upstream, MIT-licensed) plus per-project overrides.
  See [`docs/budgets.md`](docs/budgets.md).
- **PII redaction at ingest.** Default-on regex-based redaction of
  emails, credit cards (Luhn-validated), SSNs, phone numbers, IP
  addresses, and common API-key shapes (`sk-...`, `sk_live_...`,
  `ghp_...`, JWTs, etc.). Per-entity actions (redact / mask / hash /
  delete), key-level rules, and allowlist mode are all configurable
  per project. Critically: policy match expressions evaluate against
  the unredacted span so the firewall semantics are preserved.
  See [`docs/redaction.md`](docs/redaction.md).
- **CEL expressions.** Full [CEL](https://github.com/google/cel-spec)
  evaluator (via `cel-python`) over the span's attributes plus a
  bound `now` timestamp for time-based rules (`now.getDayOfWeek()`,
  `now.getHours("America/Los_Angeles")`, timestamp/duration
  arithmetic). Compile-time syntax checking on `POST /v1/policies`.
  Same expression surface gcloud IAM, Envoy, and Kubernetes
  admission use.
  evaluator (via `cel-python`) over the span's attributes. Compile-time
  syntax checking on `POST /v1/policies`.
- **OTLP/HTTP trace ingest.** Standard OpenTelemetry exporter. Spans land
  in Postgres with the `gen_ai.*` semconv attributes denormalized for
  query speed.
- **Capability-scoped API keys.** Per-key `scopes TEXT[]`; endpoints
  declare what they need (`traces:write`, `policies:read`, `halts:write`,
  `budgets:write`, etc.). A leaked SDK key can ingest but can't rotate
  keys, rewrite rules, or change budgets.
- **Per-key rate limiting.** In-memory token bucket on every non-probe
  endpoint, 100 req/s sustained / 200 burst by default, tunable via
  `STRATHON_RATE_LIMIT_*` env vars. Identified by the `Authorization`
  header for authenticated requests, client IP otherwise. Returns
  `429` with `Retry-After` and `X-RateLimit-*` headers. State is
  per-process — a multi-replica deploy multiplies the effective ceiling
  by the replica count. This matches Sentry's self-hosted posture
  (their internal limiter is per-process; they recommend a reverse-proxy
  limit for production). For hard global limits in front of an
  N-replica Strathon, put nginx / Traefik / HAProxy in front. See
  [`docs/self-hosting.md`](docs/self-hosting.md).
- **Fail-closed SDK mode.** Default is fail-open (last-known halts and
  policies stay in force during a receiver outage). Operators who
  prefer safer-but-noisier semantics can pass `Client(fail_closed=True)`
  to make the SDK raise `StrathonReceiverUnreachable` at tool
  boundaries whenever the cached intervention state is older than the
  configured threshold.
- **Single Postgres dependency.** No Redis, no ClickHouse, no S3, no
  message broker.
- **Alembic-managed schema** with auto-migrate on receiver startup. Set
  `STRATHON_AUTO_MIGRATE=false` if you'd rather run migrations as a
  separate deploy step.

## On the roadmap

The bits below are still in flight on the way to v1.0:

- **Dashboard.** Next.js scaffold under `dashboard/` is empty. Until it
  ships, all configuration is via the REST API.
- **Release pipeline.** No PyPI publish or pinned Docker tags yet.
  Install from a `git clone` for now.

---

## Architecture

```
┌──────────────────┐    OTLP/HTTP traces     ┌──────────────────┐
│   Your agent     │ ──────────────────────► │     Receiver     │
│                  │                         │                  │
│ ┌──────────────┐ │ ◄────────────────────── │   (FastAPI +     │
│ │ Strathon SDK │ │   policy fetch (REST)   │    SQLAlchemy)   │
│ │ instruments  │ │                         │                  │
│ │ tool calls   │ │                         └────────┬─────────┘
│ └──────────────┘ │                                  │
│        │         │                                  │
│        ▼         │                                  ▼
│  StrathonPolicy  │                          ┌──────────────┐
│  Blocked raised  │                          │   Postgres   │
│  before tool     │                          │              │
│  body runs       │                          └──────────────┘
└──────────────────┘
```

Two attribute namespaces on every span:

- `gen_ai.*` — OpenTelemetry GenAI semantic conventions
  (`gen_ai.tool.name`, `gen_ai.usage.input_tokens`, etc.)
- `strathon.*` — Strathon-specific extensions for tool arguments,
  agent topology, and policy decisions.

Detailed docs live under `docs/`: [intervention](docs/intervention.md),
[budgets](docs/budgets.md), [redaction](docs/redaction.md),
[observability](docs/observability.md), [retention](docs/retention.md),
[sampling](docs/sampling.md), [self-hosting](docs/self-hosting.md),
[api keys](docs/api_keys.md).

---

## Status

v0 in active development. Target v1.0: end of June 2026. The
block / steer / log / alert / halt / budget paths are stable and
covered by the integration test suite that runs in CI on every push.

If you find something broken or want to discuss a feature, open an issue.

## License

Two licenses, by component:

- **SDK** (`sdk/`) — Apache License 2.0. Patent grant included.
- **Receiver** (`receiver/`) — MIT. Maximum permissiveness for self-hosting.

See [`LICENSING.md`](LICENSING.md) for the rationale.
