# Core Concepts

This page explains the mental model behind Strathon: what the pieces are and
how they fit together. If you just want to get running, see
[Getting Started](getting-started.md). For the full policy reference, see
[Runtime Intervention](intervention.md).

## The one-sentence model

Strathon sits between your agent and everything it calls — tools and model
APIs — and evaluates each call against your policies *before* it executes.
That "before" is the whole point: a blocked call never runs.

## Spans

Every action your agent takes is captured as a **span**: a structured record
of one operation. A model request is a span. A tool call is a span. A graph
node entering and exiting is a span. Spans nest: a single agent run produces a
**trace**, a tree of spans showing exactly what happened in what order.

Spans follow the OpenTelemetry data model, with attributes like
`gen_ai.request.model`, `gen_ai.tool.name`, and `strathon.tool.args`. Policies
are written against these attributes. Spans are stored in a partitioned
Postgres table: see [Spans](spans.md) for the storage detail.

## Policies

A **policy** is a rule that matches certain spans and decides what to do with
them. It has three parts:

- A **match expression** in [CEL](cel-reference.md): a boolean test against
  span attributes. For example, `attrs["gen_ai.tool.name"] == "send_email"`.
- An **action**: what to do when the expression matches (see below).
- A **priority**: when several policies match the same call, the
  higher-priority one runs first and its action wins.

Policies have versions. Editing a policy creates a new version; the audit log
records who changed what and when.

## Actions

There are seven actions. The critical distinction is *where* each one takes
effect: and therefore whether it can actually stop a call.

| Action | What it does | Affects the call? |
|--------|--------------|-------------------|
| `log` | Annotates the span. Passive record-keeping. | No (server-side) |
| `alert` | Fires a signed webhook (retried, dead-lettered on failure). | No (server-side) |
| `block` | Raises `StrathonPolicyBlocked` before the call executes. | **Yes** (at the enforcement surface) |
| `steer` | Returns a corrective string in place of the real output, so the agent self-corrects. | **Yes** (at the enforcement surface) |
| `throttle` | Consults a per-policy token bucket; calls over the cap are denied with a retry hint. | **Yes** (at the enforcement surface) |
| `require_approval` | Holds the call for human approval. Pauses for an operator decision where the surface can wait; otherwise fails closed (blocks). Never silently allows. | **Yes** (at the enforcement surface) |
| `allow` | Admits the call and short-circuits lower-priority policies. Used for carve-outs and required for allow-list mode. | **Yes** (at the enforcement surface) |

The five call-affecting actions (`block`, `steer`, `throttle`,
`require_approval`, `allow`) run *before* the tool or model call executes, at
whichever of Strathon's three enforcement layers the call passes through: the
in-process SDK for instrumented frameworks, the [MCP gateway](mcp.md) for
MCP-routed tool calls, and the [egress proxy](egress.md) for raw outbound
HTTP. That is what makes Strathon a firewall rather than an observability
tool: enforcement is inline, not after the fact.

## Enforcement is inline

Because enforcement runs in your agent's process before each call, a `block`
genuinely prevents the action — the tool function body never runs. That is what
makes Strathon a firewall rather than an observability tool: enforcement is
inline, not after the fact.

Policies are evaluated against a short-lived local cache, so a brief receiver
outage does not add latency. By default the SDK is **fail-open**: if it cannot
reach the receiver to refresh policy state, agents keep running on last-known
state rather than stalling; this favors availability. For security-critical
agents you can opt into **fail-closed** mode, where an unverifiable state stops
the call instead. See the Reliability section of the README for the trade-off
and configuration.

## Shadow mode

A policy can be set to **shadow** status. In shadow mode the policy is
evaluated and its decision is recorded, but the call is *not* actually blocked.
This lets you see what a policy *would* do against real traffic before turning
it on. Promoting a tested shadow policy to `enabled` is the safe path to
enforcement.

## Allow-list mode (default-deny)

By default, anything not matched by a `block`/`steer`/`throttle` policy is
allowed. In **allow-list mode** the default flips: calls are denied unless an
`allow` policy explicitly admits them. This is the stricter posture for
high-security environments. See [Runtime Intervention](intervention.md).

## The audit log

Every enforcement decision and every operator action is written to a
**tamper-evident audit log**: an append-only, hash-chained record. You can
prove the trail was not modified after the fact. This is what turns "we have a
firewall" into "we can demonstrate to an auditor exactly what was enforced and
when." See [Audit](audit.md).

## How the pieces fit

1. Your agent makes a tool or model call.
2. The SDK intercepts it and evaluates matching policies (using a short-lived
   local cache of policies fetched from the receiver).
3. If a call-affecting action matches, the SDK enforces it inline; the call
   is blocked, steered, throttled, or admitted: before execution.
4. The decision and the span are sent to the receiver, recorded as a trace,
   and written to the audit log.
5. You review traces, decisions, and spend in the dashboard.

## Where to go next

- **[Getting Started](getting-started.md)**: run all of this end-to-end.
- **[Runtime Intervention](intervention.md)**: the full policy reference.
- **[CEL Reference](cel-reference.md)**: the match-expression language.
- **[Troubleshooting](troubleshooting.md)**: common issues and answers.
