# Documentation

New to Strathon? Start with the **[Getting Started guide](getting-started.md)**:
it takes you from zero to a running firewall blocking a real agent action.

| Doc | Covers |
|-----|--------|
| [Getting started](getting-started.md) | Install, connect an agent, write your first policy, see it block a call |
| [Core concepts](concepts.md) | The mental model: spans, traces, policies, the seven actions, inline enforcement, audit log |
| [Scope & limitations](scope.md) | What the three enforcement layers do and don't do, and what's roadmap vs shipped |
| [Runtime intervention](intervention.md) | CEL policies, the seven actions (block/steer/throttle/log/alert/require_approval/allow), allow-list mode, time-based rules, policy versioning, halts, budgets, webhooks |
| [Human approval](approvals.md) | The require_approval action, N-of-M multi-party sign-off, fail-closed expiry with notifications, approvals at the MCP boundary |
| [Analytics](analytics.md) | Trace list, trace tree, span aggregation, behavioral drift detection (Vigil) |
| [Spans](spans.md) | Span search, attribute filtering, partitioned storage |
| [Audit log](audit.md) | Tamper-evident audit log, hash chain, SCIM filters, Merkle anchors |
| [API keys](api_keys.md) | Capability-scoped API keys, rotation, scopes reference |
| [Projects](projects.md) | Multi-project management, CRUD, auto-key minting |
| [Budgets](budgets.md) | Cost and iteration budgets, auto-halt, circuit breakers |
| [PII redaction](redaction.md) | PII redaction at ingest |
| [Retention](retention.md) | Trace retention, per-project configuration |
| [Sampling](sampling.md) | Head-based sampling, force-keep rules |
| [Metrics](metrics.md) | Prometheus metrics, health endpoints |
| [Self-hosting](self-hosting.md) | Docker, env vars, Postgres setup |
| [Scaling](scaling.md) | Horizontal scaling, PgBouncer, read replicas |
| [RBAC](rbac.md) | Role-based access control, 4 roles, auth methods |
| [CEL reference](cel-reference.md) | CEL policy language reference, 20+ examples |
| [CLI reference](cli.md) | The strathon command line: policies, traces, halts, budgets, audit, approvals, and more |
| [Compliance mapping](compliance-mapping.md) | NIST AI RMF and EU AI Act evidence mapping |
| [OWASP Agentic Top 10](owasp.md) | Coverage across the ten agentic threats, the policy templates, and where the tool-call boundary is strongest |
| [MCP gateway](mcp.md) | MCP security gateway, tool-call policy enforcement |
| [Egress proxy](egress.md) | Egress proxy, outbound request policy enforcement |
| [Locking egress](egress-locking.md) | Make the proxy mandatory via network isolation |
| [Troubleshooting](troubleshooting.md) | Common issues and FAQ |
| [frameworks/](frameworks/) | Per-framework integration guides (10 frameworks) |
