# Human Approval

Some actions are too consequential to allow automatically and too rare to block
outright — wiring a payment, deleting a table, deploying to production. For these,
Strathon pauses the agent and waits for a human to approve or deny. This is the
human-in-the-loop control the EU AI Act Article 14 calls for, applied at the
tool-call boundary.

## How it works

Approval is a policy **action**. Set a policy's action to `require_approval`, and
any tool call that matches its CEL expression pauses instead of executing. The
SDK polls the receiver and blocks the calling thread until a decision arrives, so
no architectural change is needed in your agent — the tool call simply does not
return until an operator acts (or the request expires).

```cel
attrs["gen_ai.tool.name"] in ["transfer_funds", "delete_record", "deploy"]
```

With the action set to `require_approval`, a matching call creates a pending
approval request. The agent resumes only when an operator approves; on denial the
call is refused the same way a `block` would refuse it.

## Where operators decide

Pending requests surface in the places a team already watches:

- **Dashboard** — an approval card shows the agent, the tool, the arguments, and
  the matched policy, with approve and deny actions.
- **Slack** — an interactive Block Kit message with approve and deny buttons.
- **Discord** — a rich embed with interactive components.

From the command line, operators can list and act on requests directly:

```bash
strathon approvals list --status pending
strathon approvals approve <approval-id>
strathon approvals deny <approval-id>
```

## Multi-party approval (N-of-M)

For the highest-risk actions, a single sign-off may not be enough. Strathon
supports **N-of-M** approval: a request can require, say, two of three named
approvers before the call is admitted. This mirrors the dual-control pattern used
for financial transactions and production changes, where no one person can
unilaterally authorize a sensitive operation.

## Preventing stuck agents

A blocking approval that no one answers would hang an agent indefinitely, so each
request has a timeout. When it elapses, the request fails closed: the call is
refused rather than allowed, and a notification is sent to your configured
channels so the team knows the action was auto-denied. Operators are also
notified the moment a request is raised, so most requests are answered well
inside the window.

Failing closed on an unanswered high-risk call is deliberate: if no one signs off
in time, the sensitive action does not run. This is separate from the SDK's
global fail-open or fail-closed setting, which governs what happens when the
receiver itself is unreachable. See the reliability discussion in
[Runtime Intervention](intervention.md).

## At the MCP boundary

The same action applies when agents reach tools over MCP. In the
[MCP gateway](mcp.md), a `require_approval` verdict rejects the `tools/call` with
JSON-RPC error code `-32041`, telling the agent the call needs human approval
rather than silently allowing or hard-blocking it.

## Approvals vs blocks vs halts

These three controls answer different questions:

- **block** stops a call unconditionally when its policy matches — no human in the
  loop. Use it for actions that should never happen.
- **require_approval** stops a call *conditionally on a human decision*. Use it for
  actions that are sometimes legitimate and warrant review.
- **halts** stop an agent (or the whole project) regardless of what it is doing —
  the operator kill-switch. See [Runtime Intervention](intervention.md).

## See also

- [Runtime Intervention](intervention.md) — the full policy and action reference
- [CEL Reference](cel-reference.md) — writing the match expression
- [Compliance Mapping](compliance-mapping.md) — how approval maps to EU AI Act Article 14
