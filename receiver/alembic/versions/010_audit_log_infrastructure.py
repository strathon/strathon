"""Audit log infrastructure: schema, events table, anchors, streams.

Adds the audit log data plane:

- ``audit.events`` — partitioned by RANGE on ``occurred_at`` (monthly).
  Every operator mutation produces one row. Append-only enforced via
  trigger and revoked grants. HMAC-SHA256 hash chain provides per-row
  tamper detection.

- ``audit.anchors`` — one row per minute, holds the Merkle root over the
  prior minute's events. Provides external integrity-proof points. The
  signature column is filled in by a later commit when KMS signing
  ships; until then the anchor is plaintext-verifiable from the chain.

- ``audit.streams`` — operator-registered webhook destinations that
  receive every committed audit event. Delivery rides the existing
  webhook_deliveries machinery (retries, signing, DLQ).

Partitioning is hand-rolled (no pg_partman extension dependency,
preserving Strathon's single-Postgres posture). The migration creates
the current month plus three future months; a daily dramatiq task
keeps the window populated thereafter.

Append-only enforcement is belt-and-suspenders: triggers on UPDATE,
DELETE, TRUNCATE plus REVOKE of those grants from the app role. The
triggers fire even for superusers, the REVOKE is for the normal app
path. Together they make accidental or programmatic mutation of an
audit row impossible without explicitly bypassing both.

This migration is upgrade-only as a practical matter. A downgrade
that drops audit data would itself be an auditable event the audit
log cannot record. The downgrade body is implemented for development
parity but raises an exception if any events exist, mirroring the
safety guard in earlier CHECK-extension migrations.
"""

from __future__ import annotations

from datetime import date
from typing import Sequence, Union

from alembic import op


revision: str = "010"
down_revision: Union[str, Sequence[str], None] = "009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _partition_bounds(year: int, month: int) -> tuple[str, str, str]:
    """Return (partition_suffix, from_date_iso, to_date_iso) for a month.

    Suffix format: ``YYYY_MM``. From-date is the first of ``month``;
    to-date is the first of the following month. Inclusive lower,
    exclusive upper, matching Postgres RANGE partition semantics.
    """
    if month == 12:
        next_year, next_month = year + 1, 1
    else:
        next_year, next_month = year, month + 1
    from_date = date(year, month, 1).isoformat()
    to_date = date(next_year, next_month, 1).isoformat()
    return f"{year:04d}_{month:02d}", from_date, to_date


def _initial_partition_months() -> list[tuple[int, int]]:
    """Months to create at migration time: current + 3 future.

    The daily partition-maintenance task fills in additional months
    going forward. Three months of look-ahead survives a worker outage
    of up to a month without any insert failures.
    """
    today = date.today()
    months: list[tuple[int, int]] = []
    year, month = today.year, today.month
    for _ in range(4):
        months.append((year, month))
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
    return months


