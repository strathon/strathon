# Runtime Intervention

Most agent observability tools are passive. Strathon is not.

Strathon evaluates policies *before* an agent's tool calls or LLM calls execute.
A policy can log, alert, steer, or **block** the action. This is the difference
between "you discover the problem in the dashboard tomorrow" and "the bad email
never leaves your servers."

## Policy expressions: CEL

Policies are written in [Common Expression Language (CEL)][cel] — the same
language Kubernetes admission policies, Envoy filters, gRPC interceptors, and
gcloud IAM conditions use. CEL is:

- Non-Turing-complete, with guaranteed termination
- Side-effect free, safe to evaluate untrusted input
- Microsecond-fast
- Recognized across the cloud-native ecosystem

Expressions are evaluated against a span context:

```python
{
    "name":  "langgraph.tool.send_email",
    "attrs": {
        "gen_ai.tool.name": "send_email",
        "strathon.tool.args": '{"to": "rival@competitor.com", ...}',
        "gen_ai.usage.total_tokens": 5000,
        # ... all OTel attrs available
    },
}
```

In CEL you access attrs with map indexing because the keys contain dots:

```
attrs["gen_ai.tool.name"] == "send_email" &&
attrs["strathon.tool.args"].contains("@competitor.com")
```

### Standard attributes set by Strathon instrumentations

These attributes are set consistently across all three framework integrations
(LangGraph, CrewAI, OpenAI Agents SDK), so policies written against them are
portable:

| Attribute                       | Description                                  |
|---------------------------------|----------------------------------------------|
| `strathon.framework`            | One of `langgraph`, `crewai`, `agents`       |
| `strathon.tool.name`            | The tool's name (also mirrored to `gen_ai.tool.name`) |
| `strathon.tool.args`            | The tool's input arguments, as a JSON string |
| `gen_ai.tool.name`              | Standard OTel attribute, same as `strathon.tool.name` |
| `gen_ai.request.model`          | The model name (on LLM spans)                |
| `gen_ai.usage.total_tokens`     | Token count (on LLM spans)                   |

### Writing safe policy expressions

CEL raises an error when you index a map with a key that doesn't exist. To
write policies that work safely across span types where some attributes may
be missing, guard accesses with `has()`:

```
has(attrs["gen_ai.tool.name"]) &&
attrs["gen_ai.tool.name"] == "send_email" &&
attrs["strathon.tool.args"].contains("@competitor.com")
```

In practice, when a policy errors out the SDK treats it as a non-match (the
action is allowed), so missing-key errors fail safe — but they generate log
noise and reduce policy effectiveness. Use `has()` for any attribute that
isn't guaranteed to exist on every span.

## Actions

A policy has one of four actions:

| Action  | What happens                                                                                                  | Where it runs |
|---------|---------------------------------------------------------------------------------------------------------------|---------------|
| `log`   | Annotate the matching span with `strathon.policy.*` attributes. Passive.                                      | Server        |
| `alert` | Fire a webhook (`action_config.webhook_url`). Async, doesn't block the agent.                                 | Server        |
| `block` | SDK raises `StrathonPolicyBlocked` before the tool/LLM call executes. Agent sees an error and adapts.         | SDK (client)  |
| `steer` | SDK returns a corrective string (`action_config.replacement`) in place of real output. Agent self-corrects.   | SDK (client)  |

`block` and `steer` actually prevent the action — these are SDK-side because
by the time a span reaches the server, the action has already happened.

## Scoping with `applies_to`

By default a policy is evaluated against every span. To scope a policy to
specific spans, set `applies_to` to a list of dot-segment-path tokens:

```json
{
  "name": "redact_pii_from_tool_calls",
  "match_expression": "attrs[\"strathon.tool.args\"].contains(\"@\")",
  "action": "steer",
  "applies_to": ["langgraph.tool", "crewai.tool"]
}
```

