# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in Strathon, please report it
responsibly. **Do not open a public GitHub issue.**

Email **strathon.team@gmail.com** with:

- A description of the vulnerability and its potential impact
- Steps to reproduce or a proof of concept
- The affected component (receiver, SDK, or both)
- Your preferred attribution (name, handle, or anonymous)

## Response Timeline

- **Acknowledgment**: within 48 hours of receipt
- **Initial assessment**: within 7 days
- **Fix or mitigation**: within 30 days for critical/high severity;
  within 90 days for medium/low

We will coordinate disclosure timing with you. If we have not responded
within the acknowledgment window, follow up or reach out via a GitHub
issue tagged `security` (without disclosing vulnerability details).

## Supported Versions

Strathon is pre-v1.0. Security fixes are applied to the latest commit
on `main` only. Once v1.0 ships, we will maintain a supported-versions
table here.

## Scope

The following are in scope for security reports:

- Authentication bypass or privilege escalation via the API key system
- Policy engine bypass (tool calls executing despite a matching block policy)
- Audit log tampering or hash-chain integrity breaks
- PII leaking through redaction bypass
- SQL injection or other injection attacks against the receiver
- Webhook signing key exposure or HMAC bypass
- SDK-side vulnerabilities that could allow an agent to evade enforcement

Out of scope: denial-of-service against a self-hosted instance (rate
limiting and resource allocation are the operator's responsibility),
and vulnerabilities in upstream dependencies (report those upstream,
but do let us know so we can track the fix).

## Security Design

Strathon's security architecture is documented across the codebase:

- **API keys**: SHA-256 hashed at rest, capability-scoped, per-key rate
  limited. See `docs/api_keys.md`.
- **Audit log**: HMAC-SHA256 hash-chained, append-only, per-minute
  Merkle anchors. See `docs/audit.md`.
- **PII redaction**: regex-based, default-on, Luhn-validated credit
  cards. See `docs/redaction.md`.
- **Webhook signing**: HMAC-SHA256, keys hashed at rest, plaintexts
  never persisted to disk.
- **Fail-closed SDK**: configurable staleness threshold; raises rather
  than silently allowing when the receiver is unreachable.
