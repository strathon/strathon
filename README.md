# Strathon

Open-source agent observability and runtime intervention. Single-Docker self-host. OpenTelemetry-native.

## What Strathon does

Strathon captures traces from AI agents, visualizes their topology (tool calls, sub-agent spawns, decision branches), and intervenes at runtime (budget enforcement, loop detection, persistent halt state across process restarts).

Differentiators:

- **Topology view + runtime intervention in one tool.** Most observability tools are read-only. Strathon lets you pause, resume, and halt running agents from the dashboard.
- **Persistent halt state.** Circuit breakers survive process restarts via a write-ahead log. When your agent service restarts, runaway behavior does not resume.
- **Cross-process budget rollup.** Sub-agent costs in other processes count against the parent budget ceiling. Trace-ID-based async aggregation. Works across containers and services.
- **Native integrations.** OpenAI Agents SDK, Claude Agent SDK, LangChain, CrewAI, AutoGen, raw OpenAI and Anthropic clients.
- **GitHub commit integration.** Link traces to git SHAs. See which deploy regressed your agent's behavior.
- **PII redaction default-on.** Regulated-industry positioning out of the box.
- **Single Docker image.** Postgres plus one Strathon container. No Redis, no ClickHouse, no S3.

## Quick start

Requires Docker.

```bash
git clone https://github.com/strathon/strathon.git
cd strathon
docker compose up
```

On first run, the migrations create the schema and seed a development API
key. The receiver prints a banner at startup with the key value and a
quick-test command:

```
============================================================
  Strathon receiver ready
============================================================
  Endpoint:   http://localhost:4318
  Dev API key (rotate before production!):
      stra_dev_local_default_project_do_not_use_in_production

  Quick test:
      curl -H "Authorization: Bearer stra_dev_..." \
           http://localhost:4318/v1/policies

  Run a demo:
      python examples/intervention_demo.py

  To rotate this key, see docs/api_keys.md
============================================================
```

Run one of the framework intervention demos against it:

```bash
pip install strathon langchain cel-python      # or crewai / openai-agents
python examples/intervention_demo.py
```

The demo installs a CEL policy, attempts to send an email to a competitor
address, and the receiver blocks it before the tool body runs.

### Configuration

Copy `.env.example` to `.env` and edit if you need to override defaults
(Postgres password, log level, log format, sampling rate, retention). All
env vars are documented in the example file. The dashboard isn't shipped
yet — it lands in a future release.

## Architecture

Four components over OpenTelemetry:

```
SDK (in user agent) ──► Receiver (FastAPI) ──► Postgres ──► Dashboard (Next.js)
                              ▲                     ▲
                              │                     │
                              └─ intervention ──────┘
                                 sync API
```

Two-namespace schema: `gen_ai.*` for OpenTelemetry-standard attributes, `strathon.agent.*` for agent topology, budget rollup, intervention state, loop signatures.

See `docs/architecture.md` for details.

## Status

v0 in active development. Target launch: end of June 2026.

## License

MIT. See `LICENSE`.
