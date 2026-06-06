# Getting Started

This guide takes you from nothing to a running firewall that blocks a real
agent action in about five minutes. It uses [LangGraph](frameworks/langgraph.md)
as the example framework, but the same three steps apply to any of the
[10 supported frameworks](frameworks/).

Strathon sits between your agent and the tools and models it calls. Every tool
call and model request is evaluated against your policies *before* it executes.
A blocked call never runs; an approved call proceeds and is recorded in a
tamper-evident audit log.

## Prerequisites

- Docker (or Python 3.11+ and PostgreSQL 16 if you prefer to run from source)
- An existing agent built on a supported framework

## 1. Start the receiver

The receiver is the policy engine and API. Run it with Docker:

```bash
docker run -d --name strathon \
  -p 4318:4318 \
  -e DATABASE_URL="postgresql://strathon:strathon@db:5432/strathon" \
  -e STRATHON_AUDIT_HMAC_KEY="$(openssl rand -hex 32)" \
  -e STRATHON_ENCRYPTION_KEY="$(openssl rand -base64 32)" \
  ghcr.io/strathon/receiver:latest
```

For a full local stack (receiver + dashboard + Postgres) use Docker Compose —
see [Self-Hosting](self-hosting.md).

The dashboard is available at `http://localhost:3000`. Register the first
account; it becomes the project owner.

## 2. Create an API key and install the SDK

In the dashboard, go to **Settings → API Keys** and create a key. Then install
the SDK with the extra for your framework:

```bash
pip install strathon[langgraph]
```

## 3. Connect your agent

Add two lines to your existing agent. No other code changes are needed —
Strathon instruments the framework's own extension points.

```python
from strathon import Client, instrument

client = Client(
    api_key="stra_...",          # the key from step 2
    endpoint="http://localhost:4318",
)
instrument(client, frameworks=["langgraph"])

# Your existing LangGraph agent runs unchanged from here.
result = agent.invoke({
    "messages": [{"role": "user", "content": "Email the Q3 numbers to sales@competitor.com"}]
})
```

## 4. Write a policy

Policies are written in [CEL](cel-reference.md). Create one in the dashboard
(**Policies → New**) or from the catalog of built-in templates. For example,
to block any email tool call addressed to a competitor domain:

```cel
attrs["gen_ai.tool.name"] == "send_email"
  && attrs["strathon.tool.args"].contains("competitor.com")
```

Set the action to `block` and the status to `enabled`. (Start with `shadow`
status to see what *would* be blocked without actually blocking anything —
see [Shadow mode](intervention.md).)

## 5. See it work

Run your agent again. When it tries to send the email, Strathon raises
`StrathonPolicyBlocked` before the tool function executes. The agent never
sends the message. Open the dashboard:

- **Traces** shows the full execution, with the blocked span highlighted.
- **Audit** shows a tamper-evident record of the decision: which policy
  matched, what action was taken, and when.

## What's next

- **[Policy Engine](intervention.md)** — all five actions (block, steer,
  throttle, log, alert), allow-list mode, time-based rules, versioning.
- **[CEL Reference](cel-reference.md)** — the policy language, with 20+ examples.
- **[Human Approval Workflows](intervention.md)** — pause high-risk calls for
  operator sign-off in the dashboard or Slack.
- **[Budgets](budgets.md)** — cap model spend and iteration loops per project,
  agent, or model.
- **[Framework guides](frameworks/)** — setup for all 10 supported frameworks.
- **[Self-Hosting](self-hosting.md)** — production deployment, environment
  variables, scaling.
