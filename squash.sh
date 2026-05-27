#!/bin/bash
# Strathon commit squash: 110 commits → 25 clean commits
# Run from ~/strathon
# Creates backup branch, then orphan branch with clean history

set -e

echo "=== Step 1: Backup current history ==="
git branch -D backup-full-history 2>/dev/null || true
git branch backup-full-history main
echo "Backup saved as 'backup-full-history'"

echo ""
echo "=== Step 2: Remove files that shouldn't be public ==="
rm -f dashboard/AGENTS.md dashboard/CLAUDE.md
echo "Removed AGENTS.md and CLAUDE.md"

echo ""
echo "=== Step 3: Create orphan branch ==="
git checkout --orphan clean-main
git rm -rf . > /dev/null 2>&1
git checkout backup-full-history -- .
git reset HEAD . > /dev/null 2>&1

# Remove the debug files from staging too
rm -f dashboard/AGENTS.md dashboard/CLAUDE.md

echo ""
echo "=== Step 4: Making 25 clean commits ==="

# ---- Commit 1: Foundation ----
git add \
  .gitignore \
  LICENSE \
  LICENSING.md \
  Makefile \
  .env.example \
  receiver/alembic.ini \
  receiver/alembic/env.py \
  receiver/alembic/versions/001_initial_schema.py \
  receiver/alembic/versions/002_*.py \
  receiver/alembic/versions/003_*.py \
  receiver/alembic/versions/004_*.py \
  receiver/alembic/versions/005_*.py \
  receiver/alembic/versions/006_*.py \
  receiver/alembic/versions/007_*.py \
  receiver/config.py \
  receiver/database.py \
  receiver/main.py \
  receiver/models/ \
  receiver/requirements.txt \
  receiver/.env.example \
  2>/dev/null || true
git commit -m "feat: core receiver with FastAPI, PostgreSQL, and OTLP ingest

FastAPI application with async PostgreSQL (SQLAlchemy 2.0, psycopg3).
OTLP protobuf trace ingest at /v1/traces. Database schema with projects,
users, API keys, sessions, policies, spans (RANGE partitioned by month),
traces, audit events, webhooks, project settings, halt state.

Alembic migrations 001-007. Configurable via environment variables.
Auto-creates default project on startup."

# ---- Commit 2: Policy engine ----
git add \
  receiver/api/policies.py \
  receiver/api/traces.py \
  receiver/api/spans.py \
  receiver/api/health.py \
  receiver/api/analytics.py \
  receiver/api/topology.py \
  receiver/api/_deps.py \
  receiver/api/__init__.py \
  receiver/repositories/ \
  receiver/protobuf/ \
  receiver/sampling.py \
  receiver/redaction.py \
  receiver/rate_limit.py \
  receiver/metrics.py \
  2>/dev/null || true
git commit -m "feat: CEL policy engine with 6 enforcement actions

CEL (Common Expression Language) policy evaluation on every span.
Actions: block, steer, throttle, log, alert, require_approval.
Policy priority ordering, batch operations, conflict detection.
Spans API with full-text search, faceted filtering, aggregation.
Trace tree reconstruction, topology map, analytics endpoints.
PII redaction with google-re2 regex engine.
Prometheus metrics, health checks, configurable sampling."

# ---- Commit 3: Policies advanced ----
git add \
  receiver/api/policy_suggestions.py \
  receiver/api/policy_templates.py \
  receiver/api/simulate.py \
  receiver/alembic/versions/008_*.py \
  receiver/alembic/versions/009_*.py \
  receiver/alembic/versions/010_*.py \
  receiver/alembic/versions/011_*.py \
  receiver/alembic/versions/012_*.py \
  receiver/alembic/versions/013_*.py \
  receiver/alembic/versions/014_*.py \
  2>/dev/null || true
git commit -m "feat: policy templates, suggestions, simulation, and versioning

12 OWASP-mapped policy templates (one-click creation).
Automated policy suggestions based on trace analysis.
Dry-run simulation engine for testing policies against historical data.
Policy version history with full diff support.
Spans RANGE partitioned by month with GIN index on attributes.
Full-text search vector on spans. Audit log infrastructure."

