# Strathon

[![CI](https://github.com/strathon/strathon/actions/workflows/ci.yml/badge.svg)](https://github.com/strathon/strathon/actions/workflows/ci.yml)
[![License: MIT (receiver)](https://img.shields.io/badge/receiver-MIT-blue.svg)](receiver/LICENSE)
[![License: Apache 2.0 (sdk)](https://img.shields.io/badge/sdk-Apache%202.0-blue.svg)](sdk/LICENSE)

**An open-source firewall for AI agents.** Write a rule once, and Strathon
blocks the tool call before it runs. Supports LangGraph, CrewAI, OpenAI
Agents SDK, OpenAI, Anthropic, LangChain, AutoGen, and the Claude Agent SDK.

---

## Why Strathon

An agent is about to email a competitor. Your customer-support bot is about
to refund 100% of a transaction. A research agent is about to fetch an
internal URL. You don't want to find out after it happened.

Strathon is a rule engine that sits at the tool-call boundary in your
agent's process. You write rules in CEL (the same expression language
Kubernetes admission controllers use) and Strathon evaluates them before
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
from strathon import Client, instrument

client = Client(api_key="stra_…", endpoint="http://localhost:4318")
instrument(client, frameworks=["openai"])

# When the agent calls send_email(to="sales@competitor.com", ...)
# → strathon.policy.StrathonPolicyBlocked is raised
# → send_email's function body never executes
```

No proxy in front of your LLM provider, no separate Kubernetes sidecar,
no DSL beyond CEL. Self-host it on one container.

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
============================================================
```

Then in another terminal:

```bash
pip install strathon langchain-core cel-python
python examples/intervention_demo.py
```

The demo creates the policy above, calls `send_email` twice (once to a
competitor, once to an internal address), and shows you exactly one email
making it through. The competitor email is blocked before its function
body runs.

Configuration lives in `.env.example`. Copy it to `.env` and edit what
you need.

---

## What's built

Everything below is end-to-end, tested in CI (960+ tests across
receiver, SDK, and integration suites), and ready to use.

### Policy engine

CEL expressions over span attributes plus a bound `now` timestamp
for time-based rules. Five enforcement actions: **block** (raise
before the tool body), **steer** (replace tool output with a
corrective string), **throttle** (token-bucket rate limit per
policy-agent pair), **log** (record a match without interrupting),
**alert** (fire a webhook). **Allow-list mode** inverts the default:
deny everything unless an explicit `allow` policy admits it.

Policies support `applies_to` path filters for restricting which
span types they evaluate against. Compile-time CEL syntax checking
on `POST /v1/policies`.

### Policy dry-run simulation

`POST /v1/policies/simulate` evaluates a CEL expression against
historical spans without enabling the policy. Returns matched spans,
match rate, and timing. Lets operators test a policy before deploying it.

### Framework instrumentation

The SDK auto-instruments 8 frameworks:

| Framework | Integration style |
|-----------|-------------------|
| LangGraph | LangChain BaseCallbackHandler |
| CrewAI | Event listener on the CrewAI event bus |
| OpenAI Agents SDK | TracingProcessor on the SDK's extension point |
| OpenAI | Monkey-patch on `chat.completions.create` (sync + async + streaming) |
| Anthropic | Monkey-patch on `messages.create` (sync + async + SSE streaming) |
| LangChain | Delegates to the LangGraph handler |
| AutoGen | Monkey-patch on `BaseChatAgent.on_messages` + `BaseGroupChat.run` |
| Claude Agent SDK | Wraps `query()` and `ClaudeSDKClient.query()` |

All modules capture `gen_ai.*` and `strathon.agent.*` attributes,
return `False` gracefully when the framework isn't installed, and
are idempotent.

### Span search

`GET /v1/spans` with time-range, denormalized column filters
(agent_name, tool_name, model, kind, status, intervention_state,
12 columns total), and JSONB attribute containment via the `attr.*`
query-param prefix (backed by a GIN index). Keyset cursor pagination.
`GET /v1/spans/{trace_id}/{span_id}` for detail with events and links.

### Partitioned storage

`spans`, `span_events`, and `span_links` are RANGE-partitioned on
`start_time_unix_nano` (monthly). Composite PK
`(start_time_unix_nano, trace_id, span_id)`. Co-partitioned children
with composite FK. No default partition. A background worker premakes
3 months of partitions and drops those older than 12 months,
advisory-lock-guarded for multi-replica safety.

### Tamper-evident audit log

Every operator mutation (policy CRUD, halt toggle, budget change,
API key rotation, webhook config, project settings) is recorded in
an append-only, HMAC-SHA256 hash-chained audit log. SCIM 2.0 filter
queries, per-minute Merkle root anchors, three scopes
(`audit:read`, `audit:write`, `audit:admin`).

### Operator controls

**Kill-switches**: `POST /v1/halts` writes a halt the SDK observes
within one poll cycle and raises `StrathonHaltExceeded`. Project-scope
or agent-scope. No agent restart needed.

**Cost and iteration budgets**: per-project caps on USD spend or
tool-call count. The receiver's budget monitor writes an auto-clearing
halt when a threshold is crossed.

### Safety and compliance

**PII redaction at ingest**: default-on, covering emails, credit cards
(Luhn-validated), SSNs, phone numbers, IPs, and API-key shapes. Policy
expressions evaluate against unredacted data so firewall semantics are
preserved.

**Durable webhook delivery**: HMAC-SHA256 signed, retried with
exponential backoff, dead-lettered, replayable via REST.

**Capability-scoped API keys**: per-key `scopes TEXT[]`. A leaked SDK
key can ingest but can't rotate keys or change policies.

**Per-key rate limiting**: in-memory token bucket, 100 req/s sustained /
200 burst, tunable via env vars.

**Fail-closed SDK mode**: `Client(fail_closed=True)` raises
`StrathonReceiverUnreachable` when cached state exceeds the configured
staleness threshold.

### Infrastructure

Single Postgres dependency. No Redis, no ClickHouse, no S3, no message
broker. Alembic-managed schema with auto-migrate on startup.

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
│  before tool     │                          │  (partitioned│
│  body runs       │                          │   spans)     │
└──────────────────┘                          └──────────────┘
```

Two attribute namespaces on every span:

- `gen_ai.*`: OpenTelemetry GenAI semantic conventions
- `strathon.*`: Strathon-specific extensions for tool arguments,
  agent topology, and policy decisions

Detailed docs: [intervention](docs/intervention.md),
[analytics](docs/analytics.md),
[budgets](docs/budgets.md), [audit](docs/audit.md),
[spans](docs/spans.md), [projects](docs/projects.md),
[redaction](docs/redaction.md),
[observability](docs/observability.md), [retention](docs/retention.md),
[sampling](docs/sampling.md), [self-hosting](docs/self-hosting.md),
[api keys](docs/api_keys.md).

---

## OWASP Agentic Security Top 10

Strathon's threat model is anchored on the
[OWASP Agentic Security Threats](https://genai.owasp.org/resource/agentic-security-threats-and-mitigations/).

| Threat | Strathon primitive |
|--------|--------------------|
| ASI-01 Prompt injection | CEL policies on input content patterns |
| ASI-02 Tool misuse | Block/allow-list on `gen_ai.tool.name` |
| ASI-03 Insecure output | CEL policies on output attributes |
| ASI-04 Reasoning manipulation | Audit log captures all policy changes |
| ASI-05 Memory poisoning | Halt propagation on suspicious patterns |
| ASI-06 Excessive agency | Cost and iteration budgets |
| ASI-07 Insufficient monitoring | Span search, audit log, webhook alerts |
| ASI-09 Identity spoofing | Capability-scoped API keys, per-key rate limits |
| ASI-10 Overwhelming HITL | Audit-of-audit, operator kill-switches |

---

## Status

Active development toward v1.0. The policy engine, SDK instrumentation,
span search, audit log, partitioned storage, and operator controls are
stable and covered by 960+ tests that run in CI on every push.

If you find something broken or want to discuss a feature, open an issue.

## License

Two licenses, by component:

- **SDK** (`sdk/`) — Apache License 2.0. Patent grant included.
- **Receiver** (`receiver/`) — MIT. Maximum permissiveness for self-hosting.

See [`LICENSING.md`](LICENSING.md) for the rationale.
