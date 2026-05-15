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
- **Log and alert actions.** A matched policy records a match record and
  fires an outbound webhook without interrupting the agent.
- **CEL expressions.** Full [CEL](https://github.com/google/cel-spec)
  evaluator (via `cel-python`) over the span's attributes. Compile-time
  syntax checking on `POST /v1/policies`.
- **OTLP/HTTP trace ingest.** Standard OpenTelemetry exporter. Spans land
  in Postgres with the `gen_ai.*` semconv attributes denormalized for
  query speed.
- **Capability-scoped API keys.** Per-key `scopes TEXT[]`; endpoints
  declare what they need (`traces:write`, `policies:read`, etc.). A leaked
  SDK key can ingest but can't rotate keys or rewrite rules.
- **Single Postgres dependency.** No Redis, no ClickHouse, no S3, no
  message broker.
- **Alembic-managed schema** with auto-migrate on receiver startup. Set
  `STRATHON_AUTO_MIGRATE=false` if you'd rather run migrations as a
  separate deploy step.

## On the roadmap

The bits below have scaffolding but aren't production-ready yet. I'm
listing them here so you know what's coming and so the README never
overpromises:

- **Steer actions** (rewrite the model's response before the agent sees
  it) — partial, framework parity work in progress.
- **PII redaction** — interface in place, real redactor still stubbed.
- **Dashboard** — Next.js scaffold under `dashboard/` is empty. Until it
  ships, all policy management is via the REST API.
- **Persistent halt state and budget rollup across processes** — the
  earlier SDK-pull design left endpoint stubs at `/v1/intervention/*`
  for backward compatibility. The replacement design (server-side state
  + SDK enforcement at tool boundaries) is partway in.
- **Release pipeline** — no PyPI publish or pinned Docker tags yet.
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
[observability](docs/observability.md), [retention](docs/retention.md),
[sampling](docs/sampling.md), [self-hosting](docs/self-hosting.md),
[api keys](docs/api_keys.md).

---

## Status

v0 in active development. Target v1.0: end of June 2026. The block / log /
alert path is stable and used in the integration test suite that runs in
CI on every push.

If you find something broken or want to discuss a feature, open an issue.

## License

Two licenses, by component:

- **SDK** (`sdk/`) — Apache License 2.0. Patent grant included.
- **Receiver** (`receiver/`) — MIT. Maximum permissiveness for self-hosting.

See [`LICENSING.md`](LICENSING.md) for the rationale.
