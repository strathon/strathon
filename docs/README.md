# Documentation

New to Strathon? Start with the **[Getting Started guide](getting-started.md)** —
it takes you from zero to a running firewall blocking a real agent action.

| Doc | Covers |
|-----|--------|
| [getting-started.md](getting-started.md) | Install, connect an agent, write your first policy, see it block a call |
| [concepts.md](concepts.md) | The mental model: spans, traces, policies, the seven actions, inline enforcement, audit log |
| [scope.md](scope.md) | What the three enforcement layers do and don't do, and what's roadmap vs shipped |
| [intervention.md](intervention.md) | CEL policies, the seven actions (block/steer/throttle/log/alert/require_approval/allow), allow-list mode, time-based rules, policy versioning, halts, budgets, webhooks |
| [analytics.md](analytics.md) | Trace list, trace tree, span aggregation |
| [spans.md](spans.md) | Span search, attribute filtering, partitioned storage |
| [audit.md](audit.md) | Tamper-evident audit log, hash chain, SCIM filters, Merkle anchors |
| [api_keys.md](api_keys.md) | Capability-scoped API keys, rotation, scopes reference |
| [projects.md](projects.md) | Multi-project management, CRUD, auto-key minting |
| [budgets.md](budgets.md) | Cost and iteration budgets, auto-halt |
| [redaction.md](redaction.md) | PII redaction at ingest |
| [retention.md](retention.md) | Trace retention, per-project configuration |
| [sampling.md](sampling.md) | Head-based sampling, force-keep rules |
| [metrics.md](metrics.md) | Prometheus metrics, health endpoints |
| [self-hosting.md](self-hosting.md) | Docker, env vars, Postgres setup |
| [scaling.md](scaling.md) | Horizontal scaling, PgBouncer, read replicas |
| [rbac.md](rbac.md) | Role-based access control, 4 roles, auth methods |
| [cel-reference.md](cel-reference.md) | CEL policy language reference, 20+ examples |
| [compliance-mapping.md](compliance-mapping.md) | NIST AI RMF and EU AI Act evidence mapping |
| [mcp.md](mcp.md) | MCP security gateway, tool-call policy enforcement |
| [egress.md](egress.md) | Egress proxy, outbound request policy enforcement |
| [troubleshooting.md](troubleshooting.md) | Common issues and FAQ |
| [frameworks/](frameworks/) | Per-framework integration guides (10 frameworks) |
