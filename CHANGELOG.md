# Changelog

All notable changes to Strathon are documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

> Versions 0.1.0, 1.0.0, and 1.0.1 were early development builds published
> during initial setup. v1.1.0 is the first release intended for general use,
> and this changelog starts there.

## [Unreleased]

## [1.2.3] - 2026-06-27

### Added
- Approvals can now require multiple approvers (N-of-M). A policy may demand a
  quorum of distinct approvers before an action proceeds, rather than a single
  approval.
- Pending approvals that time out now send an expiry notification to the
  configured Slack and Discord channels when they are auto-denied, so a lapsed
  approval is no longer silent.

### Changed
- Clarified the per-framework enforcement guidance: instrumenting a client
  alone does not enforce on every surface, and the docs now state which
  surfaces each framework supports.
- Raised the floor for the optional `claude-agent-sdk` integration to `0.1.81`.

### Fixed
- Corrected CEL attribute keys in the policy templates, the plain-English
  policy generator, and the reference docs. They referenced attribute names
  the engine does not emit, so the affected policies could never match; they
  now use the emitted keys and match as documented.
- Per-call token cost is now recorded on spans, so cost-based log and alert
  policies can read it.

## [1.2.2] - 2026-06-20

### Added
- The overview gains an agent-health card showing liveness and risk; it shares
  the Agents page data so the two surfaces always agree.
- Span kinds are now color-coded across the trace waterfall and the spans
  list from a shared palette, with blocked spans highlighted, so each kind
  looks the same in both views.
- Trace and span views now surface token and cost detail where it exists: the
  selected span in the waterfall shows input/output tokens and cost, and the
  spans list shows those columns when present and links each row to its trace.

### Changed
- The receiver's OpenTelemetry floor was raised from `1.29.0` to `1.42.0`.
- The anchor model documentation now explains how single-event verification
  works today (recomputing the keyed row hash) and states plainly that anchors
  are unsigned Merkle roots, with signed, independently verifiable anchoring
  noted as a planned enhancement.

### Fixed
- The budgets page showed empty forecast and headroom and an empty
  spend-by-agent chart because it read fields the API did not return. It now
  shows end-of-month forecast and remaining headroom (from
  `/v1/costs/forecast`) and a per-agent spend chart (from `/v1/costs`), and the
  overview spend trend reads the same series.
- The audit log showed a static integrity label. It now verifies against the
  receiver: the header reflects the anchor state from `/v1/audit/anchors`
  (chain anchored with the latest anchor time, or not yet anchored), and each
  row can be inspected to recompute its entry hash via
  `/v1/audit/events/{id}/verify`, showing a pass or fail verdict.
- The compliance evidence export was not wired to a request. It now downloads
  evidence from the receiver as JSON or SARIF.
- Sparklines drew from a single data point; they now render only when at least
  two points exist.

### Security
- Hardened the audit anchor Merkle tree against forgeability. The previous
  construction could let two different event sequences produce the same root,
  weakening tamper detection; the tree now follows the standard RFC 6962 /
  RFC 9162 construction, which closes this. No production anchors used the old
  construction, so no re-anchoring is required.

## [1.2.1] - 2026-06-17

### Fixed
- The trace detail page (`/traces/{id}`) crashed on load. It read the span
  count (a number) before the span array and tried to spread it; it now reads
  the span array directly with a guard, so the waterfall, flame, and graph
  views render correctly.
- Approval cards showed "unknown agent" even when the triggering span carried
  an agent name. Approvals now record and return the agent (`gen_ai.agent.name`)
  so the real agent is shown.
- Dashboard search placeholders rendered a literal escape sequence instead of
  an ellipsis character.

## [1.2.0] - 2026-06-16

### Added
- Broader PII detection: crypto wallet addresses, IBAN (mod-97 validated),
  IPv6, US ITIN, and Indian Aadhaar (Verhoeff validated) join the existing
  email, phone, SSN, credit-card, and IP recognizers.
- Broader credential detection across modern AI providers (Hugging Face, Groq,
  xAI, Cohere, Perplexity, Replicate) and SaaS platforms (Vercel, Supabase,
  Cloudflare, DigitalOcean, Shopify, Datadog, Notion, Linear, Sentry,
  Atlassian, Square).
- `allow` is selectable when creating policies from the CLI and the dashboard,
  not just the API.
- The SDK ships a PEP 561 `py.typed` marker: type checkers now consume the
  SDK's annotations in downstream projects.
