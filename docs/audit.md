# Audit log

Strathon records every operator mutation to a tamper-evident,
append-only audit log. This is the surface compliance frameworks
(SOC 2, HIPAA, PCI DSS, GDPR, ISO 27001, EU AI Act Art. 12) require
when they ask "who changed what, when, and from where."

Audit data lives in its own Postgres schema (`audit`), separate
from the control-plane tables. Reads and writes are scope-gated;
the schema itself rejects UPDATE, DELETE, and TRUNCATE at the
trigger level so an operator who accidentally hits the wrong DELETE
gets a clear error rather than silent data loss.

## What gets logged

Every mutation routed through the receiver's REST API emits one
`audit.events` row inside the same database transaction as the
mutation itself. If the audit insert fails, the mutation rolls back.
This is the fail-closed contract: there is no path where the
control plane changes and the audit log doesn't notice.

Endpoints currently audited:

| Action                       | Category               |
|------------------------------|------------------------|
| `policy.create`              | `policy`               |
| `policy.update`              | `policy`               |
| `policy.delete`              | `policy`               |
| `halt.issue`                 | `halt`                 |
| `halt.clear`                 | `halt`                 |
| `budget.create`              | `budget`               |
| `budget.update`              | `budget`               |
| `budget.delete`              | `budget`               |
| `api_key.create`             | `api_key`              |
| `api_key.revoke`             | `api_key`              |
| `project_settings.update`    | `project_settings`     |
| `webhook_signing_key.create` | `webhook_signing_key`  |
| `webhook_signing_key.revoke` | `webhook_signing_key`  |
| `webhook_delivery.replay`    | `webhook_delivery`     |
| `model_price.set`            | `model_price`          |
| `model_price.delete`         | `model_price`          |
| `audit_stream.create`        | `audit_stream`         |
| `audit_stream.delete`        | `audit_stream`         |
| `audit.read`                 | `audit`                |

`audit.read` rows are emitted by GET requests against the audit log
itself — the audit-of-the-audit pattern Vault, GitHub Enterprise,
and 1Password ship.

## Row shape

Each audit row carries:

- **Actor**: type (`human`, `service_account`, `agent`, `system`,
  `anonymous`), id, optional display name, optional `on_behalf_of`.
- **Action**: `action` (`policy.create`), `action_category`
  (`policy`), `outcome` (`allow`, `deny`, `error`, `partial`),
  optional `reason`.
- **Resource**: type, id, optional parent, optional
  `cascade_root_id` (groups events from a bulk operation).
- **Request envelope**: `request_id`, `source_ip`, `user_agent`,
  `api_key_id`, `auth_method`.
- **Change payload**: `before_state`, `after_state`, and a
  pre-computed `diff` (RFC 6902-style JSON Patch over top-level
  keys). Sensitive fields are stripped from the snapshots before
  storage (see *Redaction* below).
- **Integrity**: `prev_hash`, `row_hash`, `hmac_key_id` (HMAC chain
  inputs; see *Integrity model*).
- **Compliance metadata**: `pii_classes` (array), `schema_version`.
- **Timing**: `occurred_at` (caller-perceived time), `ingested_at`
  (server-side clock).

The full schema is in `migration 010_audit_log_infrastructure.py`.

## Querying

Use the SCIM 2.0 `filter` query parameter on `GET /v1/audit/events`.
Examples:

```
# Everything denied in the last day
GET /v1/audit/events?filter=outcome+eq+%22deny%22&limit=50

# Every policy mutation by a specific service account
GET /v1/audit/events?filter=action_category+eq+%22policy%22+and+actor_id+eq+%22svc_42%22

# Events touching a single resource
GET /v1/audit/events?filter=resource_type+eq+%22policy%22+and+resource_id+eq+%22<id>%22

# Time-bounded sweep
GET /v1/audit/events?filter=occurred_at+ge+%222026-05-01T00:00:00Z%22+and+occurred_at+lt+%222026-06-01T00:00:00Z%22
```

Supported operators: `eq`, `ne`, `gt`, `ge`, `lt`, `le`, `co`
(contains), `sw` (starts with), `ew` (ends with). Boolean
combinators: `and`, `or`, `not`. Parentheses group. The full
grammar is in `audit/scim_filter.py`.

