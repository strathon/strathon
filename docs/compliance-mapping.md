# Compliance Mapping: NIST AI RMF & EU AI Act

How Strathon helps organizations meet AI governance and regulatory requirements.

This document maps Strathon's capabilities to specific controls in the
NIST AI Risk Management Framework (AI 100-1, AI 600-1) and obligations
in the EU AI Act (Regulation (EU) 2024/1689) for high-risk AI systems.

**Deadline context**: EU AI Act Article 6(2) high-risk obligations take
effect August 2, 2026. NIST AI RMF is voluntary but increasingly
referenced by U.S. federal agencies (FTC, SEC, OCC, CFPB) in
enforcement guidance and procurement requirements.

## EU AI Act — High-Risk AI System Obligations (Articles 9-15)

### Article 9: Risk Management System

Requires a documented, ongoing risk management process covering the
entire AI lifecycle, including identification and evaluation of known
and foreseeable risks.

| Requirement | Strathon implementation |
|---|---|
| Identify and evaluate risks | CEL policy engine evaluates every agent action against configurable risk rules. OWASP Agentic Top 10 templates provide a starting risk taxonomy. |
| Test for appropriate risk measures | Policy simulation endpoint (`POST /v1/policies/simulate`) dry-runs policies against historical spans without affecting production. |
| Ongoing risk management | Continuous policy evaluation on every span at ingest. Policy evaluation metrics (`match_count`, `last_matched_at`) track enforcement over time. |
| Residual risk documentation | Audit log with tamper-evident hash chain records every policy decision, intervention, and configuration change. |

### Article 10: Data and Data Governance

Requires training, validation, and testing datasets to meet quality
criteria, with documented data provenance and bias detection.

| Requirement | Strathon implementation |
|---|---|
| Data provenance | Full OTel trace capture with span attributes preserves the provenance chain for every agent decision (model, prompt, tool call, result). |
| Bias detection | PII redaction at ingest catches sensitive attributes. Cost attribution endpoint surfaces per-model and per-agent usage patterns that can reveal distribution skew. |

Note: Article 10 primarily applies to model providers. Strathon is a
runtime firewall, not a training platform. The controls above support
deployers documenting their data governance posture for agent runtime data.

### Article 11: Technical Documentation

Requires comprehensive records of system design decisions, data lineage,
testing methodologies, and performance benchmarks.

| Requirement | Strathon implementation |
|---|---|
| System design records | Policy export (`GET /v1/policies/export`) produces a portable snapshot of all active rules, suitable for version-controlled documentation. |
| Performance benchmarks | Span aggregation (`GET /v1/spans/aggregate`) and cost attribution (`GET /v1/costs`) provide performance and cost metrics over any time range. |
| Testing documentation | Policy simulation against historical spans generates testable, auditable evidence of policy behavior. |

### Article 12: Record-Keeping

Mandates automatic event logging to facilitate risk identification
and post-market monitoring. Logs must be proportionate to the intended
purpose and enable traceability.

| Requirement | Strathon implementation |
|---|---|
| Automatic event logging | OTLP protobuf ingest captures every agent span automatically. No manual instrumentation needed — SDK auto-instruments 10 frameworks. |
| Traceability | Trace tree endpoint (`GET /v1/traces/{trace_id}/tree`) reconstructs the full execution graph of any agent session. |
| Tamper-evident records | HMAC-SHA256 hash chain on the audit log. Per-minute Merkle root anchors. Any modification to historical records is cryptographically detectable. |
| Log retention | Configurable per-project retention with automatic partition management (premake 3 months, drop after retention window). |

Article 12 is Strathon's strongest alignment. The audit log satisfies
12(1)'s mandate for automatic event logging that enables tracing the
operation of the AI system throughout its lifecycle.

### Article 13: Transparency and Provision of Information to Deployers

Requires clear instructions for use, covering intended purpose, known
limitations, performance metrics, and required human oversight level.

| Requirement | Strathon implementation |
|---|---|
| Performance metrics | Prometheus `/metrics` endpoint with 16-panel Grafana dashboard template. Span aggregation provides per-agent, per-model, per-tool analytics. |
| System behavior visibility | Agent topology map (`GET /v1/topology`) shows agent-to-tool relationships discovered from trace data. |
| Deployer information | OpenAPI 3.1 spec at `/openapi.json` with Swagger UI and ReDoc. 14 tagged endpoint groups. |

### Article 14: Human Oversight

Requires high-risk AI systems to allow effective human oversight:
human-in-the-loop, human-on-the-loop, or human-in-command capability.

