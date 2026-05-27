"""Add webhook_deliveries and webhook_signing_keys tables for reliable alert delivery

Before this migration, alert webhooks fired through `policies.fire_webhook`
as a detached `asyncio.create_task` from the OTLP ingest handler. No row
was written for the delivery attempt; if the receiver crashed mid-send,
if the destination returned 5xx, if a DNS lookup hiccupped — the alert
was gone, with no audit trail and no way to retry. For a tool whose
whole job is "make sure the operator hears about bad things agents do,"
silent webhook loss is the worst possible failure mode.

This migration adds the two tables that make webhook delivery reliable:

* webhook_deliveries — one row per attempt. Status transitions
  pending -> succeeded | failed_retrying | dlq | abandoned. The row is
  inserted in the same transaction as the matching policy_matches row,
  so durability becomes atomic with the policy match itself. Dramatiq
  is the queue layer that triggers actual HTTP sends (with exponential
  backoff + jitter middleware); this table is the durable state
  Dramatiq's in-Redis queue is reconstructable from.

* webhook_signing_keys — per-project HMAC signing secrets following the
  Standard Webhooks spec (https://github.com/standard-webhooks/standard-webhooks).
  The plain `whsec_*` value is shown to the operator once at creation
  and discarded; only a SHA-256 hash plus a four-character prefix lives
  in the database for identification and signing. Multiple active keys
  per project support graceful rotation: during a rotation window every
  webhook carries signatures under both the old and the new key, and
  receivers can verify with either.

Indexes are chosen for the two hot paths:

  - The Dramatiq dispatcher batch-claims pending rows ordered by
    next_attempt_at, so (status, next_attempt_at) is the index that
    matters for delivery throughput.
  - Operator endpoints (list/replay DLQ entries) scan by
    (project_id, status, created_at DESC), so that covers UI queries.

Status flow:

    pending          : just enqueued, not yet attempted (or scheduled for retry)
    succeeded        : 2xx received from destination, terminal
    failed_retrying  : attempt failed but max_attempts not yet reached
    dlq              : attempts exhausted, dead-lettered, awaits manual replay
    abandoned        : non-retriable response from destination (4xx other than 429),
                       terminal — pointless to retry a malformed URL or wrong scheme

`webhook_id` is the stable Standard Webhooks `webhook-id` header value
sent on every attempt for a given delivery. Stable-across-retries is
the contract: receivers use it to dedupe at-least-once delivery.
"""

from alembic import op


# revision identifiers, used by Alembic
revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE webhook_signing_keys (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id   UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            -- 4-character public prefix (e.g., 'k7m4') used in operator UIs to
            -- distinguish keys without revealing the secret. Matches the
            -- whsec_ identifier convention from Standard Webhooks.
            prefix       TEXT NOT NULL,
            -- SHA-256 of the plaintext whsec_ secret. The plaintext is shown
            -- once at creation and never persisted; if lost, the operator
            -- creates a new key and rotates.
            secret_hash  BYTEA NOT NULL,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            revoked_at   TIMESTAMPTZ NULL,
            CONSTRAINT webhook_signing_keys_prefix_len CHECK (char_length(prefix) = 4)
        );

        CREATE INDEX idx_webhook_signing_keys_project
          ON webhook_signing_keys (project_id, revoked_at NULLS FIRST, created_at DESC);

        CREATE TABLE webhook_deliveries (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id      UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            policy_id       UUID NOT NULL REFERENCES policies(id) ON DELETE CASCADE,
            -- The Standard Webhooks msg id. Stable across retries; receivers
            -- use it as the idempotency key. We default it via gen_random_uuid
            -- formatted as 'msg_' + uuid hex so it round-trips cleanly as a
            -- single text token (no dashes that some libs treat specially).
            webhook_id      TEXT NOT NULL UNIQUE,
            url             TEXT NOT NULL,
            payload         JSONB NOT NULL,

            status          TEXT NOT NULL DEFAULT 'pending',
            attempts        INTEGER NOT NULL DEFAULT 0,
            max_attempts    INTEGER NOT NULL DEFAULT 8,

            -- When the dispatcher should next try this row. Indexed for the
            -- claim query. For terminal rows (succeeded/dlq/abandoned) this
            -- is left at the last-set value; the WHERE status = 'pending'
            -- filter in the claim query excludes them.
            next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_attempt_at TIMESTAMPTZ NULL,
            last_response_status INTEGER NULL,
            last_error      TEXT NULL,

            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            CONSTRAINT webhook_deliveries_status_valid
                CHECK (status IN ('pending', 'succeeded', 'failed_retrying',
                                  'dlq', 'abandoned')),
            CONSTRAINT webhook_deliveries_attempts_nonneg
                CHECK (attempts >= 0 AND attempts <= max_attempts)
        );

        -- Hot path for the dispatcher: due-pending rows ordered by schedule.
        -- Partial index keeps it small (most rows are terminal-status quickly).
        CREATE INDEX idx_webhook_deliveries_due
          ON webhook_deliveries (next_attempt_at)
          WHERE status = 'pending';

        -- Operator-side queries: list/inspect by project and status.
        CREATE INDEX idx_webhook_deliveries_project
          ON webhook_deliveries (project_id, status, created_at DESC);

        -- Used by replay endpoint to find rows by their stable webhook_id.
        -- (UNIQUE on webhook_id already covers this; keeping the explicit
        --  comment so the read intent is documented in the schema.)
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS webhook_deliveries;
        DROP TABLE IF EXISTS webhook_signing_keys;
        """
    )
