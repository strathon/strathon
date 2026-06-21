<div align="center">
  <img src="https://raw.githubusercontent.com/strathon/strathon/main/assets/banner.png" alt="Strathon" width="600" />

  <p><strong>Open-source AI agent firewall</strong></p>
  <p>Blocks dangerous agent tool calls before they execute. In-process CEL enforcement at the tool-call boundary.</p>

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
  <a href="https://github.com/strathon/strathon/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-Apache%202.0-blue.svg" alt="License"></a>
  <a href="https://discord.gg/Ta9XRmh4H"><img src="https://img.shields.io/badge/Discord-5865F2?logo=discord&logoColor=white" alt="Discord"></a>
  <a href="https://x.com/strathonai"><img src="https://img.shields.io/twitter/follow/strathonai?logo=X&color=%23f5f5f5" alt="X"></a>
  <a href="https://www.linkedin.com/company/strathonai"><img src="https://custom-icon-badges.demolab.com/badge/LinkedIn-0A66C2?logo=linkedin-white&logoColor=fff" alt="LinkedIn"></a>
  <br/>
  <a href="https://github.com/strathon/strathon/graphs/commit-activity"><img alt="Commits last month" src="https://img.shields.io/github/commit-activity/m/strathon/strathon?labelColor=%2332b583&color=%2312b76a" /></a>
</div>

<br/>