A token matches a span name if and only if it aligns with one or more
whole dot-separated segments of the name. So `"tool"` matches
`"langgraph.tool.send_email"` because `tool` is one of the segments,
but does **not** match `"langgraph.pool.X"` — the substring `tool`
appearing inside `pool` is not a segment-aligned match. Multi-segment
tokens work too: `"langgraph.tool"` matches LangGraph tool spans but
not CrewAI tool spans.

The list is OR'd: a span matches the filter if any token aligns.
Empty list (the default) means "every span." The same rule runs on
both the server (at ingest time, gating `policy_matches` rows) and
in the SDK (gating in-process block/steer enforcement) so the two
layers always agree on which spans a policy applies to.

## Creating a policy

```bash
curl -X POST http://localhost:4318/v1/policies \
  -H "Authorization: Bearer $STRATHON_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "block_competitor_email",
    "description": "Prevent agents from emailing competitor addresses",
    "match_expression": "attrs[\"gen_ai.tool.name\"] == \"send_email\" && attrs[\"strathon.tool.args\"].contains(\"@competitor.com\")",
    "action": "block",
    "action_config": {"message": "Cannot email a competitor address."},
    "applies_to": ["langgraph.tool", "crewai.tool", "agents.tool"],
    "priority": 100
  }'
```

## Framework support

Strathon enforces policies at the tool-call boundary on every supported
framework. Block enforcement is zero-code-change: `instrument(client)` is
all the user does. Steer enforcement requires one extra line per tool
(or per agent) — replacing a tool's return value is a bigger contract
change than refusing to call, so we ask the user to opt in explicitly.

| Framework            | Block (auto)        | Steer (opt-in) | Steer opt-in call                                       |
|----------------------|---------------------|----------------|---------------------------------------------------------|
| LangGraph (LangChain)| `instrument(client)`| Per-tool       | `from strathon.policy import enforce_steer; enforce_steer(tool, client)` |
| CrewAI               | `instrument(client)`| Per-tool       | `enforce_steer(tool, client)` (same helper)             |
| OpenAI Agents SDK    | `instrument(client)`| Per-agent      | `from strathon.instrumentation.openai_agents import attach_strathon_guardrails; attach_strathon_guardrails(agent, client)` |

CrewAI's `instrument(client)` already enforces *both* block and steer
globally (its class patch sits at the right boundary for both), so the
per-tool `enforce_steer` call on CrewAI is optional — it's there for
parity with LangGraph and for users who want explicit per-tool control.

## Enforcing in your agent code

For most framework integrations, just instrument the client. The SDK pulls
policies from the receiver every 30 seconds in the background:

```python
from strathon import Client
from strathon.instrumentation.langgraph import instrument

client = Client(api_key="...", endpoint="http://localhost:4318")
handler = instrument(client)

# Use the handler in your graph invocations
graph.invoke(input, config={"callbacks": [handler]})
# Tool calls that match a block policy raise StrathonPolicyBlocked.
```

For custom tools or non-instrumented call paths, call `check_policy` directly:

```python
from strathon.policy import StrathonPolicyBlocked

decision = client.check_policy({
    "name": "myapp.action.send_money",
    "attrs": {"amount": 50000, "destination": "..."},
})
if decision.is_block:
    raise StrathonPolicyBlocked(decision.message)
if decision.is_steer:
    return decision.replacement
```

## CRUD endpoints

| Method | Path                       | Purpose                                  |
|--------|----------------------------|------------------------------------------|
| GET    | /v1/policies               | List policies (SDKs poll this every 30s) |
| POST   | /v1/policies               | Create a policy                          |
| GET    | /v1/policies/{id}          | Read one                                 |
| PATCH  | /v1/policies/{id}          | Partial update (enable/disable, change action, etc.) |
| DELETE | /v1/policies/{id}          | Delete                                   |

## Audit trail

Every match is recorded in the `policy_matches` table with the policy id,
trace id, span id, action, outcome, and timestamp. Query it directly in
Postgres or expose it through the dashboard.

[cel]: https://cel.dev/
