-- ============================================================
-- 004: Add scopes column to api_keys
-- ============================================================
-- This is the raw-SQL mirror of receiver/alembic/versions/004_api_key_scopes.py
-- kept in sync because CI applies the .sql files directly (rather than
-- invoking Alembic). Production runtime uses Alembic via the receiver's
-- auto-migrate lifespan; both paths produce the same schema.
--
-- Capability-based access control for the receiver's HTTP API. Each
-- API key now carries a list of scope strings; endpoints declare which
-- scope they require and the auth dependency rejects (HTTP 403)
-- requests whose key doesn't have it.
--
-- Default for new rows: ['traces:write', 'policies:read'].
--   - traces:write  : POST /v1/traces (OTLP ingest)
--                     POST /v1/intervention/{sync,halt} (SDK back-compat)
--   - policies:read : GET /v1/policies, GET /v1/policies/{id}
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
