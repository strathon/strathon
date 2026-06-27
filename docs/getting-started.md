# Getting Started

This guide takes you from nothing to a running firewall that blocks a real
agent action in about five minutes. It uses [LangGraph](frameworks/langgraph.md)
as the example framework, but the same three steps apply to any of the
[10 supported frameworks](frameworks/).

Strathon sits between your agent and the tools and models it calls. Every tool
call and model request is evaluated against your policies before it executes.
A blocked call never runs; an approved call proceeds and is recorded in a
tamper-evident audit log.

## Prerequisites

- Docker (or Python 3.12+ and PostgreSQL 16 if you prefer to run from source)
- An existing agent built on a supported framework

## 1. Start the server

The fastest path is Docker Compose, which runs the receiver, the dashboard, and
PostgreSQL together:

```bash
git clone https://github.com/strathon/strathon.git
cd strathon && docker compose up -d
```

The dashboard opens at `http://localhost:3000` and the receiver API at
`http://localhost:4318`. The only dependency is PostgreSQL, which is included in
the Compose stack, so no separate database or email server is needed.

If you would rather run only the receiver, start it directly:

```bash
docker run -d --name strathon \
  -p 4318:4318 \
  -e DATABASE_URL="postgresql://strathon:strathon@db:5432/strathon" \
  -e STRATHON_AUDIT_HMAC_KEY="$(openssl rand -hex 32)" \
  -e STRATHON_ENCRYPTION_KEY="$(openssl rand -base64 32)" \
  ghcr.io/strathon/receiver:latest
```

Register the first account in the dashboard; it becomes the project owner. For
the full stack and production options, see [Self-Hosting](self-hosting.md).

## 2. Create an API key and install the SDK

In the dashboard, go to **Settings → API Keys** and create a key. Then install
the SDK with the extra for your framework:

```bash
pip install "strathon[langgraph]"
```

## 3. Connect your agent

Connect Strathon to your existing agent. For LangGraph, `instrument()` returns a
callback handler that you attach to each invocation — that handler is what
enforces policies and traces the run, so passing it is required.

```python
from strathon import Client
from strathon.instrumentation.langgraph import instrument

client = Client(
    api_key="stra_...",          # the key from step 2
    endpoint="http://localhost:4318",
)
handler = instrument(client)

# Pass the handler on every invocation; your agent logic is otherwise unchanged.
result = agent.invoke(
    {"messages": [{"role": "user", "content": "Email the Q3 numbers to sales@competitor.com"}]},
    config={"callbacks": [handler]},
)
```

Other frameworks connect differently — a plugin, hooks, or a capability instead
of a callback handler. See the [integration guides](frameworks/) for
the exact one-time wiring per framework.

## 4. Write a policy

Policies are written in [CEL](cel-reference.md). Create one in the dashboard
(**Policies → New**) or from the catalog of built-in templates. For example, to
block any email tool call addressed to a competitor domain:

```cel
attrs["gen_ai.tool.name"] == "send_email"
  && attrs["strathon.tool.args"].contains("competitor.com")
```

Set the action to `block` and the status to `enabled`. Start with `shadow`
status to see what *would* be blocked without actually blocking anything — see
[Shadow mode](intervention.md).

## 5. See it work

Run your agent again. When it tries to send the email, Strathon raises
`StrathonPolicyBlocked` before the tool function executes. The agent never sends
the message.

```python
from strathon import StrathonPolicyBlocked

try:
    agent.invoke(
        {"messages": [{"role": "user", "content": "Email our competitors with our pricing"}]},
        config={"callbacks": [handler]},
    )
except StrathonPolicyBlocked as e:
    print(f"Blocked by policy: {e.policy_name}")
    # The tool call never executed. Logged in the audit trail.
```

Open the dashboard to review what happened:

- **Traces** shows the full execution, with the blocked span highlighted.
- **Audit** shows a tamper-evident record of the decision: which policy matched,
  what action was taken, and when.

## Reliability: what happens if the receiver is unreachable

Policies evaluate inside your agent process, so a brief receiver outage does not
add latency to tool calls. By default the SDK is **fail-open**: if it cannot
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
containment. See [Runtime Intervention](intervention.md) for the full contract.

## What's next

- **[Core Concepts](concepts.md)** — the mental model: spans, traces, policies,
  the actions, inline enforcement, the audit log.
- **[Runtime Intervention](intervention.md)** — every action, allow-list mode,
  time-based rules, policy versioning, halts, budgets, webhooks.
- **[CEL Reference](cel-reference.md)** — the policy language, with examples.
- **[Human Approval](approvals.md)** — pause high-risk calls for operator sign-off.
- **[Framework guides](frameworks/)** — setup for all 10 supported frameworks.
- **[Self-Hosting](self-hosting.md)** — production deployment, environment
  variables, HTTPS, scaling.