Strathon is an **open-source firewall for AI agents**: it evaluates every tool call against a [CEL](https://cel.dev) policy and blocks the dangerous ones **before** they execute.

An agent that reads untrusted content will eventually be told, by that content, to misuse its tools. The moment that matters is the tool call: the email actually sent, the shell command actually run, the request actually made. Most agent tooling records what went wrong after the fact. Strathon enforces at the tool-call boundary, in-process, while the call can still be stopped.

Enforcement runs at three layers, so a call is governed however the agent reaches the outside world. The **SDK** evaluates policies inside your agent process, in under a millisecond, and works out of the box across 10 frameworks. The **MCP gateway** ships inside the receiver and screens any MCP client you point at it. The **egress proxy** is a separate process you deploy to govern raw outbound HTTP that no SDK can see. One invariant holds everywhere: a surface that matches a policy it cannot fully execute fails closed, blocked and recorded, never silently allowed.

Seven enforcement actions. 10 framework integrations. 1,400+ tests. One PostgreSQL, self-hosted in minutes.

## Quickstart

From zero to watching Strathon block a real tool call.

### 1. Start the server

```bash
git clone https://github.com/strathon/strathon.git
cd strathon && docker compose up -d
```

Dashboard opens at `http://localhost:3000`, receiver API at `http://localhost:4318`. Register the first account (it becomes the project owner) and grab an API key from Settings → API Keys.

### 2. Write a policy

In the dashboard (Policies → New), create a policy with action **block**:

```cel
attrs["gen_ai.tool.name"] == "send_email"
  && attrs["strathon.tool.args"].contains("competitor.com")
```

Policies are CEL expressions over the call's attributes: the tool name, its arguments, the model, token usage, the agent's identity. Not sure yet? Set the policy's status to **shadow** and Strathon will record every call it *would* have blocked without enforcing anything.

### 3. Connect your agent

```bash
pip install "strathon[langgraph]"
```

```python
from strathon import Client, instrument

client = Client(
    api_key="stra_...",              # from dashboard → Settings → API Keys
    endpoint="http://localhost:4318",
)
instrument(client, frameworks=["langgraph"])
```

Your existing LangGraph agent needs no changes. Strathon instruments the framework and evaluates policies on every tool call and model request.

### 4. Watch a call get blocked

```python
from strathon import StrathonPolicyBlocked

try:
    agent.invoke({"messages": [{"role": "user",
        "content": "Email the Q3 numbers to sales@competitor.com"}]})
except StrathonPolicyBlocked as e:
    print(f"Blocked by policy: {e.policy_name}")
```

`StrathonPolicyBlocked` is raised **before** the tool call executes. The function body never runs. The decision lands in the audit trail with the matched policy, trace context, and timestamp.

## Dashboard

The receiver ships with a Next.js operator console: live posture, the policy editor with OWASP-mapped templates, approval cards, trace waterfalls, audit log, and budget charts. Light and dark mode included.

**Overview** — live posture across your agent firewall

<p align="center">
  <img src="https://raw.githubusercontent.com/strathon/strathon/main/assets/dashboard-overview-light.png" alt="Strathon dashboard overview: recent spans, blocked calls today, pending approvals, spend, and recent agent activity" width="900" />
</p>

**Policies** — every rule, its action, and how often it fires

<p align="center">
  <img src="https://raw.githubusercontent.com/strathon/strathon/main/assets/dashboard-policies-light.png" alt="Strathon policy list showing enabled policies with require_approval, block, log, and throttle actions" width="900" />
</p>

**Policy templates** — OWASP Agentic Top 10 mapped, ready to apply

<p align="center">
  <img src="https://raw.githubusercontent.com/strathon/strathon/main/assets/dashboard-policy-templates-light.png" alt="Strathon new-policy screen with OWASP-mapped templates for blocking dangerous tools, data exfiltration, SQL injection, and more" width="900" />
</p>

<details>
<summary><b>Dark mode</b> — every view ships in dark too</summary>

<p align="center">
  <img src="https://raw.githubusercontent.com/strathon/strathon/main/assets/dashboard-overview-dark.png" alt="Strathon dashboard overview in dark mode" width="900" />
</p>
<p align="center">
  <img src="https://raw.githubusercontent.com/strathon/strathon/main/assets/dashboard-policies-dark.png" alt="Strathon policy list in dark mode" width="900" />
</p>
<p align="center">
  <img src="https://raw.githubusercontent.com/strathon/strathon/main/assets/dashboard-policy-templates-dark.png" alt="Strathon policy templates in dark mode" width="900" />
</p>

</details>

## Failure Semantics

A firewall's behavior when its control plane is unreachable is part of its contract, so here is Strathon's. Policies evaluate inside your agent process against locally cached state, so a brief receiver outage adds no latency to tool calls. By default the SDK is **fail-open**: if it cannot reach the receiver to refresh policy or halt state, agents keep running rather than stalling. For security-critical agents where an unreachable receiver should *stop* tool calls instead, enable fail-closed mode:

```python
client = Client(
    api_key="stra_...",
    endpoint="http://localhost:4318",
    fail_closed=True,                  # block when state can't be verified
    fail_closed_max_staleness_sec=60,  # how stale cached state may be first
)
```

Fail-open prioritizes uptime, fail-closed prioritizes containment. Choose deliberately. See [docs/intervention](https://getstrathon.com/docs/intervention) for the full contract.

## Core Features

### Policy Engine

Write rules in [CEL](https://cel.dev) (Common Expression Language, the same language used by Kubernetes, Firebase, and Google Cloud IAM). Seven enforcement actions: **block**, **steer**, **throttle**, **log**, **alert**, **require_approval**, **allow**. Policies evaluate inside the agent process with sub-millisecond overhead. 12 OWASP-mapped templates for one-click setup. Shadow mode evaluates and records against live traffic but never enforces, so you can validate a policy's match rate before turning it on. [Learn more → getstrathon.com/docs/intervention](https://getstrathon.com/docs/intervention)

### Human Approval Workflows

Pause an agent until an operator approves or denies in the dashboard, Slack, or Discord. Multi-party approval (N-of-M) for high-risk actions like financial transactions or data deletion. Undecided requests expire automatically, so an unanswered approval fails closed instead of leaving the agent hung. On surfaces that can pause (async pre-execution hooks, tool-invoke wrapping, CrewAI) the SDK holds the call until a decision arrives; on synchronous callback surfaces that cannot pause (LangGraph, LangChain, Pydantic AI) a matched approval fails closed, blocked and recorded. The per-surface matrix is in the docs. [Learn more → getstrathon.com/docs/intervention](https://getstrathon.com/docs/intervention)

### MCP Security Gateway

Enforce policy on tools you never instrumented. Point any MCP client at the receiver's `/v1/mcp/proxy` endpoint and every `tools/call` is evaluated before it reaches the server: a blocked call comes back as a JSON-RPC error instead of executing, a steered call returns the replacement without ever contacting the upstream tool, and `tools/list` is filtered so blocked tools never appear in the model's context at all. Responses are scanned for leaked credentials on the way back. If policies cannot be loaded or evaluated, the call is blocked, not allowed. [Learn more → getstrathon.com/docs/mcp](https://getstrathon.com/docs/mcp)

### Egress Proxy

Govern the traffic no SDK can see. Run the proxy in front of the agent, set `HTTP_PROXY`, and every outbound request from any library, instrumented or not, is evaluated against the same policy set: calls carry the tool name `http.<method>` and the full URL, so the policy that blocks a domain for your agent blocks it for a third-party dependency too. A credential detected in a request body blocks the request outright; one detected in a response is redacted before the agent reads it. Runs as a separate mitmproxy process in explicit `HTTP_PROXY` mode today; transparent interception is on the roadmap. [Learn more → getstrathon.com/docs/egress](https://getstrathon.com/docs/egress)

### Credential Leak Detection

Catch secrets in motion. 70+ patterns cover AWS keys, GCP service accounts, GitHub tokens, Stripe keys, database URIs, private keys, JWTs, and more, scanned across tool arguments and request and response bodies. Pair the detector with a CEL policy to block any tool call carrying a credential, and stored spans are scrubbed at ingest so a leaked key never sits in your trace history. [Learn more → getstrathon.com/docs/redaction](https://getstrathon.com/docs/redaction)

### Behavioral Drift Detection

EWMA and CUSUM statistical analysis per agent, tracking four signals: policy deny rate, error rate, tool-call rate, and cost per minute. Auto-calibrates from each agent's first 100 observations (configurable), then fires webhook alerts when behavior shifts from the learned baseline, catching both sudden spikes and gradual drift from compromised or malfunctioning agents. [Learn more → getstrathon.com/docs/analytics](https://getstrathon.com/docs/analytics)

### Circuit Breakers

Per-agent and per-tool failure tracking, modeled on the standard CLOSED → OPEN → HALF-OPEN pattern. When an agent or tool exceeds the error threshold the breaker trips: subsequent spans are flagged with the breaker state at ingest, open breakers surface in the API and dashboard, and you can pair the flag with a policy or halt to stop the agent. Half-open probes detect recovery; a reset endpoint closes a breaker manually. [Learn more → getstrathon.com/docs/budgets](https://getstrathon.com/docs/budgets)

### Tamper-Evident Audit Log

Prove the trail was not rewritten. Every operator action is chained with HMAC-SHA256, Merkle roots are anchored at configurable intervals, and the table is append-only at the database level (PostgreSQL row-level security). Built for environments where the audit log is evidence, not just history. [Learn more → getstrathon.com/docs/audit](https://getstrathon.com/docs/audit)

### EU AI Act Compliance

Evidence export for Articles 9–15 and 19, covering risk management, data governance, transparency, human oversight, accuracy, and serious incident reporting. Agent inventory with NIST AI RMF risk scoring. Incident detection generates Article 73 reporting metadata automatically. Built for teams that need to demonstrate compliance to auditors without building bespoke tooling. [Learn more → getstrathon.com/docs/compliance-mapping](https://getstrathon.com/docs/compliance-mapping)

### Dashboard

Next.js operator UI: trace waterfall, policy editor, approval cards, agent risk scoring, audit log with hash verification, budget charts, and compliance export. BFF security proxy with httpOnly cookies. Light and dark mode, mobile responsive. [Learn more → getstrathon.com/docs](https://getstrathon.com/docs)

## Framework Integrations

```bash
pip install strathon                       # core
pip install "strathon[langgraph]"          # + LangGraph
pip install "strathon[all]"                # all 10 frameworks
```

| Framework | Integration Type | Description | Docs |
|-----------|-----------------|-------------|------|
| **LangGraph** | BaseCallbackHandler | Intercepts tool calls via LangChain callback system. Blocks and throttles before execution. | [Guide](https://getstrathon.com/docs/frameworks/langgraph) |
| **CrewAI** | Event listener + tool wrap | Event bus for observability; tool-invoke wrapping for the full action set, including interactive approval. | [Guide](https://getstrathon.com/docs/frameworks/crewai) |
| **OpenAI Agents SDK** | TracingProcessor + RunHooks | Official tracing extension point; run hooks for pre-execution enforcement. | [Guide](https://getstrathon.com/docs/frameworks/openai-agents) |
| **OpenAI** | Drop-in wrapper | Wraps `chat.completions.create`. Zero code changes beyond `instrument()`. | [Guide](https://getstrathon.com/docs/frameworks/openai) |
| **Anthropic** | Drop-in wrapper | Wraps `messages.create`. Same pattern as the OpenAI integration. | [Guide](https://getstrathon.com/docs/frameworks/anthropic) |
| **LangChain** | BaseCallbackHandler | Same handler as LangGraph. Works with chains, agents, and tools. | [Guide](https://getstrathon.com/docs/frameworks/langchain) |
| **AutoGen** | Agent wrapper | Wraps `BaseChatAgent.on_messages`. Captures multi-agent conversations. | [Guide](https://getstrathon.com/docs/frameworks/autogen) |
| **Claude Agent SDK** | PreToolUse hooks | First-class hook interception on `ClaudeSDKClient`; query wrapper for observability. | [Guide](https://getstrathon.com/docs/frameworks/claude-agent-sdk) |
| **Pydantic AI** | AbstractCapability | First-class plugin via Pydantic AI's capability system. Steer substitutes the tool result directly. | [Guide](https://getstrathon.com/docs/frameworks/pydantic-ai) |
| **Google ADK** | BasePlugin | First-class plugin via Google ADK's plugin system. No monkey-patching. | [Guide](https://getstrathon.com/docs/frameworks/google-adk) |

Integrations use first-class framework extension points wherever the framework provides them (TracingProcessor, BasePlugin, AbstractCapability, callback handlers); where none exists, Strathon wraps the framework's documented entry point. What each surface can enforce differs, and each guide says exactly which mechanism is used and what it enforces; the [per-surface matrix](https://getstrathon.com/docs/intervention) has the full picture.

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
strathon policies create --from-english "block all shell commands"   # needs STRATHON_AI_API_KEY on the receiver
strathon policies import policies.yaml
strathon policies test --name my-policy --last 100
strathon policies suggest
strathon policies conflicts

# Traces and spans
strathon traces list --limit 50 --agent my-agent
strathon traces tree <trace-id>
strathon spans search --tool send_email --limit 50

# Operations
strathon halts create --scope project --reason "Emergency"
strathon budgets list
strathon budgets forecast
strathon approvals list --status pending
strathon approvals approve <approval-id>

# Compliance and audit
strathon compliance export --format sarif
strathon audit list --limit 100

# API key management
strathon keys list
strathon keys create --name "ci-pipeline" --scope traces:read --scope policies:read
strathon keys rotate <key-id>          # new secret, old one invalidated
strathon keys revoke <key-id>

# Administration
strathon admin list-users
strathon admin reset-password --email user@company.com
strathon admin transfer-ownership --to user@company.com
strathon admin revoke-all-keys
```

14 command groups. Every read command takes a `--json` flag for scripting and CI pipelines. The CLI is fully headless: bootstrap with a dev key or `strathon` registration, then create and rotate scoped keys without ever opening the dashboard.

## Performance

Throughput depends on your hardware, PostgreSQL configuration, and span payload, so rather than quote a single number, Strathon ships a reproducible benchmark that measures it on your environment:

```bash
# terminal 1: start the receiver
cd receiver && uvicorn main:app --workers 4 --port 4318

# terminal 2: from the repo root
pip install httpx opentelemetry-proto protobuf
python benchmarks/loadtest.py --endpoint http://127.0.0.1:4318 \
    --api-key "$STRATHON_API_KEY" --requests 5000 --concurrency 16
```

It drives the full per-span pipeline (protobuf parse, CEL policy evaluation, 70+ credential patterns, PII redaction, and the batched PostgreSQL write) and reports sustained spans/sec, latency p50/p95/p99, and error rate for the hardware it ran on. (Reference environment we test against: MacBook Pro M-series, 4 uvicorn workers, PostgreSQL 16, 50,000 spans.)

Receivers are stateless and scale horizontally behind a load balancer. The receiver tier scales near-linearly until the shared PostgreSQL becomes the bottleneck: all receivers write to one primary, so plan capacity around the database write path (PgBouncer, a larger primary, read replicas for dashboard queries), not the receiver count. See the [Scaling Guide](https://getstrathon.com/docs/scaling).

## OWASP Agentic Security Coverage

Strathon's threat model is anchored on the [OWASP Top 10 for Agentic Applications 2026](https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/).

| Threat | Strathon Coverage |
|--------|-------------------|
| **ASI01** Agent Goal Hijack | CEL policies on prompt content and input patterns, block/alert on detected hijack attempts |
| **ASI02** Tool Misuse and Exploitation | Pre-execution policy enforcement on tool names and arguments (block/allow-list), exact tool-name matching with default-deny allow-lists, and approval workflows for sensitive tools |
| **ASI03** Identity and Privilege Abuse | Scoped API keys, RBAC (4 roles), MFA, per-key rate limits |
| **ASI04** Agentic Supply Chain Vulnerabilities | [MCP gateway](https://getstrathon.com/docs/mcp) evaluates third-party tool/MCP-server calls against policies; [egress proxy](https://getstrathon.com/docs/egress) with domain allowlisting; credential scanning on tool responses |
| **ASI05** Unexpected Code Execution | Block and allow-list policies on shell, code, and SQL tools; approval workflows required before code-executing tools run |
| **ASI06** Memory and Context Poisoning | Behavioral drift detection (Vigil, EWMA/CUSUM) surfaces poisoning effects; halt propagation; content redaction on ingested data |
| **ASI07** Insecure Inter-Agent Communication | MCP gateway evaluates inter-agent and tool calls against policies, fails closed when evaluation cannot complete |
| **ASI08** Cascading Failures | Cost and iteration budgets with auto-halt, kill switches, and halt propagation to contain failures before they fan out; circuit breakers flag repeatedly failing agents and tools |
| **ASI09** Human-Agent Trust Exploitation | Human approval workflows (multi-party N-of-M, automatic expiry of undecided requests, Slack/Discord approval notifications), tamper-evident audit log, trace search, and SARIF export for accountability |
| **ASI10** Rogue Agents | Behavioral drift detection (Vigil), heartbeat monitoring, SDK integrity check, kill switches |

## Scope and Limitations

Strathon enforces policy at the **tool-call boundary**: it inspects each tool call and its arguments before execution and can block, steer, throttle, require approval, log, alert, or explicitly allow. This is the layer where an agent's decisions become real-world actions, and it is the right place to stop an action regardless of whether the model was mistaken, manipulated, or compromised.

It is one layer of agent security, not the whole of it. Two honesty notes worth stating up front, with the full picture in [docs/scope.md](docs/scope.md):

- **Credentials: detection today, injection later.** Strathon detects and redacts secrets (70+ patterns) in tool arguments and request/response bodies, which is reactive protection against a leak in progress. Gateway-side credential *injection*, where the agent never holds the secret at all, is stronger and is on the roadmap, not shipped.
- **Egress: explicit today, transparent later.** The egress proxy runs in explicit (`HTTP_PROXY`) mode, which is defense-in-depth for a cooperating agent. Transparent, network-level interception that an agent cannot opt out of is on the roadmap.

Some attack classes are not solvable at the tool-call boundary alone, and we would rather say so than imply otherwise:

- **Data-flow exfiltration.** When sensitive data read earlier is smuggled inside an otherwise-valid argument (for example, encoded into a URL on an allowed domain), the individual call looks legitimate. Catching this reliably requires data-flow provenance (taint tracking), which is on our roadmap.
- **Poisoned tool output and context attacks.** Instructions injected into a tool's response, or into the tool list during a protocol handshake, can influence an agent without producing a malicious call of their own. These need response sanitization and input filtering, not only call-time enforcement.
- **Memory poisoning.** A malicious instruction planted in an agent's long-term memory produces a later call that carries no in-band signal. Defending this is a memory-integrity and training-time problem.
- **Aggregate/economic abuse.** Each call can be legitimate while the volume is the attack. Strathon's budgets and drift detection address this, but through cost and rate accounting rather than per-call policy.

The honest framing from the security community applies: an agent that combines access to private data, exposure to untrusted content, and an outbound channel is structurally exploitable, and the most reliable mitigation is to remove one of those legs by design. Strathon governs the outbound-action leg well; it is most effective as part of a layered design, not as a single guarantee.

## Architecture

<p align="center">
  <img src="https://raw.githubusercontent.com/strathon/strathon/main/assets/architecture.png" alt="Strathon architecture: the SDK enforces in-process at the tool-call boundary as the primary path; the MCP gateway and egress proxy are opt-in layers; all three feed the FastAPI receiver backed by a single PostgreSQL, with a Next.js dashboard" width="900" />
</p>

Single PostgreSQL dependency. No ClickHouse, no S3, no required Redis (Redis is optional and only enables async webhook delivery). Self-host on one machine or scale horizontally behind a load balancer.

## Deploy

### Self-hosted (recommended)

```bash
# Docker Compose: PostgreSQL, receiver, and dashboard
git clone https://github.com/strathon/strathon.git
cd strathon && docker compose up
```

Register the first account, create a policy, get an API key, and connect your agent. No email server needed; the only dependency is PostgreSQL, included in the Compose stack. Strathon ships as two images, `ghcr.io/strathon/receiver` and `ghcr.io/strathon/dashboard`, plus PostgreSQL. Compose runs all three; you can also run the receiver on its own or scale the dashboard independently.

Before running in production, set the security keys (`STRATHON_AUDIT_HMAC_KEY`, `STRATHON_ENCRYPTION_KEY`, `STRATHON_PASSWORD_PEPPER`) in your `.env`; without them the receiver falls back to development defaults with a warning. See [Self-Hosting](https://getstrathon.com/docs/self-hosting).

### Docker images

```bash
docker pull ghcr.io/strathon/receiver:latest
docker pull ghcr.io/strathon/dashboard:latest
```

### HTTPS (production)

Put Caddy or nginx in front of the receiver as a reverse proxy. See [Deploying with HTTPS](https://getstrathon.com/docs/self-hosting#https) for a complete Caddyfile and nginx config.

### Cloud (managed)

A managed cloud offering is planned. Self-hosting is the supported deployment today; see [getstrathon.com](https://getstrathon.com) for updates.

## Community

- [Discord](https://discord.gg/Ta9XRmh4H) for questions, discussion, and support
- [GitHub Issues](https://github.com/strathon/strathon/issues) for bug reports
- [GitHub Discussions](https://github.com/strathon/strathon/discussions) for feature requests and ideas
- [Contributing](CONTRIBUTING.md) for the development setup guide

## License

Apache License 2.0, across all components. See [LICENSE](LICENSE) and
[LICENSING.md](LICENSING.md).
