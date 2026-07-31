# Failure Model

What Strathon does when something breaks. A firewall's behavior under failure is
part of its contract, so this page states it once, for every component, in one
place.

The short version: **a surface that stands between an agent and an action it has
not taken yet fails closed. A surface that records something already done fails
open.** Everything below follows from that.

---

## Three behaviors, not two

Most discussions of this reduce to fail-open versus fail-closed. Strathon has a
third, and confusing it with the other two is the usual source of trouble.

**Fail closed** — refuse the action. Used where a failure would otherwise let
something through that policy was supposed to stop. The egress proxy, the MCP
gateway, and audit writes all work this way.

**Fail open** — carry on, and record. Used where refusing protects nothing and
destroys evidence. Span ingest works this way: the tool call already happened,
and dropping the span would lose the record of it along with the diagnosis of
why policy evaluation failed.

**Fail static** — keep the last known good state. Used where both of the above
are wrong. When the SDK cannot refresh policy from the receiver, it keeps the
policies it already has rather than clearing them. Clearing would silently
disarm every agent; blocking would turn a receiver outage into a fleet outage.

### The test

When you are unsure which applies to a new component, ask: *if this is broken
and nobody notices, what does an attacker get?*

- Enforcement broken and failing open — everything. Must fail closed.
- Ingest broken and failing closed — nothing gained, telemetry lost. Fail open.
- Policy cache cleared on a network blip — every agent unenforced. Fail static.

---

## Per component

| Component | What breaks | Behavior | Configurable |
|---|---|---|---|
| [SDK policy enforcer](intervention.md) | Receiver unreachable on refresh | Fail static, then fail open | Yes — `fail_closed` |
| [SDK halt enforcer](intervention.md) | Receiver unreachable on refresh | Fail static, then fail open | Yes — `fail_closed` |
| Policy evaluation, any surface | An enforcing policy cannot be evaluated (or every policy fails) | Raises rather than returning no-match | No |
| [Egress proxy](egress.md) | Evaluation raises, CEL engine absent | Fail closed — block, record | No |
| [MCP gateway](mcp.md) | Evaluation raises, CEL engine absent | Fail closed — reject the `tools/call` | No |
| Span ingest | Evaluation raises | Fail open — record the span unmatched | No |
| [Audit write](audit.md) | Insert fails | Fail closed — the mutation rolls back | No |
| [Approval request](approvals.md) | Nobody answers before the timeout | Fail closed — auto-denied, notified | Timeout length |
| `require_approval` on a synchronous surface | Surface cannot pause | Fail closed — block, record | No |
| Credential redaction | Independent of policy evaluation | Unaffected | No |

Two rows deserve expanding.

### Policy evaluation raising

The evaluator returns the policies that matched a call. Evaluating a policy has
three outcomes, not two: it matched, it did not match, or it could not be
evaluated at all — a compile error, or the common case of a runtime error such
as a referenced attribute being absent from the span. Collapsing that third
outcome into "did not match" is what let an unevaluable policy read as an allow:
a proxy running without the CEL engine installed passed all traffic while
appearing healthy, and a policy whose expression referenced a missing attribute
silently stopped enforcing.

An evaluation error is now distinct from a no-match, and how it is handled
depends on what the policy would have done. If a policy that *enforces* — block,
require_approval, or throttle — cannot be evaluated, the evaluator raises: that
policy might have been the decisive block, so the call must fail closed rather
than be allowed on the assumption it did not match. An error on a policy that
cannot turn an allow into a block — log, alert, steer — is logged and skipped, so
one malformed non-enforcing policy does not fail the whole call closed. A total
failure, where every candidate policy errors, also raises, because evaluation is
then broken rather than simply unmatched.

Evaluation runs against the full tool arguments. The arguments are truncated
only in the copy written to a span for storage, never in the value a policy sees,
so an oversized payload cannot push malicious content past a length limit and out
of a content policy's view.

Each caller then does what its role requires. The egress proxy and MCP gateway
let it reach their fail-closed handlers and block. Span ingest catches it and
records the span unmatched, because that path is writing down something that has
already happened.

### The SDK, in detail

Policies evaluate inside your agent process against locally cached state, so a
brief receiver outage adds no latency to tool calls.

On a failed refresh the enforcer keeps the policies it last fetched. That is the
fail-static part, and it is the behavior for a short outage: enforcement
continues against the last known good rules.

What happens when the outage is not short is your choice. By default the SDK is
fail-open — agents keep running rather than stalling. For agents where an
unreachable receiver should stop tool calls instead:

```python
client = Client(
    api_key="stra_...",
    endpoint="http://localhost:4318",
    fail_closed=True,
    fail_closed_max_staleness_sec=60.0,  # default; tune to your refresh cadence
)
```

Once enabled, both the policy and halt enforcers raise
`StrathonReceiverUnreachable` at the tool boundary when the last successful
refresh is older than the threshold. The 60s default sits well above the 1s halt
refresh and 30s policy refresh, so a brief blip never trips it.

This is opt-in deliberately. A fleet of agents raising on every tool call is
itself an outage, so it belongs in environments that would rather stop than
continue unverified. Fail-open prioritizes uptime; fail-closed prioritizes
containment. Choose per deployment.

---

## What is *not* a failure mode

Two settings use the same words and mean something else. Both have caused
confusion.

**`intervention_default_action`** decides what happens to a call that matched no
policy. `allow` is the default and makes Strathon a deny-list: policies name
what to stop. `block` makes it an allow-list: everything is refused unless a
policy permits it. This is a working system making a decision, not a broken one
— see [Runtime Intervention](intervention.md).

**`require_approval` on a synchronous surface** blocks because the surface
cannot pause, not because anything failed. LangGraph, LangChain, and Pydantic AI
expose observer callbacks that cannot hold a call open while an operator
decides, so a matched approval policy blocks and records instead of waiting. The
enforcement matrix in [Runtime Intervention](intervention.md) names which
surfaces can pause.

---

## The invariant

One rule holds across every surface: **a surface that matches a policy it cannot
fully execute fails closed — blocked and recorded, never silently allowed.**

Completeness is a matrix, not a per-surface property: every action against every
surface. When the policy-evaluation gap above was found in the egress proxy, the
same gap was present in the MCP gateway, and it was found by re-checking the
matrix rather than by the original report.

## See also

- [Runtime Intervention](intervention.md) — actions, the enforcement matrix, and
  the full SDK reliability contract
- [Scope and Limitations](scope.md) — what Strathon does not defend against
- [Egress Proxy](egress.md) and [MCP Gateway](mcp.md) — the two surfaces that
  always fail closed
- [Audit Log](audit.md) — the transactional fail-closed write contract