Filterable attributes are an allowlist; an attempt to filter on a
non-allowlisted column returns 400 with the list of valid columns.

### Pagination

Responses include a `next_cursor` opaque token when more rows are
available. Pass it back as `?cursor=<token>`:

```
GET /v1/audit/events?limit=100
→ {"data": [...], "next_cursor": "eyJvY2N1cnJlZF9hdCI6Li4ufQ"}

GET /v1/audit/events?limit=100&cursor=eyJvY2N1cnJlZF9hdCI6Li4ufQ
→ {"data": [...], "next_cursor": null}
```

Cursor format is base64url-encoded JSON over `(occurred_at, id)`;
results are ordered newest first. Hard cap per page is 1000.

## Scopes

| Scope          | Grants                                                  |
|----------------|---------------------------------------------------------|
| `audit:read`   | `GET /v1/audit/events*`, `GET /v1/audit/anchors`        |
| `audit:write`  | `POST` / `DELETE` `/v1/audit/streams`                   |
| `audit:admin`  | Reserved for break-glass legal-hold release operations  |

`audit:admin` is enumerable but no current endpoint requires it;
A future release wires it once legal-hold / e-discovery surfaces ship.

## Integrity model

The audit log is **tamper-evident**, not tamper-proof. Two layers:

1. **Per-row HMAC chain** — every row's `row_hash` is computed as
   `HMAC-SHA256(K, canonical_json(row) || prev_hash)` where
   `prev_hash` is the previous row's `row_hash` for the same
   project. The chain is per-project so multi-tenant deployments
   verify each tenant's chain independently. A single row
   modification, insertion, or deletion breaks the chain at that
   point.
2. **Per-minute Merkle anchors** — a background worker computes a
   Merkle root over the prior interval's `row_hash` values and
   inserts an `audit.anchors` row every
   `STRATHON_AUDIT_ANCHOR_INTERVAL_SECONDS` (default 60s). The
   anchor's `merkle_root` is a 32-byte SHA-256 digest. A future release
   adds KMS signing so the anchor itself is non-repudiable.

To verify a single event, call `GET /v1/audit/events/{id}/verify`.
The receiver recomputes the HMAC against the row's stored
`prev_hash` and the configured HMAC key, and returns
`{"valid": true|false}`.

Defense-in-depth at the DB level:

- `audit.events` triggers reject UPDATE, DELETE, and TRUNCATE.
  Triggers fire even for the postgres superuser; an operator who
  drops to a psql shell can't accidentally clobber a row.
- The app role's grants on `audit.events` are INSERT and SELECT
  only; UPDATE/DELETE/TRUNCATE are revoked from PUBLIC and from
  the app role explicitly.
- A determined attacker with `ALTER TABLE` can still drop the
  trigger. The per-minute anchor records the prior-minute Merkle
  root, so historical rows can't be silently rewritten without
  invalidating every anchor that includes them. A future release's
  externally-signed anchors close this last residual gap.

## HMAC key generation and rotation

The HMAC key is sourced from `STRATHON_AUDIT_HMAC_KEY`. Generate
with:

```
python -c 'import secrets; print(secrets.token_hex(32))'
```

The behavior with an empty key depends on the deployment mode
(`STRATHON_MODE`, default `self-hosted`). In self-hosted mode an empty
key falls back to a deterministic dev key with a one-time warning in
the logs, so the receiver is usable out of the box — set a real key
for any non-development deployment. In cloud mode an empty key raises
instead of silently signing with a known value.

Each row records `hmac_key_id` so historical rows continue to
verify under the key they were signed with. The current release ships a single
key (`hmac_key_id = 1`); rotation in a future release increments the id and
keeps the previous key available for chain verification of
historical rows.

## Redaction

Sensitive fields are stripped from `before_state` / `after_state`
before storage. Three strategies, one per field name:

- **exclude** (default for raw secrets): the field is removed
  entirely. Applies to `api_key`, `value`, `key`, `secret`,
  `token`, `signing_key`, `signing_secret`, `session_token`,
  `refresh_token`. The raw value never reaches the audit row.
- **redact**: the field's value is replaced with the literal
  `[REDACTED]`. Applies to `password`, `password_hash`. Existence
  matters but value doesn't.
- **hmac**: the value is replaced with `hmac-sha256:<hex>` so
  external reports can be correlated without exposing the
  underlying identifier. Applies to `stripe_customer_id`,
  `external_user_id`.

