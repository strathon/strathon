"""Organizations: the tenancy layer above projects.

Introduces an ``organizations`` table and an ``org_id`` foreign key on
``projects``. Every existing project is backfilled into a single default
organization, so self-hosted deployments continue to work unchanged with
one (effectively invisible) organization.

Project ``slug`` uniqueness moves from global to per-organization. On a
single-tenant self-host this is a no-op; it lets a multi-tenant deployment
have two organizations that each name a project the same thing.

The ``cloud_*`` columns are nullable and unused on self-host. They exist so
hosted billing/usage metering can attach later without another migration.

Revision ID: 026
Revises: 025
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "026"
down_revision: Union[str, Sequence[str], None] = "025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Fixed UUID for the single default organization. Deterministic so the
# bootstrap and this migration agree without a lookup.
DEFAULT_ORG_ID = "00000000-0000-0000-0000-0000000000aa"


def upgrade() -> None:
    # 1. organizations table.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS organizations (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name        TEXT NOT NULL,
            slug        TEXT NOT NULL UNIQUE,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            deleted_at  TIMESTAMPTZ,
            -- Hosted-only fields; null and unused on self-host.
            cloud_plan                  TEXT,
            cloud_billing_cycle_anchor  TIMESTAMPTZ,
            cloud_current_cycle_usage   BIGINT,
            cloud_config                JSONB
        )
        """
    )

    # 2. The single default organization. Idempotent.
    op.execute(
        f"""
        INSERT INTO organizations (id, name, slug)
        VALUES ('{DEFAULT_ORG_ID}'::uuid, 'Default', 'default')
        ON CONFLICT (id) DO NOTHING
        """
    )

    # 3. projects.org_id, nullable first so the backfill can run.
    op.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS org_id UUID")

    # 4. Backfill: every existing project joins the default organization.
    op.execute(
        f"UPDATE projects SET org_id = '{DEFAULT_ORG_ID}'::uuid "
        "WHERE org_id IS NULL"
    )

    # 5. Enforce NOT NULL + FK now that every row has an org.
    op.execute("ALTER TABLE projects ALTER COLUMN org_id SET NOT NULL")
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'fk_projects_org'
            ) THEN
                ALTER TABLE projects
                    ADD CONSTRAINT fk_projects_org
                    FOREIGN KEY (org_id) REFERENCES organizations (id)
                    ON DELETE CASCADE;
            END IF;
        END $$
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_projects_org ON projects (org_id)")

    # 6. Slug uniqueness: global -> per-organization.
    #    Drop the old global UNIQUE constraint (named projects_slug_key by
    #    Postgres when declared inline) and the partial slug index, then add
    #    a per-org partial unique index over live (non-deleted) projects.
    op.execute("ALTER TABLE projects DROP CONSTRAINT IF EXISTS projects_slug_key")
    op.execute("DROP INDEX IF EXISTS idx_projects_slug")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_projects_org_slug "
        "ON projects (org_id, slug) WHERE deleted_at IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_projects_org_slug")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_slug "
        "ON projects (slug) WHERE deleted_at IS NULL"
    )
    op.execute("DROP INDEX IF EXISTS idx_projects_org")
    op.execute("ALTER TABLE projects DROP CONSTRAINT IF EXISTS fk_projects_org")
    op.execute("ALTER TABLE projects DROP COLUMN IF EXISTS org_id")
    op.execute("DROP TABLE IF EXISTS organizations")
