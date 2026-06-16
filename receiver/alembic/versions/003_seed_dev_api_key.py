"""Seed development API key for the default project

Optionally inserts a well-known development API key into the default
project for local-dev convenience. OFF by default; enable with the env
var STRATHON_SEED_DEV_KEY=true (never seeded in cloud mode). The key is

    stra_dev_local_default_project_do_not_use_in_production

The receiver's startup banner detects this key and prints a quickstart
banner when it's present (and silently does not when it's been revoked
in production). Idempotent via ON CONFLICT (id) DO NOTHING.

Revision ID: 003
Revises: 002
Create Date: 2026-05-14

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "003"
down_revision: Union[str, Sequence[str], None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_UPGRADE_SQL = r"""-- ============================================================
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
-- This key's cleartext value is publicly known. Anyone with HTTP access to the receiver
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
"""


_DOWNGRADE_SQL = r"""
DELETE FROM api_keys WHERE id = '00000000-0000-0000-0000-000000000010';
"""


def upgrade() -> None:
    # SECURITY: the seeded key value is publicly known (it ships in this
    # migration). Seeding it into every database — including hosted/cloud
    # tenants — would mean every tenant shares a known credential, breaking
    # tenant isolation. So seeding is OFF by default and must be explicitly
    # opted into for local development via STRATHON_SEED_DEV_KEY=true. It is
    # never seeded in cloud mode regardless of that flag.
    import os

    mode = os.environ.get("STRATHON_MODE", "self-hosted").strip().lower()
    opt_in = os.environ.get("STRATHON_SEED_DEV_KEY", "").strip().lower() in (
        "1", "true", "yes", "on",
    )
    if mode == "cloud" or not opt_in:
        return
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    op.execute(_DOWNGRADE_SQL)
