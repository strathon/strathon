# OWASP Agentic Top 10 Coverage

How Strathon maps to the [OWASP Top 10 for Agentic Applications
2026](https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/),
the threat model Strathon's design is anchored on.

The OWASP Top 10 for Agentic Applications (ASI01–ASI10) is the reference
taxonomy for how agentic systems get attacked: goal hijacking, tool misuse,
privilege abuse, supply-chain compromise, and the rest. Strathon ships policy
templates mapped to these threats, available via `GET /v1/policy-templates`
and applicable with a single API call.

A note on honesty up front: not every item on this list is fully solvable at
the tool-call boundary, and the table below says so where it matters. Some
threats (memory and context poisoning, cascading failures) are addressed
through detection and containment rather than a single pre-execution verdict,
and a few have legs that live outside any runtime firewall. The [Scope &
Limitations](scope.md) page is the long version of where those lines fall.

## Threat coverage

| OWASP Threat | Template | Strathon mechanism |
|---|---|---|
| ASI01 Agent Goal Hijack | prompt-injection-detection | CEL policy on span attributes |
| ASI02 Tool Misuse and Exploitation | tool-access-allowlist | Deny-by-default (allow-list mode) |
| ASI03 Identity and Privilege Abuse | (built-in) | Scoped API keys, RBAC, MFA, per-key rate limits |
| ASI04 Agentic Supply Chain Vulnerabilities | (built-in) | MCP gateway with policy evaluation, egress proxy, credential scanning |
| ASI05 Unexpected Code Execution | tool-access-allowlist | Block/allow-list on shell, code, and SQL tools; approval before code execution |
| ASI06 Memory and Context Poisoning | (built-in) | Behavioral drift detection (Vigil), halt propagation, content redaction |
| ASI07 Insecure Inter-Agent Communication | (built-in) | MCP gateway policy evaluation, fail-closed enforcement |
| ASI08 Cascading Failures | iteration-budget-guard, cost-budget-guard | Budgets with auto-halt, circuit breakers, kill switches, halt propagation |
| ASI09 Human-Agent Trust Exploitation | (built-in) | Human approval workflows, tamper-evident audit log, SARIF export |
| ASI10 Rogue Agents | (built-in) | Vigil drift detection, heartbeat monitoring, kill switches |

## Where the boundary is strongest

ASI02, ASI05, and ASI07 are the threats a tool-call firewall addresses most
directly: each is a question about whether a specific tool call should run, and
that is exactly the decision Strathon makes before the call executes. Deny-by-default
allow-lists, argument-level CEL rules, and approval gates give you a hard stop
at the moment an agent's decision becomes a real action.

ASI03 and ASI09 are addressed through identity and accountability primitives
(scoped keys, RBAC, multi-party approval, a tamper-evident audit trail) rather
than per-call matching alone.

## Where it is detection and containment, not prevention

ASI06 (memory and context poisoning) and ASI08 (cascading failures) produce
calls that carry no in-band signal at the moment they fire. Strathon addresses
these through behavioral drift detection, budgets with auto-halt, circuit
breakers, and halt propagation, which surface and contain the effect rather
than blocking a single malicious call. This is real coverage, but it is a
different mechanism than pre-execution enforcement, and worth understanding as
such.

## Applying the templates

Each template above maps to a ready-made policy. List them with `GET
/v1/policy-templates`, then apply one to a project with a single call, or
create the policy in the dashboard from the templates picker. Templates are a
starting point: tune the CEL match and action to your own tools and risk
tolerance before relying on them.

## Related

- [Runtime intervention](intervention.md): the enforcement engine these templates drive
- [Scope and limitations](scope.md): what Strathon does and does not cover, in full
- [EU AI Act & NIST mapping](compliance-mapping.md): regulatory framework alignment
