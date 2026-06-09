# CrewAI Integration

Strathon integrates with CrewAI via its event listener system, hooking
into the CrewAI event bus to capture tool use, task delegation, and
agent collaboration events.

## Installation

```bash
pip install strathon[crewai]
```

## Setup

```python
from strathon import Client, instrument

client = Client(
    api_key="stra_...",
    endpoint="http://localhost:4318",
)
instrument(client, frameworks=["crewai"])
```

Every `Crew.kickoff()` invocation is traced automatically. Tool calls
pass through the policy engine before executing.

## What Gets Captured

- **Agent actions**: which agent is active, task assignment
- **Tool calls**: tool name, arguments, return value
- **Task lifecycle**: start, delegation, completion
- **LLM calls**: model, tokens, latency per agent
- **Crew coordination**: agent-to-agent delegation events

Each crew run creates a trace with nested spans for every agent step.

## Example Policy

Block any tool call that sends outbound HTTP requests:

```cel
attrs["gen_ai.tool.name"] in ["http_request", "web_request", "api_call"]
```

Flag tool calls made by agents that have delegation enabled — delegation is a
common source of loops and cost overruns in CrewAI, so you may want stricter
policies on those agents:

```cel
attrs["strathon.agent.allow_delegation"] == true
```

## Approval Workflow

Require human approval for financial actions:

```cel
attrs["gen_ai.tool.name"] == "transfer_funds"
```

The crew pauses until an operator approves in the dashboard or Slack.

## Notes

- CrewAI's `BaseEventListener` fires `ToolUsageStartedEvent` before
  tool execution, which Strathon intercepts for policy enforcement.
- Works with CrewAI 0.80+.
- Multi-agent crews create a single trace with per-agent spans.

## Learn More

- [Policy Engine docs](https://getstrathon.com/docs/intervention)
- [CEL Reference](https://getstrathon.com/docs/cel-reference)
- [CrewAI documentation](https://docs.crewai.com/)