| Requirement | Strathon implementation |
|---|---|
| Human-in-the-loop | Kill-switch halts (`POST /v1/halts`) immediately stop agent execution at project or agent scope. Operators can intervene at any point. |
| Human-on-the-loop | Real-time policy enforcement evaluates every tool call. Alert action triggers webhooks for operator notification. Budget monitor auto-halts agents exceeding cost or iteration thresholds. |
| Human-in-command | Deny-by-default policy mode (allow-list). Only explicitly permitted tool calls proceed. All others are blocked before execution. |
| Override and disable | Halts CRUD API. Budget CRUD API. Policy enable/disable/delete with batch operations. |

Article 14 is Strathon's core value proposition. The combination of
halts, policies, budgets, and deny-by-default mode provides all three
levels of human oversight defined in the Act.

### Article 15: Accuracy, Robustness and Cybersecurity

Requires appropriate levels of accuracy, robustness, and cybersecurity
for the risk level of the AI system.

| Requirement | Strathon implementation |
|---|---|
| Cybersecurity | Argon2id password hashing, SHA-256 API key hashing, HMAC-signed webhooks, per-IP login rate limiting, key rotation with grace period, key expiration with auto-reaper. |
| Robustness | Fail-closed SDK mode (policy check failure blocks tool execution). Head-based sampling with force-keep for critical traces. PII redaction at ingest. |
| Resilience | Deep `/ready` probe checks DB, migrations, partitions, and 5 background tasks. Advisory-lock-guarded workers prevent dual execution. |

## NIST AI RMF (AI 100-1) — Core Functions

### GOVERN: Organizational Risk Culture

| Subcategory | Strathon implementation |
|---|---|
| GV-1.1: Legal/regulatory understanding | This compliance mapping document. Policy templates mapped to OWASP Agentic Top 10 threats. |
| GV-1.3: Risk management level determination | Per-project settings with configurable retention, sampling rates, and PII redaction rules. Projects isolate risk management per deployment context. |
| GV-1.5: Ongoing monitoring and periodic review | Continuous policy evaluation at ingest. Budget monitor runs on periodic ticks. Key reaper checks for expiring credentials. |
| GV-1.6: AI system inventory | Projects CRUD with auto-key mint. Each project represents an inventoried AI system with its own policies, budgets, halts, and API keys. |
| GV-4.3: Organizational practices for managing AI risk | RBAC with 4 fixed roles (owner/admin/operator/viewer). Audit trail for every configuration change. |

### MAP: Risk Context and Identification

| Subcategory | Strathon implementation |
|---|---|
| MP-2.3: Scientific integrity and reproducibility | Tamper-evident audit log with hash chain and Merkle anchors ensures log integrity. Policy versioning captures every rule change. |
| MP-3.4: Risks from third-party entities | Agent topology map shows all agent-to-tool relationships. Tool-level policy enforcement applies to every framework integration (10 frameworks). |
| MP-5.1: Likelihood and magnitude of impact | Policy evaluation metrics (match_count, last_matched_at) quantify how often each risk rule fires. Span aggregation provides error rates and cost per agent. |

### MEASURE: Risk Assessment and Analysis

| Subcategory | Strathon implementation |
|---|---|
| MS-1.1: AI risks based on intended purpose | Policy simulation dry-runs rules against historical data. Cost attribution surfaces per-model and per-agent spend for risk-proportional resource allocation. |
| MS-2.5: AI system trustworthiness | Continuous policy enforcement measures every agent action against the configured trust boundary. Policy conflict detection identifies contradictions in the rule set. |
| MS-2.6: Evaluation of security risks | PII redaction at ingest. Per-key rate limiting. Login rate limiting. Webhook HMAC signing. API key rotation with grace period. |
| MS-2.7: AI system evaluation with domain expert | Policy simulation endpoint enables domain experts to test rule behavior against real traces without production impact. |

### MANAGE: Risk Response and Monitoring

| Subcategory | Strathon implementation |
|---|---|
| MG-1.1: Risk treatment plans | Policies with 5 action types (block/steer/throttle/log/alert) map directly to risk treatment options: avoid (block), mitigate (steer/throttle), accept (log), escalate (alert). |
| MG-2.2: Mechanisms to halt AI systems | Kill-switch halts at project and agent scope. Budget monitor auto-halts on threshold breach. Both create audit trail entries. |
| MG-2.6: Post-deployment monitoring | Prometheus metrics, Grafana dashboard, webhook notifications, budget monitoring, and the agent topology map provide continuous post-deployment visibility. |
| MG-3.1: Post-deployment risk management | Policy export/import enables staging-to-production promotion with version-controlled rule sets. Policy versioning tracks every change. |
| MG-4.1: Post-deployment monitoring, appeal, and override | Halts provide immediate override. Audit log provides the appeal evidence trail. Policy dry-run simulation enables testing changes before deployment. |