The full rule table is in `audit/redaction.py`. Operators who need
additional fields covered open a PR — The current release ships a fixed
conservative default; a future release surfaces a per-tenant rules table.

## Streams (webhook destinations)

Operators register HTTPS endpoints that receive every committed
audit event for their project. The delivery rides the existing
webhook_deliveries machinery (retries, signing, DLQ).

```
POST /v1/audit/streams
Content-Type: application/json
Authorization: Bearer <key-with-audit:write>

{
  "name": "splunk-prod",
  "url": "https://hec.splunk.example.com/services/collector",
  "signing_key_id": "<uuid>",
  "categories": ["policy", "halt", "api_key"]
}
```

`categories` is optional; omit to receive every category. The
signing key, if specified, is one of the project's existing
webhook signing keys; if omitted, the project's primary signing
key is used.

To stop a stream, `DELETE /v1/audit/streams/{id}`. Both create and
delete produce their own audit events (`audit_stream.create`,
`audit_stream.delete`).

## Retention

Events are stored in monthly partitions of `audit.events`. The
`STRATHON_AUDIT_HOT_MONTHS` setting (default 24) controls how many
months remain on the hot Postgres tier. The default of 24 months
satisfies the strictest current frameworks: HIPAA's six-year
records-of-disclosure requirement is met by 24 months hot plus a
cold archive (planned WORM tier); SOC 2 has no fixed minimum but
auditors expect at least one full audit cycle (typically 12 months).

A daily background task runs `ensure_future_partitions` which
creates partitions for the current month plus three lookahead
months. Idempotent via `CREATE TABLE IF NOT EXISTS`.

## OWASP Agentic Top 10 mapping

- **ASI04 (Agentic Supply Chain Vulnerabilities)** — audit captures the policy
  mutation that opened the attack vector. `policy.update` rows
  with `before_state` and `after_state` give the reviewer the
  exact change.
- **ASI08 (Cascading Failures)** — `halt.issue` rows record who
  triggered the halt and why; the resource and reason fields are
  human-readable in dashboards and incident reviews.
- **ASI09 (Human-Agent Trust Exploitation)** — every audit row records actor
  type, actor id, request id, source IP, and user agent. A halt
  issued by an unexpected actor is visible at a glance.
- **ASI10 (Rogue Agents)** — `audit.read` rows surface the
  query patterns of operators consuming the audit log itself. A
  flood of automated reads is detectable from the audit log.

## Compliance framework mapping

| Framework        | Requirement                              | How we meet it                                                                 |
|------------------|------------------------------------------|--------------------------------------------------------------------------------|
| SOC 2 CC7.2/7.3  | Detect & respond to security events       | Append-only audit log + integrity verification endpoint                        |
| HIPAA §164.312(b)| 6-year audit log retention                | Monthly partitions + `STRATHON_AUDIT_HOT_MONTHS=72`                             |
| PCI DSS 10.5.1   | Limit audit-log viewing to those with need| `audit:read` scope; `audit.read` self-logging                                  |
| GDPR Art 5(1)(f) | Integrity & confidentiality of logs       | HMAC chain + Merkle anchors                                                    |
| GDPR Art 17      | Right to erasure                          | Cascade-delete on resource teardown emits `cascade_root_id` group              |
| ISO 27001 A.8.15 | Protected audit logs                      | Append-only triggers + revoked grants + signed anchors (planned)               |
| EU AI Act Art 12 | High-risk AI logging (Dec 2027, Annex III) | All policy/halt mutations logged with 24-month hot retention                   |

## Operational notes

- The audit log is per-project. There is no cross-project
  global view; reads scoped to one project return only that
  project's events.
- HMAC key rotation must coordinate with chain verification:
  A future release ships a key-rotation tool that increments `hmac_key_id`
  and keeps historical keys available.
- Anchor sealer runs in the receiver process, not a separate
  worker. Restarts skip an anchor or two depending on timing;
  the worker picks up from the last anchor automatically.
- Streams ride dramatiq webhook delivery. If your project has no
  Redis configured, audit streams will still queue durably (rows
  in `webhook_deliveries`) but send inline to the request that
  triggered them — fine for dev, set `STRATHON_WEBHOOK_REDIS_URL`
  for production.
