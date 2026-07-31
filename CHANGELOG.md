# Changelog

All notable changes to Strathon are documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

> Versions 0.1.0, 1.0.0, and 1.0.1 were early development builds published
> during initial setup; this changelog starts at v1.1.0.
>
> Versions 1.1.0 through 1.3.0 were pre-release iterations that hardened the
> enforcement surfaces, the audit chain, and the deployment story.

## [Unreleased]


## [1.4.0] - 2026-07-31

A hardening release: every enforcement surface now fails closed when it cannot
confirm a call is allowed, and several ways a request could crash the receiver
are closed. No breaking changes.

### Added
- The list tables (traces, spans, agents) can be sorted by column, and traces
  and spans have a per-table menu to show or hide columns. Sorting is applied
  by the receiver across the full result set, not just the loaded page, so it
  stays correct as results paginate.
- The SDK client accepts a custom span exporter. It still defaults to OTLP over
  HTTP, but a deployment can now send spans over OTLP gRPC, to a console
  exporter for local debugging, or to any other OpenTelemetry exporter.

### Changed
- The dashboard keyboard shortcuts follow common conventions: toggle sidebar is
  `Cmd+B` and settings is `Cmd+,`.
- The production compose file refuses to boot on the built-in development keys,
  so a real deployment must supply its own HMAC key, encryption key, and
  password pepper.
- The published receiver image runs as a non-root user and installs from a
  hash-pinned lockfile, so a substituted dependency fails the hash check. A
  deployment that bind-mounts a data directory may need to adjust its ownership.

### Fixed
- A deleted policy lingered in the ingest cache until its TTL expired, because
  the cache was dropped before the delete transaction committed. It is now
  dropped after the commit, and never on a rollback.
- A created or revoked webhook signing key desynced from the database when the
  request rolled back: a created secret survived for a row that never
  persisted, and a revoked key's secret vanished while its row stayed active.
  Both changes now apply only after the transaction commits.
- The `strathon` CLI read no results against populated data on several list
  commands, and `audit list` called a route that does not exist. Every list
  command now reads the response the same way, and the route is corrected.

### Security
- **An enforcing policy that could not be evaluated was treated as no-match.**
  A `block`, `require_approval`, or `throttle` policy whose expression errored
  (most often a missing attribute) returned no match, so the surface let the
  call through. Evaluation failure on an enforcing policy now fails closed, and
  the full policy set is evaluated before failing so one policy's error no
  longer discards another policy's match on the same span. **If you rely on an
  enforcing policy, verify that a call it should block returns blocked.**
- **The MCP gateway and egress proxy could fail open.** The gateway honored a
  caller-supplied flag that turned off its fail-closed behavior and did not
  validate the upstream URL against the SSRF guard; the egress proxy started in
  an allow posture and admitted traffic until its first policy refresh
  succeeded. The gateway now always fails closed and validates the URL, and the
  egress proxy blocks until a refresh has loaded the project's posture. **If you
  run the egress proxy, verify that a known-blocked request returns 403.**
- Every SDK adapter now fails closed on a receiver-unreachable error when
  `fail_closed` is set, where previously only a policy block was re-raised, so a
  receiver outage could silently drop enforcement. Policy evaluation also sees
  the full tool arguments, so a padded argument can no longer push content past
  a policy's view.
- Project endpoints and project-scoped member operations crossed tenant
  boundaries: several read or acted on projects outside the caller's
  organization. They are now confined to the caller's organization, and
  creating an API key or deleting a project requires re-authentication.
- The audit anchor chain was forgeable by truncation, and its full listing was
  readable by any project member. The chain now resists truncation, the full
  listing is restricted to an instance admin, and a project reader sees only
  per-project tamper-evidence status.
- A NUL byte in a string field or an out-of-range integer reached Postgres as
  an unhandled error that returned a 500 and dropped the request. Both are now
  rejected or sanitized at the request boundary. Redaction and credential
  scanning also require `google-re2`, so a missing engine reports not-ready
  rather than silently falling back to a backtracking regex engine.
- The four unbounded list endpoints (approvals, budgets, costs, halts) now cap
  their page size, so a caller can no longer request an arbitrarily large page.

## [1.3.0] - 2026-07-25

Drops Python 3.10 and removes the SDK `proxy` extra;
see **Removed** before upgrading.

### Added
- Interventions are now visible end to end. The SDK marks every span where
  it blocked, throttled, steered, or held a call for approval, the receiver
  persists that marker, and the dashboard reads it. Enforcement that had
  actually happened could previously render as though nothing had.
- Sampling never drops an intervened span. A blocked or steered call is
  audit-critical and survives any sample rate.
- `STRATHON_REQUIRE_SECURITY_KEYS` makes a missing HMAC key, encryption key,
  or password pepper a hard boot failure instead of a warning, naming the
  variables that are missing. Cloud deployments always enforce it;
  zero-config local trials are unchanged.
- `ops/provision_app_role.sql` creates the least-privilege database role
  that row-level security binds to, for deployments that run the receiver as
  a non-owner.
- A [failure model](docs/failure-model.md) page states what every component
  does when something breaks, alongside new coverage of key loss and
  recovery, the browser security headers, and API key rotation.

### Changed
- Dependency floors across all three packages now state the versions that
  are tested. `cryptography` moves from `44.0` to `49.0.0` and `protobuf`
  from `6.31.1` to `7.35.1` in the receiver.
- A bulk policy delete now records each deleted policy in the audit event,
  not just its id, so the audit log can answer what a bulk delete removed.