## NIST AI 600-1 — Generative AI Profile

| GAI Risk Category | Strathon implementation |
|---|---|
| CBRN Information | Tool-call blocking policies can prevent agents from accessing CBRN-relevant tools or data sources. |
| Confabulation | Trace capture preserves model outputs alongside tool call context, enabling factual verification workflows. |
| Data Privacy | PII redaction at ingest (regex + Luhn-validated credit card detection). Configurable per-project redaction rules. |
| Information Security (Prompt Injection) | CEL policies can match on span attributes to detect prompt injection patterns. OWASP template for prompt injection included. |
| Harmful Bias | Per-agent, per-model cost attribution and span aggregation surface usage distribution patterns. |
| Intellectual Property | Audit log provides tamper-evident records of all agent actions for IP dispute resolution. |

## OWASP Agentic Top 10 Coverage

Strathon ships policy templates mapped to the [OWASP Top 10 for Agentic
Applications 2026](https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/).
These are available via `GET /v1/policy-templates` and can be applied
with a single API call.

| OWASP Threat | Template | Strathon mechanism |
|---|---|---|
| ASI01 Agent Goal Hijack | prompt-injection-detection | CEL policy on span attributes |
| ASI02 Tool Misuse and Exploitation | tool-access-allowlist | Deny-by-default (allow-list mode) |
| ASI03 Identity and Privilege Abuse | (built-in) | Scoped API keys, RBAC, MFA, per-key rate limits |
| ASI04 Insecure Agent-to-Agent Communication | (built-in) | MCP gateway with policy evaluation |
| ASI05 Unsafe Agent Memory Management | (built-in) | Behavioral drift detection (Vigil), halt propagation |
| ASI06 Implicit Trust and Inadequate Verification | iteration-budget-guard, cost-budget-guard | Budgets with auto-halt, approval workflows |
| ASI07 Overwhelming HITL Controls | (built-in) | Multi-party approval, auto-escalation, circuit breakers |
| ASI08 Inadequate Agent Access Controls | tool-access-allowlist | Per-tool policy enforcement, egress proxy, credential scanning |
| ASI09 Insufficient Logging, Monitoring, and Auditing | (built-in) | OTLP ingest + audit log + Prometheus metrics |
| ASI10 Rogue Agents | (built-in) | Vigil drift detection, heartbeat monitoring, kill switches |

## ISO/IEC 42001:2023 Alignment

NIST published an official crosswalk mapping AI RMF subcategories to
ISO 42001 clauses. Organizations using Strathon's NIST AI RMF alignment
(documented above) can reference this crosswalk to map their Strathon
controls to ISO 42001 certification requirements.

Key ISO 42001 clauses covered by Strathon:

- **Clause 6.1.2 (AI risk assessment)**: Policy engine + simulation + evaluation metrics.
- **Clause 8.4 (AI system lifecycle)**: Trace capture, policy versioning, audit log.
- **Clause 9.1 (Monitoring, measurement, analysis)**: Prometheus metrics, span aggregation, topology map.
- **Clause 10.1 (Continual improvement)**: Policy export/import for staged rollout, evaluation metrics for rule tuning.

## SOC 2 Trust Service Criteria

| Criteria | Strathon implementation |
|---|---|
| CC6.1: Logical access controls | RBAC (4 roles), API key scopes, per-key rate limiting. |
| CC6.3: Restrict access based on authorization | Scope-based API key auth. Owner/admin/operator/viewer hierarchy. |
| CC7.2: Monitor system components for anomalies | Budget monitor auto-halt, policy match metrics, Prometheus /metrics. |
| CC8.1: Change management | Policy versioning, audit log, tamper-evident hash chain. |

## Summary

Strathon provides runtime enforcement and observability controls that
directly address the operational requirements of EU AI Act Articles 9-15,
NIST AI RMF GOVERN/MAP/MEASURE/MANAGE functions, and the NIST AI 600-1
Generative AI Profile. It does not replace organizational governance
(policies, training, risk committees) but provides the technical
infrastructure that makes compliance demonstrable and auditable.

For organizations preparing for the August 2, 2026 EU AI Act deadline,
Strathon's audit log, policy engine, kill-switches, and budget controls
provide the Article 12 (record-keeping) and Article 14 (human oversight)
capabilities that are the most scrutinized during conformity assessment.
