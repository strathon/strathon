# Changelog

All notable changes to Strathon are documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

> Versions 0.1.0, 1.0.0, and 1.0.1 were early development builds published
> during initial setup. v1.1.0 is the first release intended for general use,
> and this changelog starts there.

## [Unreleased]

### Fixed

- **Shadow policies no longer enforce.** The SDK dropped the `shadow` field
  when parsing `/v1/policies`, so a shadow `block` policy blocked live
  traffic in-process; the MCP gateway and egress proxy had the same gap.
  All three enforcement surfaces now skip shadow policies; server-side
  recording of shadow decisions is unchanged.
- `instrument()` now raises `ValueError` on an unknown framework name
  (as its docstring already stated) instead of logging a warning and
  silently skipping — a typo'd name meant requested enforcement that
  never attached.
- Fail-closed approval messages on the LangGraph and Pydantic AI surfaces
  referenced a decorator that does not exist; they now point at
  `enforce_steer`.
- The `claude-agent` and `all` extras now install `claude-agent-sdk`,
  the package the Claude Agent SDK integration instruments.


## [1.1.0] - 2026-06-06

### Added

**Policy engine**
- CEL policy engine with seven actions (block, steer, throttle, log, alert, require_approval, allow)
- Allow-list mode, time-based rules, policy versioning, shadow mode
- OWASP-mapped policy templates

**Human oversight**
- Multi-party (N-of-M) approval workflows
- Kill-switch halts and SDK poll-based approval

**Data plane**
- OTLP protobuf ingest, RANGE-partitioned spans
- Span search, full-text search, aggregation, trace tree
- PII redaction, sampling, retention

**Integrations & auth**
- 10 framework integrations, fail-closed mode, per-key scoped auth
- RBAC, Argon2id auth, TOTP MFA, API key rotation
- Tamper-evident HMAC-SHA256 audit log with Merkle anchors

**CLI**
- Create policies from OWASP templates (`--template`), from plain English (`--from-english`), or by bulk import (`policies import`)
- Dry-run a policy against recent traces (`policies test`)

**Compliance**
- EU AI Act evidence export (Articles 9-15, 19)
- Agent inventory with risk scoring, agent topology map
- OWASP Agentic Applications 2026 mapping (ASI01-ASI10) across docs and templates

**Deployment & docs**
- Self-host with Docker Compose, including PgBouncer connection pooling
- Per-framework integration guides for all 10 supported frameworks
- Enterprise scaling guide (horizontal scaling, PgBouncer, read replicas)
- Published to PyPI: `pip install strathon`

[Unreleased]: https://github.com/strathon/strathon/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/strathon/strathon/releases/tag/v1.1.0
