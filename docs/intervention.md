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
        "strathon.tool.input": '{"to": "rival@competitor.com", ...}',
        "gen_ai.usage.total_tokens": 5000,
        # ... all OTel attrs available
    },
}
```

In CEL you access attrs with map indexing because the keys contain dots:

```
attrs["gen_ai.tool.name"] == "send_email" &&
attrs["strathon.tool.input"].contains("@competitor.com")
```

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

## Creating a policy

```bash
curl -X POST http://localhost:4318/v1/policies \
  -H "Content-Type: application/json" \
  -d '{
    "name": "block_competitor_email",
    "description": "Prevent agents from emailing competitor addresses",
    "match_expression": "attrs[\"gen_ai.tool.name\"] == \"send_email\" && attrs[\"strathon.tool.input\"].contains(\"@competitor.com\")",
    "action": "block",
    "action_config": {"message": "Cannot email a competitor address."},
    "priority": 100
  }'
```

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
