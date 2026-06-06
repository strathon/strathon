<div align="center">
  <img src="https://raw.githubusercontent.com/strathon/strathon/main/assets/banner.png" alt="Strathon" width="600" />

  <p><strong>Open-source AI agent firewall</strong></p>
  <p>Open-source AI agent firewall. CEL policies, runtime enforcement at the tool-call boundary, and EU AI Act compliance.</p>

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
  <a href="https://discord.gg/Ta9XRmh4H"><img src="https://img.shields.io/badge/Discord-5865F2?logo=discord&logoColor=white" alt="Discord"></a>
  <a href="https://twitter.com/strathonai"><img src="https://img.shields.io/twitter/follow/strathonai?logo=X&color=%23f5f5f5" alt="X"></a>
  <a href="https://www.linkedin.com/company/strathonai"><img src="https://custom-icon-badges.demolab.com/badge/LinkedIn-0A66C2?logo=linkedin-white&logoColor=fff" alt="LinkedIn"></a>
  <br/>
  <a href="https://github.com/strathon/strathon/graphs/commit-activity"><img alt="Commits last month" src="https://img.shields.io/github/commit-activity/m/strathon/strathon?labelColor=%2332b583&color=%2312b76a" /></a>
</div>

<br/>

Strathon is an **open-source firewall for AI agents**. Write a [CEL](https://cel.dev) policy, and Strathon blocks the tool call **before** it executes — no gateway proxy, no latency tax, enforcement happens inside your agent process. Self-host in minutes with Docker Compose. 1,000+ tests, 10 framework integrations, EU AI Act compliance.

Other agent observability tools tell you what went wrong after the fact. Enterprise SaaS products block in real time but are closed-source and gateway-layer. **Strathon is the only project that is open-source, framework-native, and actually blocks.**

## Quickstart

### 1. Start the server

```bash
git clone https://github.com/strathon/strathon.git
cd strathon && docker compose up -d
```

Dashboard opens at `http://localhost:3000`, receiver API at `http://localhost:4318`.

### 2. Install the SDK

```bash
pip install strathon
```

### 3. Create a policy and connect your agent

```python
from strathon import Client, instrument

client = Client(
    api_key="stra_...",         # from dashboard → Settings → API Keys
    endpoint="http://localhost:4318",
)
instrument(client, frameworks=["langgraph"])

# Your existing LangGraph agent — no changes needed. Strathon instruments
# the framework and evaluates policies on every tool call and model request.
result = agent.invoke({
    "messages": [{"role": "user", "content": "Email the Q3 numbers to sales@competitor.com"}]
})
# A policy that blocks email tool calls to competitor domains halts this
# before the tool runs — and records the decision in the audit log.
```

### 4. What happens when a policy matches

If you've created a policy like:

```cel
attrs["gen_ai.tool.name"] == "send_email"
  && attrs["gen_ai.tool.args"].contains("competitor.com")
```

Strathon raises `StrathonPolicyBlocked` **before** the tool call executes. The function body never runs. The block is logged in the audit trail with the matched policy, trace context, and timestamp.

```python
from strathon import StrathonPolicyBlocked

try:
    agent.run("Email our competitors with our pricing")
except StrathonPolicyBlocked as e:
    print(f"Blocked by policy: {e.policy_name}")
    # The tool call never executed. Logged in audit trail.
```

### Reliability: what happens if the receiver is unreachable

Policies evaluate inside your agent process, so a brief receiver outage doesn't
add latency to tool calls. By default the SDK is **fail-open**: if it can't
reach the receiver to refresh policy or halt state, agents keep running rather
than stalling. This favors availability and is the right default for most
deployments.

For security-critical agents where an unreachable receiver should *stop* tool
calls rather than allow them, enable **fail-closed** mode:

```python
client = Client(
    api_key="stra_...",
    endpoint="http://localhost:4318",
    fail_closed=True,                  # block when state can't be verified
    fail_closed_max_staleness_sec=60,  # how stale cached state may be first
)
```

Choose deliberately: fail-open prioritizes uptime, fail-closed prioritizes
containment. See [docs/intervention](https://getstrathon.com/docs/intervention)
for the full contract.

## Self-Host

```bash
git clone https://github.com/strathon/strathon.git
cd strathon && docker compose up
```

Opens at `http://localhost:3000` (dashboard) and `http://localhost:4318` (receiver API).

Register the first account, create a policy, get an API key, and connect your agent. No email server needed. The only dependency is PostgreSQL, which is included in the Compose stack.

Strathon ships as two images — `ghcr.io/strathon/receiver` and `ghcr.io/strathon/dashboard` — plus PostgreSQL. Compose runs all three; you can also run the receiver on its own or scale the dashboard independently.

For production deployments, see [Deploying with HTTPS](https://getstrathon.com/docs/self-hosting#https) (Caddy or nginx reverse proxy).

## Core Features

### Policy Engine

Write rules in [CEL](https://cel.dev) (Common Expression Language — the same language used by Kubernetes, Firebase, and Google Cloud IAM). Six enforcement actions: **block**, **steer**, **throttle**, **log**, **alert**, **require_approval**. Policies evaluate inside the agent process with sub-millisecond overhead, not at a network gateway. 12 OWASP-mapped templates for one-click setup. Shadow mode lets you test policies against live traffic without enforcing them, so you can validate before going live. [Learn more → getstrathon.com/docs/intervention](https://getstrathon.com/docs/intervention)

### Human Approval Workflows

Pause agent execution until an operator approves or denies in the dashboard, Slack, or Discord. Multi-party approval (N-of-M) for high-risk actions like financial transactions or data deletion. Automatic expiry and escalation prevent stuck agents. The SDK polls the receiver and blocks the calling thread until a decision arrives, so no architectural changes are needed in your agent. [Learn more → getstrathon.com/docs/approvals](https://getstrathon.com/docs/approvals)

### 50+ Credential Patterns

Detects AWS keys, GCP service accounts, GitHub tokens, Stripe keys, database URIs, private keys, JWTs, and more. Scans tool arguments and responses at ingest time. Paired with CEL policies, you can block any tool call that contains a detected credential pattern — preventing accidental secret leakage before it happens. [Learn more → getstrathon.com/docs/redaction](https://getstrathon.com/docs/redaction)

### EU AI Act Compliance

Evidence export for Articles 9–15 and 19, covering risk management, data governance, transparency, human oversight, accuracy, and serious incident reporting. Agent inventory with NIST AI RMF risk scoring. Incident detection generates Article 73 reporting metadata automatically. Designed for teams that need to demonstrate compliance to auditors without building bespoke tooling. [Learn more → getstrathon.com/docs/compliance-mapping](https://getstrathon.com/docs/compliance-mapping)

### MCP Security Gateway

Proxy between your agents and MCP servers. Every MCP request and response passes through the policy engine before reaching the server or returning to the agent. Credential scanning catches leaked secrets in MCP tool responses. Combined with egress policies, you control exactly which MCP tools your agents can call and what data they can send. Fails closed: a tool call is blocked, not allowed, if policy evaluation can't complete. [Learn more → getstrathon.com/docs/mcp](https://getstrathon.com/docs/mcp)

### Egress Proxy

A network-layer catch-all. Runs as a mitmproxy addon in front of the agent and enforces the same policies on all outbound HTTP, including calls the in-process SDK can't see (raw network calls, uninstrumented tools, third-party libraries). Scans request and response bodies for credential leakage. Optional defense-in-depth for higher-security deployments. [Learn more → getstrathon.com/docs/egress](https://getstrathon.com/docs/egress)

### Behavioral Drift Detection

EWMA and CUSUM statistical analysis per agent. Auto-calibrates from the first 50 observations, then fires alerts when behavior shifts — token usage spikes, tool call patterns change, latency anomalies. Catches compromised or malfunctioning agents before damage accumulates. [Learn more → getstrathon.com/docs/analytics](https://getstrathon.com/docs/analytics)

### Circuit Breakers

Per-agent and per-tool automatic trip/half-open/reset, modeled on the standard circuit breaker pattern. When error rates exceed the threshold, the breaker trips and blocks further calls. Half-open probes let traffic resume gradually. Contains blast radius without operator intervention. [Learn more → getstrathon.com/docs/budgets](https://getstrathon.com/docs/budgets)

### Dashboard

Next.js operator UI with trace waterfall, policy editor, approval cards, agent risk scoring, audit log with hash verification, budget charts, and compliance export. BFF security proxy with httpOnly cookies. Light and dark mode. Mobile responsive. [Learn more → getstrathon.com/docs](https://getstrathon.com/docs)

### Tamper-Evident Audit Log

HMAC-SHA256 hash chain on every operator action. Merkle root anchoring at configurable intervals. Append-only at the database level (PostgreSQL RLS). Built for environments where you need to prove the audit trail was not modified after the fact. [Learn more → getstrathon.com/docs/audit](https://getstrathon.com/docs/audit)

## Framework Integrations

```bash
pip install strathon                     # core
pip install strathon[langgraph]          # + LangGraph
pip install strathon[all]                # all 10 frameworks
```

| Framework | Integration Type | Description | Docs |
|-----------|-----------------|-------------|------|
| **LangGraph** | BaseCallbackHandler | Intercepts tool calls via LangChain callback system. Block/steer before execution. | [Guide](https://getstrathon.com/docs/frameworks/langgraph) |
| **CrewAI** | Event listener | Hooks into CrewAI event bus. Captures tool use, delegation, and task events. | [Guide](https://getstrathon.com/docs/frameworks/crewai) |
| **OpenAI Agents SDK** | TracingProcessor | Extends the official tracing extension point. Full span lifecycle capture. | [Guide](https://getstrathon.com/docs/frameworks/openai-agents) |
| **OpenAI** | Drop-in wrapper | Wraps `chat.completions.create`. Zero code changes beyond `instrument()`. | [Guide](https://getstrathon.com/docs/frameworks/openai) |
| **Anthropic** | Drop-in wrapper | Wraps `messages.create`. Same pattern as OpenAI integration. | [Guide](https://getstrathon.com/docs/frameworks/anthropic) |
| **LangChain** | BaseCallbackHandler | Same handler as LangGraph. Works with chains, agents, and tools. | [Guide](https://getstrathon.com/docs/frameworks/langchain) |
| **AutoGen** | Agent wrapper | Wraps `BaseChatAgent.on_messages`. Captures multi-agent conversations. | [Guide](https://getstrathon.com/docs/frameworks/autogen) |
| **Claude Agent SDK** | Query wrapper | Wraps `query()`. Captures tool use and agent reasoning. | [Guide](https://getstrathon.com/docs/frameworks/claude-agent-sdk) |
| **Pydantic AI** | AbstractCapability | First-class plugin via Pydantic AI's capability system. No monkey-patching. | [Guide](https://getstrathon.com/docs/frameworks/pydantic-ai) |
| **Google ADK** | BasePlugin | First-class plugin via Google ADK's plugin system. No monkey-patching. | [Guide](https://getstrathon.com/docs/frameworks/google-adk) |

All integrations use first-class framework extension points where available. Zero monkey-patching.

## CLI

```bash
pip install strathon-cli
```

```bash
# Policy management
strathon policies list
strathon policies create --name "block-email" \
  --expr 'attrs["gen_ai.tool.name"] == "send_email"' --action block
strathon policies create --template block-prompt-injection
strathon policies create --from-english "block all shell commands"
strathon policies import policies.yaml
strathon policies test --name my-policy --last 100
strathon policies suggest
strathon policies conflicts

# Traces and spans
strathon traces list --last 1h
strathon traces tree <trace-id>
strathon spans search --tool send_email --limit 50

# Operations
strathon halts create --scope project --reason "Emergency"
strathon budgets list
strathon budgets forecast --agent my-agent --days 30
strathon approvals list --pending
strathon approvals approve <approval-id>

# Compliance and audit
strathon compliance export --format sarif
strathon audit list --last 24h

# Administration
strathon admin list-users
strathon admin reset-password --email user@company.com
strathon admin transfer-ownership --to user@company.com
strathon admin revoke-all-keys
```

13 command groups, 30+ subcommands. `--json` flag on every command for scripting and CI pipelines.

## Performance

Throughput depends on your hardware, PostgreSQL configuration, and span
payload, so rather than quote a single number, Strathon ships a reproducible
benchmark that measures it on your environment:

```bash
cd receiver && uvicorn main:app --workers 4 --port 4318
pip install httpx opentelemetry-proto protobuf
python benchmarks/loadtest.py --endpoint http://127.0.0.1:4318 \
    --api-key "$STRATHON_API_KEY" --requests 5000 --concurrency 16
```

It drives the full per-span pipeline — protobuf parse, CEL policy evaluation,
50+ credential patterns, PII redaction, and the batched PostgreSQL write — and
reports sustained spans/sec, latency p50/p95/p99, and error rate for the
hardware it ran on.

Receivers are stateless and scale horizontally behind a load balancer. The
receiver tier scales near-linearly until the shared PostgreSQL becomes the
bottleneck — all receivers write to one primary, so plan capacity around the
database write path (PgBouncer, a larger primary, read replicas for dashboard
queries), not the receiver count. See the [Scaling Guide](https://getstrathon.com/docs/scaling).

Benchmarked on MacBook Pro M-series, 4 uvicorn workers, PostgreSQL 16, 50,000 spans.

## OWASP Agentic Security Coverage

Strathon's threat model is anchored on the [OWASP Top 10 for Agentic Applications 2026](https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/).

| Threat | Strathon Coverage |
|--------|-------------------|
| **ASI01** Agent Goal Hijack | CEL policies on prompt content and input patterns, block/alert on detected hijack attempts |
| **ASI02** Tool Misuse and Exploitation | Block/allow-list on tool names and arguments, approval workflows for sensitive tools |
| **ASI03** Identity and Privilege Abuse | Scoped API keys, RBAC (4 roles), MFA, per-key rate limits |
| **ASI04** Insecure Agent-to-Agent Communication | MCP gateway intercepts and evaluates all agent-to-agent calls against policies |
| **ASI05** Unsafe Agent Memory Management | Detects memory poisoning effects via behavioral drift detection (EWMA/CUSUM), halt propagation |
| **ASI06** Implicit Trust and Inadequate Verification | Cost and iteration budgets with auto-halt, approval workflows, circuit breakers |
| **ASI07** Overwhelming HITL Controls | Multi-party approval (N-of-M), auto-escalation, approval reaper, circuit breakers, kill switches |
| **ASI08** Inadequate Agent Access Controls | [Egress proxy](https://getstrathon.com/docs/egress) with domain allowlisting, MCP gateway, credential scanning, RBAC |
| **ASI09** Insufficient Logging, Monitoring, and Auditing | Tamper-evident audit log, trace search, webhook alerts, dashboard, SARIF export |
| **ASI10** Rogue Agents | Behavioral drift detection (Vigil), heartbeat monitoring, SDK integrity check, kill switches |

## Scope and Limitations

Strathon enforces policy at the **tool-call boundary**: it inspects each tool call and its arguments before execution and can block, steer, throttle, require approval, log, or alert. This is the layer where an agent's decisions become real-world actions, and it is the right place to stop an action regardless of whether the model was mistaken, manipulated, or compromised.

It is one layer of agent security, not the whole of it. Some attack classes are not solvable at the tool-call boundary alone, and we would rather say so than imply otherwise:

- **Data-flow exfiltration.** When sensitive data read earlier is smuggled inside an otherwise-valid argument (for example, encoded into a URL on an allowed domain), the individual call looks legitimate. Catching this reliably requires data-flow provenance (taint tracking), which is on our roadmap.
- **Poisoned tool output and context attacks.** Instructions injected into a tool's response, or into the tool-list during a protocol handshake, can influence an agent without producing a malicious call of their own. These need response sanitization and input filtering, not only call-time enforcement.
- **Memory poisoning.** A malicious instruction planted in an agent's long-term memory produces a later call that carries no in-band signal. Defending this is a memory-integrity and training-time problem.
- **Aggregate/economic abuse.** Each call can be legitimate while the volume is the attack. Strathon's budgets and drift detection address this, but through cost and rate accounting rather than per-call policy.

The honest framing from the security community applies: an agent that combines access to private data, exposure to untrusted content, and an outbound channel is structurally exploitable, and the most reliable mitigation is to remove one of those legs by design. Strathon governs the outbound-action leg well; it is most effective as part of a layered design, not as a single guarantee.

## Architecture

```
 Your Agent                       Strathon
┌─────────────────┐              ┌──────────────────┐        ┌────────────┐
│                 │   OTLP/HTTP  │    Receiver      │        │            │
│   Agent code    │─────────────▶│    (FastAPI)     │───────▶│ PostgreSQL │
│                 │              │                  │        │            │
│  ┌───────────┐  │◀─────────────│  Policy eval     │        └────────────┘
│  │ Strathon  │  │  block/allow │  Credential scan │
│  │ SDK       │  │              │  PII redaction   │        ┌───────────┐
│  │ (3 lines) │  │              │  Audit log       │───────▶│ Dashboard │
│  └───────────┘  │              │  Webhooks        │        │ (Next.js) │
└─────────────────┘              └──────────────────┘        └───────────┘
```

Single PostgreSQL dependency. No Redis, no ClickHouse, no S3. Self-host on one machine or scale horizontally behind a load balancer.

## Deploy

### Self-hosted (recommended)

```bash
# Docker Compose — includes PostgreSQL, receiver, and dashboard
git clone https://github.com/strathon/strathon.git
cd strathon && docker compose up
```

### Docker images

```bash
# Pull from GitHub Container Registry
docker pull ghcr.io/strathon/receiver:latest
docker pull ghcr.io/strathon/dashboard:latest
```

### HTTPS (production)

For production, put Caddy or nginx in front of the receiver as a reverse proxy. See [Deploying with HTTPS](https://getstrathon.com/docs/self-hosting#https) for a complete Caddyfile and nginx config.

### Cloud (managed)

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