# ---- Commit 4: Auth + RBAC ----
git add \
  receiver/auth.py \
  receiver/password.py \
  receiver/api/auth_endpoints.py \
  receiver/api/api_keys.py \
  receiver/api/members.py \
  receiver/alembic/versions/015_*.py \
  receiver/alembic/versions/016_*.py \
  2>/dev/null || true
git commit -m "feat: RBAC with 4 roles, Argon2id auth, API key management

4-role RBAC: owner, admin, operator, viewer with scope-based access.
Argon2id password hashing (OWASP parameters, pepper support).
Dual authentication: API keys (SHA-256 hashed) + session tokens.
Key rotation, expiration, IP allowlisting, last_used_at tracking.
Timing-safe comparisons throughout. Transparent parameter rehashing."

# ---- Commit 5: Shadow mode + approvals ----
git add \
  receiver/api/approvals.py \
  receiver/api/intervention.py \
  receiver/alembic/versions/017_*.py \
  receiver/alembic/versions/018_*.py \
  receiver/alembic/versions/019_*.py \
  receiver/alembic/versions/020_*.py \
  2>/dev/null || true
git commit -m "feat: shadow mode, human approval workflows, multi-party approval

Shadow mode: policies evaluate but never enforce. Safe policy testing.
Human approval: require_approval action pauses agent execution until
an operator approves or denies. Multi-party approval (N-of-M approvers).
Approval reaper for expired requests. SDK poll-based approval flow.
Optimistic locking (SELECT FOR UPDATE) prevents concurrent races."

# ---- Commit 6: Compliance + intelligence ----
git add \
  receiver/api/compliance_export.py \
  receiver/api/agent_inventory.py \
  receiver/api/cost_forecast.py \
  receiver/api/costs.py \
  receiver/api/budgets.py \
  receiver/api/model_prices.py \
  receiver/incident_detector.py \
  2>/dev/null || true
git commit -m "feat: EU AI Act compliance, cost intelligence, incident detection

EU AI Act evidence export (Articles 9-15, 19 with recommendations).
Agent inventory with NIST AI RMF risk scoring.
Cost attribution per agent/model/tool with forecasting and burn alerts.
Budget enforcement with automatic halt on overspend.
Incident detection with Article 73 reporting metadata.
Model price database for cost calculation."

# ---- Commit 7: Security hardening ----
git add \
  receiver/alembic/versions/021_*.py \
  receiver/encryption.py \
  receiver/credential_patterns.py \
  2>/dev/null || true
git commit -m "feat: security hardening and column-level encryption

DB-level audit immutability (REVOKE UPDATE/DELETE + trigger).
Postgres Row-Level Security for tenant isolation.
Webhook SSRF protection (post-DNS-resolution IP blocking).
Replay protection, CORS allowlist, trusted hosts, security headers.
Column-level encryption (Fernet AES-256) for TOTP and webhook secrets.
Protobuf size cap (4MB), Pydantic extra=forbid, hmac.compare_digest.
PII base64 decode-rescan for encoding evasion defense."

# ---- Commit 8: MFA + password management ----
git add \
  receiver/alembic/versions/022_*.py \
  2>/dev/null || true
git commit -m "feat: TOTP MFA with backup codes and password reset

TOTP MFA via pyotp with encrypted secret storage.
10 hashed backup codes for device loss recovery.
Password reset: admin-initiated (temp password) and email-based (SMTP).
Force password change flag after admin reset.
Account lockout (5 failures, 15 min lock). Concurrent session cap (10)."

# ---- Commit 9: Integrations ----
git add \
  receiver/api/notification_channels.py \
  receiver/api/github_integration.py \
  receiver/integrations/ \
  receiver/alembic/versions/023_*.py \
  2>/dev/null || true
git commit -m "feat: Slack, Discord, GitHub, and webhook integrations

Slack: Block Kit messages, interactive approve/deny buttons, signature
verification. Discord: rich embeds, interactive components.
GitHub App: webhook handler, commit tracking, issues from incidents.
Generic webhooks: HMAC-signed, SSRF-protected, replay-protected.
Notification dispatcher routes events to configured channels."

# ---- Commit 10: Webhooks ----
git add \
  receiver/webhooks/ \
  receiver/api/webhook_deliveries.py \
  receiver/api/webhook_signing_keys.py \
  2>/dev/null || true
git commit -m "feat: webhook delivery system with signing and retry

