# Changelog

All notable changes to Strathon are documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.1.0] - 2026-06-XX

First public release.

### Added

**Policy Engine**
- CEL policy evaluation with 6 actions: block, steer, throttle, log, alert, require_approval
- Allow-list mode, time-based rules, policy versioning
- 12 OWASP-mapped templates (including MINJA memory-poisoning)
- Shadow mode, batch operations, conflict detection
- Policy export/import, evaluation metrics, automated suggestions

**Human Oversight (EU AI Act Article 14)**
- Approval workflow with multi-party support (N-of-M)
- Kill-switches (halts) with project or agent scope
- SDK poll-based approval with configurable timeout

**10 Framework Integrations**
- LangGraph, CrewAI, OpenAI Agents SDK, OpenAI, Anthropic, LangChain, AutoGen, Claude Agent SDK, Pydantic AI, Google ADK
- Fail-closed mode, per-key scoped auth

**Data Plane**
- OTLP protobuf ingest (4MB cap), RANGE-partitioned spans
- Full-text search, span aggregation, trace tree, PII redaction
- Head-based sampling, configurable retention

**Compliance + Intelligence**
- EU AI Act compliance evidence export (Articles 9-15, 19)
- Agent inventory with risk scoring, cost forecasting
- Incident detection with Article 73 metadata, agent topology map

**Integrations**
- Slack (Block Kit, interactive approve/deny buttons)
- Discord (rich embeds, interactive components)
- GitHub App (webhooks, commit tracking, issues from incidents)
- Notification channels CRUD with event filters

**Identity + Access**
- RBAC (owner/admin/operator/viewer), Argon2id auth, TOTP MFA
- API key rotation, expiration, IP allowlisting
- Column-level encryption (Fernet AES-256), auth failure logging

**Audit**
- Tamper-evident HMAC-SHA256 hash chain, DB-level immutability
- SCIM 2.0 queries, Merkle root anchors

**CLI**
- 12 command groups, 30+ subcommands, Rich formatting

### Security
- protobuf >= 6.31.1 (CVE-2025-4565)
- Webhook SSRF + replay protection
- google-re2 for PII regexes, Pydantic extra="forbid"
- hmac.compare_digest on all secrets
- Postgres RLS, /docs gating, CORS allowlist
- Pre-commit secret scanning
