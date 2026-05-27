<div align="center">
  <img src="https://raw.githubusercontent.com/strathon/strathon/main/assets/banner.png" alt="Strathon" width="600" />

  <p><strong>Open-source AI agent firewall</strong></p>
  <p>Runtime security, CEL policies, and EU AI Act compliance for LLM agents.</p>

  <div>
    <a href="https://getstrathon.com"><strong>Website</strong></a> ·
    <a href="https://getstrathon.com/docs"><strong>Docs</strong></a> ·
    <a href="https://getstrathon.com/docs/quickstart"><strong>Quickstart</strong></a> ·
    <a href="https://github.com/strathon/strathon/issues"><strong>Report Bug</strong></a> ·
    <a href="https://discord.gg/Ta9XRmh4H"><strong>Discord</strong></a>
  </div>
  <br/>

  <a href="https://pypi.org/project/strathon"><img src="https://img.shields.io/pypi/v/strathon?color=blue&logo=python&logoColor=white" alt="PyPI"></a>
  <a href="https://github.com/strathon/strathon/actions/workflows/ci.yml"><img src="https://github.com/strathon/strathon/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/strathon/strathon/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-MIT%20%2F%20Apache%202.0-blue.svg" alt="License"></a>
  <a href="https://discord.gg/Ta9XRmh4H"><img src="https://img.shields.io/discord/1234567890?logo=discord&labelColor=%235462eb&logoColor=white&color=%235462eb" alt="Discord"></a>
  <a href="https://twitter.com/strathonai"><img src="https://img.shields.io/twitter/follow/strathonai?logo=X&color=%23f5f5f5" alt="X"></a>
</div>

<br/>

Strathon is an **open-source firewall for AI agents**. Write a CEL rule, and Strathon blocks the tool call before it executes. Self-host in minutes with Docker Compose. 1,000+ tests, 10 framework integrations, EU AI Act compliance built in.

## Quickstart

```bash
pip install strathon
```

```python
from strathon import Client, instrument

client = Client(api_key="stra_...", endpoint="http://localhost:4318")
instrument(client, frameworks=["openai"])

# When your agent calls send_email(to="sales@competitor.com")
# → StrathonPolicyBlocked is raised
# → The function body never executes
```

That's it. Three lines. Your agent is protected.

## Self-Host

```bash
git clone https://github.com/strathon/strathon.git
cd strathon
docker compose up
```

Opens at `http://localhost:3000` (dashboard) and `http://localhost:4318` (receiver API).

Register the first account, create a policy, get an API key, and connect your agent. No email server needed. No external dependencies beyond PostgreSQL (included in Docker Compose).

## Core Features