- Deleting a policy no longer writes a version snapshot. `policy_versions`
  cascades on delete, so the snapshot was removed by the same statement that
  created it; deletions are recorded in the audit log instead.
- Cloud mode no longer seeds a `default` project. The first user to register
  became owner of a shared project and bypassed provisioning entirely.
- The EU AI Act mapping is scoped to the evidence Strathon produces, and the
  audit append-only claim now names the triggers that enforce it.
- Key rotation keeps the previous key valid for 72 hours. The dashboard and
  the CLI previously said it was invalidated immediately.

### Removed
- **Python 3.10 support.** `strathon` and `strathon-cli` now require 3.11.
  3.10 reaches end of life on 31 October 2026. Python 3.14 is tested; the
  `crewai` extra does not install there, because CrewAI declares
  `requires-python <3.14`.
- **The SDK `proxy` extra.** The egress addon ships as its own image and
  does not use the SDK. Install `mitmproxy` and `cel-python` directly, as
  [docs/egress.md](docs/egress.md) describes.
- Two RBAC scope constants that appeared in neither the role map nor the
  known-scope set. Any endpoint declaring them would have locked out every
  role.

### Fixed
- Audit rows carrying an IP address failed verification. psycopg3 decodes
  Postgres `INET` columns into `ipaddress` objects on read while the value
  hashed at insert was a plain string, so the chain reported tampering that
  had never happened.
- The three security-key warnings had never been printed on a default boot.
  With auto-migrate enabled, Alembic's `fileConfig` disabled the loggers
  that already existed, dropping every log line after the migration step.
- Switching projects did not persist on the default deployment. The cookie
  keyed its `Secure` flag off `NODE_ENV`, but a self-hosted image is built
  with `NODE_ENV=production` and usually served over HTTP, where browsers
  silently drop `Secure` cookies.
- Losing the encryption key could lock a user out permanently. MFA
  verification raised while decrypting the TOTP secret, and the exception
  killed the backup-code branch below it, so even hashed backup codes could
  not get in.
- Retried OTLP batches inflated policy match counts. The same span evaluated
  against the same policy recorded a fresh `policy_matches` row each time.
- Row-level security on `api_keys` could never match. Migration 021 used the
  tenant predicate from the data tables, but `api_keys` is read during
  authentication, before the tenant setting exists.
- Agent risk scoring and policy suggestions disagreed about which tools
  count as sensitive. The heuristic was defined twice and the two copies had
  drifted; both now read one definition.
- The OWASP threat table mapped every risk to policy template IDs that do
  not exist, so none of them could be created.
- HMAC key rotation was documented as preserving verification of existing
  rows. Verification only ever uses the current key, so a key change resets
  the chain, and the docs now say so.
- The egress proxy deployment documented an image that would have blocked
  every request: it copied two receiver modules where the addon needs three,
  and installed neither the policy module nor the CEL engine.

### Security
- **An unevaluable policy set was treated as a clean no-match.** The
  evaluator returned an empty list both when nothing matched and when every
  policy failed to evaluate, and the egress proxy and MCP gateway both read
  the second as permission to allow. An egress proxy running without the CEL
  engine installed passed all traffic while appearing healthy. Evaluation
  failure is now distinct from no-match and both surfaces fail closed. **If
  you run the egress proxy or the MCP gateway, confirm `cel-python` is
  present in that environment and verify that a known-blocked request
  returns 403.**
- A bad or revoked API key left the SDK silently unenforcing. An HTTP 401 or
  403 from the receiver was caught as a network error and logged at debug,
  so policy enforcement and halt sync went inactive while the agent kept
  running. Both enforcers now warn once per distinct error, naming the
  consequence.
- Deleting a project did not delete access to it. Its API keys still
  authenticated, a session could still reach it by pinning `X-Project-Id`,
  and the budget monitor kept evaluating its budgets, writing halts and
  firing alerts for a project the operator had removed.
- `/ready` is unauthenticated and interpolated raw exception text into its
  response, where a driver error can carry the connection string or
  filesystem paths. It also returned the applied schema revision,
  fingerprinting the build. It now returns the decision and logs the detail.
- Registration, MFA verification, and password-reset requests were the only
  unauthenticated endpoints with no rate limit. MFA verification now caps
  guesses per token and then destroys it; a six-digit code with unlimited
  attempts against a renewable token is brute-forceable by anyone who
  already has the password.
- The dashboard sends a strict Content-Security-Policy with a per-request
  nonce and `strict-dynamic`, so an injected script carries no nonce and the
  browser refuses it. HSTS and `upgrade-insecure-requests` are sent only
  over TLS.
- `next` moves to 16.2.11, clearing four advisories. The transitive `sharp`
  and `postcss` dependencies are overridden to releases that carry the
  libvips fixes and the postcss source-map path traversal fix
  (GHSA-r28c-9q8g-f849).
- Bulk data export now requires its own scope rather than riding on read
  access. Browsing spans and dumping the dataset are different capabilities.

## [1.2.3] - 2026-06-28

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

[Unreleased]: https://github.com/strathon/strathon/compare/v1.4.0...HEAD
[1.4.0]: https://github.com/strathon/strathon/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/strathon/strathon/compare/v1.2.3...v1.3.0
[1.2.3]: https://github.com/strathon/strathon/compare/v1.2.2...v1.2.3
[1.2.2]: https://github.com/strathon/strathon/compare/v1.2.1...v1.2.2
[1.2.1]: https://github.com/strathon/strathon/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/strathon/strathon/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/strathon/strathon/releases/tag/v1.1.0
