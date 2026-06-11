# Google ADK Integration

Strathon integrates with Google's Agent Development Kit (ADK) via its
`BasePlugin` system. This is a first-class integration — no monkey-patching.

## Installation

```bash
pip install strathon[google-adk]
```

## Setup

```python
from strathon import Client, instrument

client = Client(
    api_key="stra_...",
    endpoint="http://localhost:4318",
)
instrument(client, frameworks=["google_adk"])
```

The integration registers a `StrathonFirewallPlugin` that evaluates
policies on every tool call and LLM interaction within ADK agents.

## What Gets Captured

- **Agent runs**: agent name, model, configuration
- **Tool calls**: tool name, arguments, return value
- **LLM calls**: Gemini model, tokens, latency
- **Agent events**: lifecycle events from the ADK event system

## Example Policy

Block tool calls that access Google Cloud resources in production:

```cel
attrs["gen_ai.tool.name"] == "gcloud_run"
  && attrs["strathon.tool.args"].contains("--project=prod-")
```

Require approval for database mutations:

```cel
attrs["gen_ai.tool.name"] == "bigquery_query"
  && (attrs["strathon.tool.args"].contains("DELETE")
      || attrs["strathon.tool.args"].contains("UPDATE"))
```

## Notes

- Uses `BasePlugin` — Google ADK's official plugin interface.
- Zero monkey-patching. The plugin is registered at instrument time.
- Requires `google-adk>=1.7.0` (installed by the `google-adk` extra).
- 25 tests cover the integration.

## Learn More

- [Policy Engine docs](https://getstrathon.com/docs/intervention)
- [CEL Reference](https://getstrathon.com/docs/cel-reference)
- [Google ADK documentation](https://google.github.io/adk-docs/)
