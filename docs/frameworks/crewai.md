# CrewAI Integration

Strathon enforces the full action set on CrewAI, including interactive
approval: tool calls are evaluated at the tool-invoke boundary before they
run, while the event bus supplies task, delegation, and collaboration traces.
Two mechanisms, one `instrument()` call.

> **Enforcement scope:** full. Tool-invoke wrapping enforces all seven
> actions, including `require_approval` with an interactive pause until an
> operator approves or denies. The event listener provides observability
> alongside it.


## Installation

```bash
pip install "strathon[crewai]"
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

Flag tool calls made by agents that have delegation enabled: delegation is a
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

- Observability rides CrewAI's event bus (`BaseEventListener`,
  `ToolUsageStartedEvent`). Enforcement wraps the tool-invoke boundary
  (a class-level patch installed at instrument time), which is what lets
  CrewAI support the full action set including interactive approval.
- Works with CrewAI 0.80+.
- Multi-agent crews create a single trace with per-agent spans.

## Learn More

- [Policy Engine docs](https://getstrathon.com/docs/intervention)
- [CEL Reference](https://getstrathon.com/docs/cel-reference)
- [CrewAI documentation](https://docs.crewai.com/)