**Policy Engine** — Write rules in [CEL](https://cel.dev) (Common Expression Language, used by Kubernetes and Firebase). Six enforcement actions: block, steer, throttle, log, alert, require_approval. 12 OWASP-mapped templates for one-click setup. Shadow mode for safe testing.

**Human Approval Workflows** — Pause agent execution until an operator approves or denies. Multi-party approval (N-of-M). Automatic expiry and escalation.

**Dashboard** — Next.js operator UI with trace waterfall, policy editor, approval cards, agent risk scoring, audit log with hash verification, budget charts, and compliance export. BFF security proxy with httpOnly cookies.

**50+ Credential Patterns** — Detects AWS keys, GCP service accounts, GitHub tokens, Stripe keys, database URIs, private keys, JWTs, and more. Scans tool arguments and responses at ingest.

**EU AI Act Compliance** — Evidence export for Articles 9-15 and 19. Agent inventory with NIST AI RMF risk scoring. Incident detection with Article 73 reporting metadata.

**Behavioral Drift Detection** — EWMA/CUSUM statistical analysis per agent. Auto-calibrates from observations. Fires alerts when agent behavior shifts.

**Circuit Breakers** — Per-agent and per-tool automatic trip/half-open/reset. Contains blast radius without operator intervention.

**MCP Security Gateway** — Proxy between agents and MCP servers. Policy evaluation and credential scanning on every request and response.

**Tamper-Evident Audit Log** — HMAC-SHA256 hash chain on every operator action. Merkle root anchoring. Immutable at the database level.

## Framework Integrations

```bash
pip install strathon                     # core
pip install strathon[openai-agents]      # + OpenAI Agents SDK
pip install strathon[all]                # all frameworks
```

| Framework | Integration |
|-----------|------------|
| **LangGraph** | LangChain BaseCallbackHandler |
| **CrewAI** | Event listener on CrewAI event bus |
| **OpenAI Agents SDK** | TracingProcessor extension point |
| **OpenAI** | Drop-in wrapper on `chat.completions.create` |
| **Anthropic** | Drop-in wrapper on `messages.create` |
| **LangChain** | BaseCallbackHandler |
| **AutoGen** | Wrapper on `BaseChatAgent.on_messages` |
| **Claude Agent SDK** | Wrapper on `query()` |
| **Pydantic AI** | AbstractCapability plugin |
| **Google ADK** | BasePlugin |

All integrations use first-class framework plugin systems. Zero monkey-patching where possible.

## Performance

| Metric | Value |
|--------|-------|
| Throughput | **2,080 spans/sec** (single instance) |
| Latency p50 | 214ms |
| Latency p95 | 663ms |
| Error rate | 0% |

Single instance handles **180M spans/day**. Supports ~2,500 concurrent agents at 50 tool calls/min. Full pipeline per span: protobuf parse, CEL policy evaluation, 50+ credential patterns, PII redaction, circuit breaker check, batch PostgreSQL write.

Tested on MacBook Pro M-series, 4 uvicorn workers, PostgreSQL 16, 50,000 spans.

## OWASP Agentic Security Coverage

Strathon's threat model is anchored on the [OWASP Top 10 for Agentic AI](https://genai.owasp.org/resource/agentic-security-threats-and-mitigations/).

| Threat | Strathon |
|--------|----------|
| ASI-01 Prompt injection | CEL policies on input content patterns |
| ASI-02 Tool misuse | Block/allow-list on tool names and arguments |
| ASI-03 Insecure output | CEL policies on output + 50+ credential patterns |
| ASI-04 Reasoning manipulation | Tamper-evident audit log |
| ASI-05 Memory poisoning | Halt propagation + behavioral drift detection |
| ASI-06 Excessive agency | Cost and iteration budgets with auto-halt |
| ASI-07 Insufficient monitoring | Trace search, audit log, webhook alerts, dashboard |
| ASI-09 Identity spoofing | Scoped API keys, per-key rate limits, MFA |
| ASI-10 Overwhelming HITL | Multi-party approval, circuit breakers, kill switches |

## Architecture

```
Your Agent                        Strathon
┌─────────────────┐              ┌─────────────────┐        ┌───────────┐
│                 │   OTLP/HTTP  │    Receiver      │        │           │
│  Agent code     │─────────────▶│    (FastAPI)      │───────▶│ PostgreSQL│
│                 │              │                   │        │           │
│  ┌───────────┐  │◀─────────────│  Policy eval      │        └───────────┘
│  │Strathon   │  │  block/allow │  Credential scan  │
│  │SDK        │  │              │  PII redaction    │        ┌───────────┐
│  │(3 lines)  │  │              │  Audit log        │───────▶│ Dashboard │
│  └───────────┘  │              │  Webhooks         │        │ (Next.js) │
└─────────────────┘              └─────────────────┘        └───────────┘
```

Single PostgreSQL dependency. No Redis, no ClickHouse, no S3. Self-host on one machine or scale horizontally behind a load balancer.

## CLI

```bash
pip install strathon-cli
```

```bash
strathon policies list
strathon policies create --name "block-email" --expr 'attrs["gen_ai.tool.name"] == "send_email"' --action block
strathon traces list --last 1h
strathon halts create --scope project --reason "Emergency"
strathon admin list-users
```

13 command groups, 30+ subcommands. `--json` flag for scripting.

## Deploy

**Self-hosted** (recommended for getting started):
```bash
docker compose up
```

**Cloud** (managed, coming soon):
Sign up at [getstrathon.com](https://getstrathon.com) for managed Strathon with automatic updates, backups, and support.

## Community

- [Discord](https://discord.gg/Ta9XRmh4H) — questions, discussion, support
- [GitHub Issues](https://github.com/strathon/strathon/issues) — bug reports
- [GitHub Discussions](https://github.com/strathon/strathon/discussions) — feature requests, ideas
- [Contributing](CONTRIBUTING.md) — setup guide for development

## License

- **SDK** (`sdk/`) — Apache License 2.0
- **Receiver** (`receiver/`) — MIT License

See [LICENSING.md](LICENSING.md) for details.
