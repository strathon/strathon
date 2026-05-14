-- ============================================================
-- 003: Seed development API key for the default project
-- ============================================================
-- This migration inserts a well-known development API key that
-- demos and local-dev workflows can use out of the box.
--
-- The raw key is:
--
--     stra_dev_local_default_project_do_not_use_in_production
--
-- SHA-256 hash of the above (precomputed; verifiable with
--     printf 'stra_dev_local_default_project_do_not_use_in_production' | sha256sum
-- ):
--     d167e0111ebddd7e1001ad51ded8b7f9f7887c127a626063a83e02b6e6807924
--
-- !!! SECURITY !!!
-- This key has cleartext-known. Anyone with HTTP access to the receiver
-- can act as the default project. ROTATE BEFORE PRODUCTION:
--   1. POST /v1/api_keys to create a real key
--   2. DELETE /v1/api_keys/<this-key-id> to revoke this seed
--
-- The seeding is idempotent: re-running the migration won't create
-- duplicate rows.

INSERT INTO api_keys (id, project_id, name, key_hash, key_prefix)
VALUES (
    '00000000-0000-0000-0000-000000000010',                                      -- well-known id so revocation is reproducible
    '00000000-0000-0000-0000-000000000001',                                      -- default project id (seeded in 001)
    'Local development (seeded by migration 003 — rotate for production)',
    'd167e0111ebddd7e1001ad51ded8b7f9f7887c127a626063a83e02b6e6807924',           -- sha256 hex of the well-known dev key
    'stra_dev_loc'                                                               -- first 12 chars of the raw key
)
ON CONFLICT (id) DO NOTHING;
