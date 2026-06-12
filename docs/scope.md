# Scope and Limitations

Strathon is a firewall for AI agents. It enforces policy at the **tool-call
boundary** — the point where an agent's decision becomes a real-world action —
and it does so at three independent layers. This page states plainly what each
layer does today, what it does not do, and what is on the roadmap rather than
shipped. For a security product, being precise about the edges matters more than
sounding comprehensive: the claims below are the ones we will stand behind.

## The three enforcement layers

Strathon enforces at three places an agent's actions can pass through. They are
independent — each works on its own, and they compose when used together.

| Layer | Where it sits | What it sees | Can it be bypassed by the agent? |
|-------|---------------|--------------|----------------------------------|
| **SDK (in-process)** | Inside the agent process, at the framework's tool-call boundary | Tool calls made through an instrumented framework | The SDK runs in the same process as the agent, so it is a cooperating control, not a containment boundary against hostile in-process code. |
| **MCP gateway** | In front of an MCP server | Every `tools/call` an agent routes over MCP | Out-of-band: the agent talks to the gateway, not the tool. It cannot remove the gateway from the path by editing its own code. |
| **Egress proxy** | In front of the agent's outbound HTTP | Arbitrary outbound HTTP, including calls the SDK can't see | Depends on deployment. In explicit-proxy mode the agent honors an `HTTP_PROXY` variable it could, in principle, ignore. Transparent (network-level) deployment closes that gap and is on the roadmap. |

The right mental model: the SDK gives you fine-grained, framework-aware
enforcement for a cooperating agent; the MCP gateway and egress proxy give you
out-of-band boundaries that hold even when in-process enforcement is missing or
sidestepped. Defense in depth, not one control.

## What each layer does today

**SDK.** Evaluates each tool call against your CEL policies before the tool
runs, and can block, steer (substitute the result), throttle, require approval,
log, alert, or allow. Across the supported frameworks the available actions vary
by surface: some frameworks expose a synchronous callback that can block but
cannot pause for approval; see [intervention.md](intervention.md) for the
per-surface matrix. Where a surface cannot fully execute a matched action, it
fails closed.

**MCP gateway.** Evaluates every `tools/call` against the same policies, fails
closed if evaluation can't complete, and scans tool responses for leaked
credentials. See [mcp.md](mcp.md).

**Egress proxy.** Evaluates outbound HTTP against your policies, scans request
and response bodies for credential patterns, and blocks or redacts. See
[egress.md](egress.md).

## Credential handling: detection today, injection later

Strathon's credential handling today is **detection and redaction**: it
recognizes secrets (50+ patterns) in tool arguments and in request/response
bodies, and it blocks the leak or masks the secret. This is reactive; it
catches a secret that the agent is holding and about to send.

It is **not** credential injection. In an injection model the secret never
reaches the agent at all: the gateway holds it and substitutes it on the wire
toward the legitimate destination, so the agent cannot leak a key it never had.
Injection is a stronger, preventive posture and it is on the roadmap; it is not
shipped today. We would rather name the difference than let "credential
security" imply more than detection.

## Egress: explicit today, transparent later

The egress proxy today runs in **explicit mode**; the agent's process is
pointed at it via `HTTP_PROXY`/`HTTPS_PROXY`. This enforces on all traffic that
honors those variables, which is the right defense-in-depth layer for a
cooperating agent. It does not, on its own, contain an agent that deliberately
ignores the proxy. **Transparent mode** (routing the agent's traffic at the
network or namespace level so it cannot opt out) closes that gap and is on the
roadmap. Until it ships, we describe the egress proxy as recommended
defense-in-depth, not an un-bypassable boundary.

## Attack classes not solvable at the tool-call boundary

Strathon governs the outbound-action leg of an agent well. Some attack classes
are not solvable at the single-call boundary by any stateless filter, and we say
so rather than imply otherwise:

- **Data-flow exfiltration.** When sensitive data read earlier is smuggled
  inside an otherwise-valid argument (for example, encoded into a URL on an
  allowed domain), the individual call looks legitimate. Catching this reliably
  requires data-flow provenance (taint tracking), which is on the roadmap.
- **Poisoned tool output and context attacks.** Instructions injected into a
  tool's response, or into the tool list during a protocol handshake, can
  influence an agent without producing a malicious call of their own. These need
  response sanitization and input filtering, not only call-time enforcement.
- **Memory poisoning.** A malicious instruction planted in an agent's long-term
  memory produces a later call that carries no in-band signal. Defending this is
  a memory-integrity and training-time problem.
- **Aggregate / economic abuse.** Each call can be legitimate while the volume
  is the attack. Strathon's budgets and drift detection address this through cost
  and rate accounting rather than per-call policy.

The honest framing from the security community applies: an agent that combines
access to private data, exposure to untrusted content, and an outbound channel
is structurally exploitable, and the most reliable mitigation is to remove one
of those legs by design. Strathon governs the outbound-action leg well; it is
most effective as part of a layered design, not as a single guarantee.

## Summary

Shipped today: three-layer enforcement (SDK, MCP gateway, egress proxy), CEL
policies, credential detection and redaction, fail-closed behavior, and an
explicit-mode egress proxy. On the roadmap: credential injection, transparent
egress, and data-flow provenance for the cross-call attack classes above. When
those ship, this page will say so; until then, it does not.