- Python 3.13 is tested in CI and officially supported by the SDK.
- Notification channels: route approvals, incidents, policy interventions,
  and budget alerts to Slack, Discord, a generic webhook, or GitHub issues,
  configurable from the dashboard with per-channel event selection.
- Dashboard: an enforcement-mix overview, per-agent budget spend, a usage
  section (metered usage in cloud mode), and an activity log on the trace
  detail view.
- `strathon-admin reset-password` CLI for break-glass account recovery: an
  operator with database access can reset a locked-out owner's password (and
  optionally clear their MFA) without a running receiver.
- Users can change their own password and display name from the dashboard
  (`POST /v1/auth/change-password`, `PATCH /v1/auth/me`).

### Changed
- The dashboard now targets Node 24 (current LTS). Updated dependencies
  across the SDK and dashboard to current releases.
- Relicensed the receiver and CLI from MIT to Apache 2.0. The project is now
  uniformly Apache 2.0 with NOTICE files and the canonical license text.
- The seeded development API key is now opt-in (`STRATHON_SEED_DEV_KEY=true`)
  and is never seeded in cloud mode. The local `docker compose` setup opts in
  so the quickstart works out of the box; production does not.
- Documentation overhauled end to end: framework guides now state
  per-surface enforcement scope, reference pages are cross-linked, and the
  README, PyPI pages, and CLI examples are verified against the shipped
  code.
- Ownership transfer is now a two-step, consent-based flow: the owner sends a
  request to an existing admin, who accepts or declines it from a card under
  Members before any roles change. Previously the swap was immediate.
- Changing your password now requires a current MFA code when the account has
  MFA enabled, matching the verification required for other sensitive actions.
- Sensitive member actions (reset password, disable MFA, change role, remove)
  now require the caller to outrank the target: an admin can manage operators
  and viewers but not a peer admin or the owner. Enforced server-side and
  reflected in the dashboard.

### Fixed
- Human-in-the-loop approvals now work end to end. The SDK posts to a new
  POST /v1/approvals endpoint to open a pending approval when a require_approval
  policy matches; the held call resumes or is denied on the human decision.
  Approval requests can be routed to a notification channel with approve/deny
  links.
- **Shadow policies no longer enforce.** The SDK dropped the `shadow` field
  when parsing `/v1/policies`, so a shadow `block` policy blocked live
  traffic in-process; the MCP gateway and egress proxy had the same gap.
  All three enforcement surfaces now skip shadow policies; server-side
  recording of shadow decisions is unchanged.
- `instrument()` raises `ValueError` on an unknown framework name instead of
  logging a warning and silently skipping, so a typo no longer leaves
  enforcement unattached.
- The dashboard's password-reset proxy pointed at the wrong receiver paths,
  so self-service reset returned a 404. It now targets the correct endpoints.
- Slack interactive approve/deny buttons resolve the approval in-process,
  authenticated by the verified Slack request signature, instead of an
  internal HTTP call that relied on the seeded development key.
- Admin-generated temporary passwords (member reset, the admin reset endpoint,
  and the recovery CLI) now always satisfy the password policy, so they no
  longer fail validation on the member's first sign-in.
- Dashboard data correctness: the approvals filter, the blocked statistic
  (which counted shadow-mode hits), trace rollups now derived from spans, and
  timestamps in the viewer's local time zone.
- Various dashboard UI fixes.
- Agent-health alerts (missed heartbeat, behavioral drift, SDK integrity
  violation) are now selectable notification events. They were dispatched but
  not in the subscribable set, so channels with an event filter dropped them;
  they now route to Slack, Discord, webhook, and GitHub with proper formatting.
- The Docker Compose files now pass the security keys
  (`STRATHON_AUDIT_HMAC_KEY`, `STRATHON_ENCRYPTION_KEY`,
  `STRATHON_PASSWORD_PEPPER`) through from `.env`, so a self-hosted deployment
  can set real values.
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

[Unreleased]: https://github.com/strathon/strathon/compare/v1.2.3...HEAD
[1.2.3]: https://github.com/strathon/strathon/compare/v1.2.2...v1.2.3
[1.2.2]: https://github.com/strathon/strathon/compare/v1.2.1...v1.2.2
[1.2.1]: https://github.com/strathon/strathon/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/strathon/strathon/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/strathon/strathon/releases/tag/v1.1.0