Dramatiq-based async webhook delivery with exponential backoff.
HMAC-SHA256 request signing with rotatable keys.
Delivery tracking, retry management, sweep for stale deliveries.
SSRF guard with post-DNS-resolution IP validation."

# ---- Commit 11: Halts + projects ----
git add \
  receiver/api/halts.py \
  receiver/api/projects.py \
  receiver/api/project_settings.py \
  2>/dev/null || true
git commit -m "feat: halt switches and project management

Halts: emergency kill switches at project or agent scope.
Project CRUD with slug-based routing.
Project settings: PII redaction config, retention, intervention defaults."

# ---- Commit 12: Audit system ----
git add \
  receiver/api/audit.py \
  receiver/audit_helpers.py \
  2>/dev/null || true
git commit -m "feat: tamper-evident audit log with HMAC hash chain

HMAC-SHA256 hash chain for audit entry integrity verification.
Merkle root anchoring for external attestation.
SCIM 2.0 query support. Structured audit events.
Green lock icon verification pattern (Pangea-style UX)."

# ---- Commit 13: Competitive features ----
git add \
  receiver/circuit_breaker.py \
  receiver/mcp_gateway.py \
  receiver/sarif_output.py \
  receiver/api/security_tools.py \
  receiver/egress_proxy.py \
  2>/dev/null || true
git commit -m "feat: circuit breakers, MCP gateway, SARIF output, egress proxy

Circuit breakers: per-agent/tool auto trip/half-open/reset. Contains
blast radius when agents fail without operator intervention.
MCP security gateway: proxy between agent and MCP servers with policy
evaluation and credential scanning on responses.
SARIF v2.1.0 output for GitHub Code Scanning integration.
Agent BOM export in CycloneDX 1.6 format.
50+ credential detection patterns (AWS, GCP, Azure, GitHub, Slack,
Stripe, database URIs, private keys, JWT, bearer tokens).
HTTP egress proxy via mitmproxy addon for zero-trust network control."

# ---- Commit 14: Behavioral intelligence ----
git add \
  receiver/vigil.py \
  receiver/heartbeat.py \
  receiver/retention_cleanup.py \
  receiver/security_auto.py \
  receiver/alembic/versions/024_*.py \
  2>/dev/null || true
git commit -m "feat: behavioral drift detection, heartbeat, data retention

Vigil: EWMA/CUSUM statistical drift detection per agent. Auto-calibrates
baseline from 100+ observations. Fires alerts on sustained behavioral shift.
Heartbeat monitoring: detects agents that stop sending SDK heartbeats.
SDK integrity check: SHA-256 code hash detects runtime modification.
Data retention cleanup: daily background task for expired data.
Account lockout, concurrent session cap, approval optimistic locking."

# ---- Commit 15: Dashboard convenience endpoints ----
git add \
  receiver/api/dashboard_convenience.py \
  receiver/alembic/versions/025_*.py \
  2>/dev/null || true
git commit -m "feat: dashboard convenience endpoints and auth enhancements

GET /v1/auth/capabilities (no auth, drives login/register UI).
GET /v1/version. POST /v1/auth/change-password.
Members CRUD: list, invite, change role, remove, reset password,
disable MFA, transfer ownership. Pending invitations table.
Project settings GET/PATCH. GDPR Article 20 data export.
Force password change flag. Path aliases for BFF proxy compatibility.
Startup warnings for missing security env vars."

# ---- Commit 16: SDK ----
git add sdk/ 2>/dev/null || true
git commit -m "feat: Python SDK with 10 framework integrations

pip install strathon. 3-line integration for any agent framework.
Client with OpenTelemetry-based span export to receiver.
Runtime policy enforcement: block, steer, throttle, require_approval.
Fail-closed mode with configurable staleness window.
Heartbeat thread (30s liveness signal) with code hash integrity check.

Framework integrations: LangGraph, CrewAI, OpenAI Agents SDK,
OpenAI direct, Anthropic direct, LangChain, AutoGen, Claude Agent SDK,
Pydantic AI (AbstractCapability), Google ADK (BasePlugin).
Zero monkey-patching — uses first-class framework plugin systems."

# ---- Commit 17: CLI ----
git add cli/ 2>/dev/null || true
git commit -m "feat: CLI with 13 command groups and 30+ subcommands

