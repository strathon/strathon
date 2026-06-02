"""Add scopes column to api_keys for capability-based access control

Up until this migration, every API key implicitly had full access to
every endpoint that read the Authorization header — and the /v1/api_keys
endpoints didn't read the header at all, so anyone reachable to the
receiver could manage keys. The api/ restructure preserved
that gap as a known issue; this migration plus the accompanying code
changes close it.

Each key now carries a list of scope strings; endpoints declare which
scope they require and the auth dependency rejects (HTTP 403) requests
whose key doesn't have it.

Server-side default for new rows: ['traces:write', 'policies:read'] —
the minimum an SDK needs (ingest plus poll-for-block/steer). Existing
rows backfill to this default automatically via the server_default.

The seeded development key (id 0...010, see migration 003) is updated
to ['*'] (the wildcard) so it retains its current "do anything in
development" behavior. Production deployments rotate this key after
first boot; replacement keys created via POST /v1/api_keys get whatever
scopes the caller requests, validated against the known set in
receiver/auth.py:KNOWN_SCOPES.

A CHECK constraint enforces a non-empty array. The full set of valid
scope strings is application-side (auth.py), not database-side, because
scopes evolve as new endpoints land and we don't want a DB migration
for each addition.

Revision ID: 004
Revises: 003
Create Date: 2026-05-15

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "004"
down_revision: Union[str, Sequence[str], None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_UPGRADE_SQL = r"""-- ============================================================
-- 004: Add scopes column to api_keys
-- ============================================================
-- Capability-based access control for the receiver's HTTP API. Each
-- API key now carries a list of scope strings; endpoints declare which
-- scope they require and the auth dependency rejects (HTTP 403)
-- requests whose key doesn't have it.
--
-- Default for new rows: ['traces:write', 'policies:read'].
--   - traces:write  - POST /v1/traces (OTLP ingest)
--                     POST /v1/intervention/{sync,halt} (SDK back-compat)
--   - policies:read - GET /v1/policies, GET /v1/policies/{id}
--                     (SDK polls for client-side block/steer enforcement)
--
-- These are the two things an SDK key needs and nothing else. Admin
-- operations (managing policies, creating/revoking keys) require
-- explicit scopes that aren't in the default.
--
-- Existing rows: the server_default backfills them to the SDK defaults.
-- The seeded development key is then upgraded to the wildcard '*' so
-- it retains its "everything works in dev" property.
--
-- The empty-scope check keeps misconfigured rows from silently granting
-- nothing (which would manifest as 403 errors that look like a bug).

ALTER TABLE api_keys
    ADD COLUMN scopes TEXT[] NOT NULL
    DEFAULT ARRAY['traces:write', 'policies:read']::text[];

ALTER TABLE api_keys
    ADD CONSTRAINT api_keys_scopes_not_empty
    CHECK (cardinality(scopes) > 0);

-- The seeded dev key (id 0...010, see migration 003) is intentionally
-- given the wildcard so the out-of-box demo flow works for every
-- endpoint. Operators rotating to a real key choose its scopes
-- deliberately at POST /v1/api_keys time.
UPDATE api_keys
    SET scopes = ARRAY['*']::text[]
    WHERE id = '00000000-0000-0000-0000-000000000010';
"""


_DOWNGRADE_SQL = r"""
ALTER TABLE api_keys DROP CONSTRAINT IF EXISTS api_keys_scopes_not_empty;
ALTER TABLE api_keys DROP COLUMN IF EXISTS scopes;
"""


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    op.execute(_DOWNGRADE_SQL)