_UPGRADE_HEAD = r"""
CREATE SCHEMA IF NOT EXISTS audit;

-- ---- audit.events ----
-- Partitioned by RANGE on occurred_at. Partition key MUST be included
-- in the PK per Postgres constraints, hence (occurred_at, id).
CREATE TABLE audit.events (
    id              UUID        NOT NULL DEFAULT gen_random_uuid(),
    sequence_no     BIGSERIAL   NOT NULL,
    occurred_at     TIMESTAMPTZ NOT NULL,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),

    -- Tenancy. project_id is the v1 tenancy boundary; org_id will be
    -- added when the orgs commit ships and backfilled from
    -- project -> org membership.
    project_id      UUID        NOT NULL,

    -- Actor. The trio (type, id, display) lets us audit humans,
    -- service accounts, agents acting on behalf of humans, and the
    -- system itself.
    actor_type      TEXT        NOT NULL
        CHECK (actor_type IN ('human','service_account','agent','system','anonymous')),
    actor_id        TEXT        NOT NULL,
    actor_display   TEXT,
    on_behalf_of    TEXT,

    -- Action. action_category is one of a small enumerated set
    -- (policy, halt, budget, api_key, project_settings, ...) that
    -- groups related actions for filtering. action is the specific
    -- verb (policy.create, policy.update, halt.issue, ...). outcome
    -- captures whether the mutation succeeded; reason explains a deny
    -- or error.
    action          TEXT        NOT NULL,
    action_category TEXT        NOT NULL,
    outcome         TEXT        NOT NULL
        CHECK (outcome IN ('allow','deny','error','partial')),
    reason          TEXT,

    -- Resource. The triple (type, id, parent) identifies what was
    -- mutated. cascade_root_id groups related events from a bulk or
    -- cascading operation (e.g., deleting a project cascades to
    -- deleting all its policies; one parent event + one
    -- cascade.complete event, rather than 101 rows).
    resource_type   TEXT        NOT NULL,
    resource_id     TEXT        NOT NULL,
    resource_parent TEXT,
    cascade_root_id UUID,

    -- Request envelope. request_id is the X-Request-ID header (or a
    -- server-generated UUID if absent). api_key_id is the key's id,
    -- never the key value. auth_method indicates the authentication
    -- path used.
    request_id      UUID        NOT NULL,
    source_ip       INET,
    user_agent      TEXT,
    api_key_id      TEXT,
    auth_method     TEXT,

    -- Change payload. before_state and after_state are full snapshots
    -- of the resource. diff is an RFC 6902 JSON Patch derived from
    -- them; redundant but pre-computed for efficient operator review.
    -- Each capped at 64 KB at the application layer; oversized
    -- payloads are stored by reference (planned).
    before_state    JSONB,
    after_state     JSONB,
    diff            JSONB,

    -- Integrity. prev_hash is the row_hash of the immediately
    -- preceding event for the same project. row_hash is the
    -- HMAC-SHA256 over (canonical_json(this_row) || prev_hash). The
    -- chain is per-project so two projects' audit logs are
    -- independently verifiable. hmac_key_id references which HMAC
    -- key signed the row; rotation creates a new id and existing
    -- rows continue to verify under their original key.
    prev_hash       BYTEA       NOT NULL,
    row_hash        BYTEA       NOT NULL,
    hmac_key_id     SMALLINT    NOT NULL,

    -- Compliance metadata. pii_classes lists the data classifications
    -- present in this row (e.g., {'email','ip_address'}), used for
    -- routing to compliance reviewers and for GDPR Article 30 records
    -- of processing. schema_version pins the row shape for forward
    -- compatibility.
    pii_classes     TEXT[]      NOT NULL DEFAULT '{}',
    schema_version  SMALLINT    NOT NULL DEFAULT 1,

    PRIMARY KEY (occurred_at, id)
) PARTITION BY RANGE (occurred_at);

COMMENT ON TABLE audit.events IS
    'Append-only audit trail of operator mutations. Partitioned monthly. '
    'Tamper-evident via HMAC-SHA256 hash chain (prev_hash, row_hash). '
    'See docs/audit.md for the full design rationale.';

-- ---- Indexes on the parent (propagate to all partitions) ----

-- BRIN on occurred_at is ideal for append-only time-series: ~10 KB
-- index for 100 GB of data, vs gigabytes for a B-tree. Combine with
-- partition pruning for fast time-windowed scans.
CREATE INDEX events_brin_time ON audit.events
    USING BRIN (occurred_at) WITH (pages_per_range = 32);

CREATE INDEX events_project_time
    ON audit.events (project_id, occurred_at DESC);
CREATE INDEX events_actor_time
    ON audit.events (actor_id, occurred_at DESC);
CREATE INDEX events_resource_time
    ON audit.events (resource_type, resource_id, occurred_at DESC);
CREATE INDEX events_request
    ON audit.events (request_id);
CREATE INDEX events_action_time
    ON audit.events (action_category, action, occurred_at DESC);

-- Partial index for failures: small but frequently queried during
-- incident response.
CREATE INDEX events_denied_time
    ON audit.events (occurred_at DESC, project_id)
    WHERE outcome IN ('deny','error');

-- Cascade-group lookup. NULL is the common case so the partial saves
-- index size.
CREATE INDEX events_cascade
    ON audit.events (cascade_root_id)
    WHERE cascade_root_id IS NOT NULL;

-- ---- Append-only enforcement (triggers) ----
-- Triggers fire even for the postgres superuser. Combined with
-- REVOKE UPDATE/DELETE/TRUNCATE on PUBLIC, this provides defense in
-- depth against accidental or programmatic mutation. A determined
-- attacker with superuser can still ALTER TABLE to drop the trigger;
-- the per-minute anchor and forward-looking WORM archive (planned)
-- close that residual gap.
CREATE OR REPLACE FUNCTION audit.deny_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit.events is append-only (op=%)', TG_OP;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER events_no_update BEFORE UPDATE ON audit.events
    FOR EACH ROW EXECUTE FUNCTION audit.deny_mutation();
CREATE TRIGGER events_no_delete BEFORE DELETE ON audit.events
    FOR EACH ROW EXECUTE FUNCTION audit.deny_mutation();
CREATE TRIGGER events_no_truncate BEFORE TRUNCATE ON audit.events
    FOR EACH STATEMENT EXECUTE FUNCTION audit.deny_mutation();

-- ---- audit.anchors ----
-- One row per minute (cron'd from the worker). last_row_hash anchors
-- the per-project hash chain into a globally-ordered timeline.
-- merkle_root is the Merkle root over (sequence_no, row_hash) for
-- all events in the prior minute; verification of a single event
-- requires the row, the prev_hash chain to the next anchor, and the
-- Merkle inclusion proof. signature is filled in by KMS signing in
-- planned; current anchors are plaintext-verifiable.
CREATE TABLE audit.anchors (
    anchor_at       TIMESTAMPTZ PRIMARY KEY,
    last_sequence   BIGINT      NOT NULL,
    last_row_hash   BYTEA       NOT NULL,
    merkle_root     BYTEA       NOT NULL,
    event_count     INT         NOT NULL,
    signature       BYTEA,
    signing_key_id  TEXT
);

COMMENT ON TABLE audit.anchors IS
    'Per-minute integrity anchors over audit.events. Currently stores '
    'plaintext-verifiable Merkle roots; KMS signatures planned.';

-- ---- audit.streams ----
-- Operator-registered webhook destinations. Each enabled stream
-- receives every audit event committed for its project, via the
-- existing webhook_deliveries machinery.
CREATE TABLE audit.streams (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      UUID        NOT NULL,
    name            TEXT        NOT NULL,
    url             TEXT        NOT NULL,
    signing_key_id  UUID,
    enabled         BOOLEAN     NOT NULL DEFAULT TRUE,
    -- paused_at is set when consecutive failures exceed a threshold;
    -- the stream remains in the table but new events skip it until
    -- an operator unpause action. Buffered events past 7 days from
    -- pause are not replayed (matches GitHub Enterprise behavior).
    paused_at       TIMESTAMPTZ,
    pause_reason    TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    -- Categories to include; NULL means all categories.
    -- e.g., '{policy,halt,api_key}' to receive only those.
    categories      TEXT[]
);

CREATE INDEX audit_streams_project_enabled
    ON audit.streams (project_id) WHERE enabled = TRUE AND paused_at IS NULL;

CREATE UNIQUE INDEX audit_streams_project_name
    ON audit.streams (project_id, name);

-- ---- Grants ----
-- Belt-and-suspenders on top of the triggers. The app role can read
-- and insert; it explicitly cannot UPDATE, DELETE, or TRUNCATE.
-- A future audit-admin role (separate connection pool) will own
-- the partition-management DDL.
GRANT USAGE ON SCHEMA audit TO strathon;
GRANT INSERT, SELECT ON audit.events TO strathon;
GRANT USAGE, SELECT ON SEQUENCE audit.events_sequence_no_seq TO strathon;
GRANT INSERT, SELECT ON audit.anchors TO strathon;
GRANT SELECT, INSERT, UPDATE, DELETE ON audit.streams TO strathon;
REVOKE UPDATE, DELETE, TRUNCATE ON audit.events FROM strathon;
REVOKE UPDATE, DELETE, TRUNCATE ON audit.events FROM PUBLIC;
"""