pip install strathon-cli. Click-based CLI with Rich table output.
Command groups: policies, traces, spans, halts, templates, agents,
compliance, budgets, audit, projects, approvals, notifications, admin.
Admin commands: reset-password, create-user, list-users,
transfer-ownership, revoke-all-keys.
--json flag for machine-readable output. Env var configuration."

# ---- Commit 18: Dashboard ----
git add dashboard/ 2>/dev/null || true
git commit -m "feat: Next.js 16 dashboard with BFF security proxy

19 pages wired to receiver API via 32 BFF proxy routes.
httpOnly session cookies (Secure, SameSite=Strict).
CVE-2025-29927 middleware protection. Security headers.

Pages: overview, policies (list + detail + CEL editor + impact
simulator), traces (list + waterfall with minimap), spans (facets),
approvals (cards + countdown), agents (risk rings), audit (hash
locks), budgets (KPI + chart), compliance (progress rings + export),
settings (members + auth + retention + API keys), login (MFA),
register, forgot/reset/change password.

STRATHON_MODE controls self-hosted vs cloud UI. Welcome banner.
Empty/error/loading states. Client-side input validation.
Light and dark mode. Mobile responsive."

# ---- Commit 19: Tests ----
git add \
  receiver/tests/ \
  tests/ \
  receiver/conftest.py \
  2>/dev/null || true
git commit -m "test: 1000+ tests covering all endpoints and features

Receiver tests: OTLP ingest, policies CRUD, shadow mode, approvals,
audit, budgets, compliance, cost forecast, API keys, halts, MFA,
RBAC, rate limiting, redaction, sampling, security hardening,
webhooks, topology, competitive features, dashboard convenience.
SDK tests: policy enforcement, approval flow, framework integrations,
fail-closed mode, steer, throttle, halt.
E2E tests: budget lifecycle, halt lifecycle, cross-framework parity."

# ---- Commit 20: Examples ----
git add \
  examples/ \
  docs/ \
  2>/dev/null || true
git commit -m "docs: CEL reference, examples, and integration guides

CEL policy reference with 20 common examples and AI prompt template
for natural language to CEL conversion.
16 example scripts demonstrating SDK integration with various
frameworks and policy configurations."

# ---- Commit 21: Community docs ----
git add \
  CHANGELOG.md \
  CONTRIBUTING.md \
  CODE_OF_CONDUCT.md \
  SECURITY.md \
  README.md \
  .github/ \
  2>/dev/null || true
git commit -m "docs: community documentation and GitHub templates

CHANGELOG, CONTRIBUTING guide, CODE_OF_CONDUCT, SECURITY policy.
GitHub issue templates (bug report, feature request).
Pull request template. CI workflow (GitHub Actions)."

# ---- Commit 22: Benchmarks ----
git add benchmarks/ 2>/dev/null || true
git commit -m "bench: load testing script for OTLP ingest throughput

Async load test generating valid OTLP protobuf payloads.
Reports: throughput (spans/sec), latency percentiles, error rate.
Configurable: requests, concurrency, batch size, endpoint.
2,080 spans/sec on single instance with full policy pipeline."

# ---- Commit 23: Infrastructure ----
git add \
  Dockerfile \
  docker-compose*.yml \
  scripts/ \
  .strathon-forbidden-words.example \
  2>/dev/null || true
git commit -m "infra: Dockerfile, docker-compose, and deployment scripts

Multi-stage Dockerfile for receiver.
docker-compose.yml: receiver + postgres + dashboard (self-hosted).
docker-compose.prod.yml: production configuration.
Pre-commit secret scanning hook."

# ---- Commit 24: Catch remaining files ----
git add -A 2>/dev/null || true
# Check if there's anything left to commit
if ! git diff --cached --quiet 2>/dev/null; then
  git commit -m "chore: configuration files and remaining assets"
fi

echo ""
echo "=== Step 5: Replace main ==="
git branch -D main 2>/dev/null || true
git branch -m clean-main main

echo ""
echo "=== Done! ==="
echo "Verify with: git log --oneline"
echo ""
echo "When ready: git push origin main --force"
echo ""
echo "Full history preserved in: backup-full-history branch"
echo "To restore: git checkout backup-full-history && git branch -D main && git branch -m main"
