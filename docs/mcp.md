# MCP Security Gateway

The MCP Security Gateway puts Strathon policy enforcement between an AI agent
and an [MCP](https://modelcontextprotocol.io) (Model Context Protocol) server.
MCP itself has no authorization layer: any tool an MCP server exposes, a
connected agent can call. The gateway adds one.

```
Agent  ->  Strathon MCP Gateway  ->  upstream MCP server
           (policy evaluation)
```

## How it fits

The MCP gateway is one of Strathon's three enforcement layers. It governs the
tool calls an agent makes **over MCP**, the same way the in-process SDK governs
tools the agent calls directly and the egress proxy governs raw outbound HTTP.
Same policies, three boundaries.

It enforces by default once your agents route through it; a project's policies
apply with no extra opt-in, and the default posture admits any call no policy
blocks (it enforces the rules you write). To harden a project to default-deny,
blocking any `tools/call` not explicitly allowed, set its
`intervention_default_action` to `block`; see
[intervention.md](intervention.md) on allow-list mode.

The gateway is the relevant layer whenever an agent reaches tools through an MCP
server. If none of your agents use MCP, this layer simply has no traffic to act
on; the other two layers still enforce.

Every MCP request is evaluated against the **same enabled policies** the rest
of Strathon uses; the gateway calls the identical policy primitive the trace
ingest path does, so an MCP `tools/call` is judged exactly like a tool call
captured from an instrumented framework. There is no separate ruleset to keep
in sync.

## Endpoint

```
POST /v1/mcp/proxy
```

Scope: `traces:write` (proxying live tool calls is the same trust level as
writing spans).

Request body:

| Field            | Type        | Default | Meaning                                                    |
|------------------|-------------|---------|------------------------------------------------------------|
| `upstream_url`   | string      | —       | The real MCP server to forward allowed requests to.        |
| `request`        | object      | —       | The MCP JSON-RPC request to evaluate and proxy.            |
| `blocked_tools`  | string[]    | `[]`    | Tool names to hard-block regardless of policy.             |
| `scan_responses` | bool        | `true`  | Redact leaked credentials from upstream responses.         |
| `fail_open`      | bool        | `false` | Allow a `tools/call` if policy evaluation fails. See below.|

Example:

```bash
curl -X POST http://localhost:4318/v1/mcp/proxy \
  -H "Authorization: Bearer $STRATHON_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "upstream_url": "http://localhost:3000/mcp",
    "request": {
      "jsonrpc": "2.0", "id": 1, "method": "tools/call",
      "params": {"name": "send_email", "arguments": {"to": "x@y.com"}}
    }
  }'
```

## What gets enforced

| MCP method        | Behavior                                                                 |
|-------------------|--------------------------------------------------------------------------|
| `tools/call`      | Evaluated against policies. **Main enforcement point.**                  |
| `tools/list`      | Forwarded; tools in `blocked_tools` are filtered out of the result.      |
| `resources/read`  | Forwarded; response scanned for leaked credentials (if `scan_responses`).|
| anything else     | Forwarded unchanged.                                                     |

For `tools/call`, the matching policy's action determines the outcome:

- **block** -> the call is rejected with a JSON-RPC error (code `-32040`); the
  upstream server is never contacted.
- **require_approval** -> rejected with code `-32041`; the agent is told the
  call needs human approval.
- **throttle** -> the gateway is the choke point and refuses rate-limited calls
  directly with a throttle error (code `-32043`, including `retry_after`); the
  upstream server is not contacted.
- **steer** -> the gateway returns the policy's `replacement` as the tool result
  without forwarding to the upstream server, so the agent receives the steered
  value instead of the real tool output.
- **allow / log** -> forwarded to the upstream server.

The policy match is evaluated against a span-shaped context where the span
name is the tool name and the attributes carry `gen_ai.tool.name` and the
JSON-encoded `strathon.tool.args`. So a policy like

```
attrs["gen_ai.tool.name"] == "send_email"
```

matches MCP calls and framework-captured calls identically.

## Fail-closed by default

If policy evaluation cannot complete (for example, the policy set fails to
load), a `tools/call` is **blocked**, not allowed. A security gateway that
allowed traffic whenever its policy engine was unavailable would be a
bypass-by-denial-of-service: an attacker who could disrupt evaluation would
disable enforcement.

If you would rather prioritize availability over strict enforcement, set
`"fail_open": true` in the request body. When a degraded (failed-evaluation)
allow occurs, the gateway logs it server-side (the evaluation error is logged
with a stack trace); note that this is a log record, not an entry in the
tamper-evident audit log.

## Credential scanning on responses

When `scan_responses` is true (the default), text content in `tools/call` and
`resources/read` responses is scanned with the same credential-pattern engine
used elsewhere in Strathon, and any matched secrets are redacted before the
response reaches the agent. This catches an upstream MCP tool that returns a
leaked key or token in its output.

## Relationship to the SDK

The MCP gateway and the SDK instrumentation are complementary:

- The **SDK** enforces in-process at the tool-call boundary inside an agent
  framework (LangGraph, CrewAI, etc.) and can substitute tool results
  (full steer/throttle).
- The **MCP gateway** enforces at the network boundary in front of an MCP
  server, for agents that reach tools over MCP rather than through an
  instrumented framework.

Use whichever matches how your agent reaches its tools; they can be used
together.