def _create_partition_sql(year: int, month: int) -> str:
    suffix, from_date, to_date = _partition_bounds(year, month)
    return (
        f"CREATE TABLE IF NOT EXISTS audit.events_{suffix} "
        f"PARTITION OF audit.events "
        f"FOR VALUES FROM ('{from_date}') TO ('{to_date}');"
    )


_DOWNGRADE_SQL = r"""
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM audit.events LIMIT 1) THEN
        RAISE EXCEPTION
            'Cannot downgrade: audit.events contains rows. '
            'Audit data is append-only by design. Manually archive '
            'and TRUNCATE (with trigger disabled, by superuser) '
            'before downgrading.';
    END IF;
END $$;

DROP TABLE IF EXISTS audit.streams;
DROP TABLE IF EXISTS audit.anchors;
DROP TABLE IF EXISTS audit.events CASCADE;
DROP FUNCTION IF EXISTS audit.deny_mutation();
DROP SCHEMA IF EXISTS audit;
"""


def upgrade() -> None:
    op.execute(_UPGRADE_HEAD)
    # Initial partitions: current month + 3 future. The daily
    # maintenance task picks up from there.
    for year, month in _initial_partition_months():
        op.execute(_create_partition_sql(year, month))


def downgrade() -> None:
    op.execute(_DOWNGRADE_SQL)
