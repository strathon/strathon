"""RBAC: extend users and project_members for email/password auth + roles.

The users table was created in 001 with github_id as the primary identity.
RBAC shifts to email+password as the primary auth for the dashboard, keeping
github_id optional for future OAuth linking. The project_members role set
changes from (owner, admin, member) to (owner, admin, operator, viewer) to
match the four-role RBAC model.

Sessions table already exists from 001; we add the relationship column to
link sessions to user email lookups.

Researched: OWASP Password Storage Cheat Sheet (Argon2id params), Langfuse
RBAC model (owner/admin/member/viewer + project-level overrides), LangSmith
RBAC model (org-level + workspace-level + custom roles). Design: fixed
four-role model for open-source tier; custom roles deferred to ee/.

Revision ID: 015
Revises: 014
"""

from alembic import op


revision = "015"
down_revision = "014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    -- 1. Extend users table for email+password auth
    --    github_id becomes optional (future OAuth linking, not primary identity)
    ALTER TABLE users ALTER COLUMN github_id DROP NOT NULL;
    ALTER TABLE users ALTER COLUMN github_username DROP NOT NULL;

    ALTER TABLE users ADD COLUMN IF NOT EXISTS email TEXT;
    ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT;
    ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name TEXT;
    ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;

    -- Unique index on lowercased email for case-insensitive lookups
    CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_lower
        ON users (LOWER(email))
        WHERE email IS NOT NULL;

    -- 2. Update project_members role constraint
    --    Old: owner, admin, member
    --    New: owner, admin, operator, viewer
    ALTER TABLE project_members DROP CONSTRAINT IF EXISTS project_members_role_check;
    ALTER TABLE project_members ADD CONSTRAINT project_members_role_check
        CHECK (role IN ('owner', 'admin', 'operator', 'viewer'));

    -- Add invitation tracking columns
    ALTER TABLE project_members ADD COLUMN IF NOT EXISTS invited_at TIMESTAMPTZ;
    ALTER TABLE project_members ADD COLUMN IF NOT EXISTS accepted_at TIMESTAMPTZ;
    ALTER TABLE project_members ADD COLUMN IF NOT EXISTS invited_by UUID REFERENCES users(id);

    -- Migrate any existing 'member' roles to 'operator' (safe upgrade path)
    UPDATE project_members SET role = 'operator' WHERE role = 'member';
    """)


def downgrade() -> None:
    op.execute("""
    -- Revert project_members role constraint
    UPDATE project_members SET role = 'member' WHERE role = 'operator';
    ALTER TABLE project_members DROP CONSTRAINT IF EXISTS project_members_role_check;
    ALTER TABLE project_members ADD CONSTRAINT project_members_role_check
        CHECK (role IN ('owner', 'admin', 'member'));

    ALTER TABLE project_members DROP COLUMN IF EXISTS invited_at;
    ALTER TABLE project_members DROP COLUMN IF EXISTS accepted_at;
    ALTER TABLE project_members DROP COLUMN IF EXISTS invited_by;

    -- Revert users table
    DROP INDEX IF EXISTS idx_users_email_lower;
    ALTER TABLE users DROP COLUMN IF EXISTS is_active;
    ALTER TABLE users DROP COLUMN IF EXISTS display_name;
    ALTER TABLE users DROP COLUMN IF EXISTS password_hash;
    ALTER TABLE users DROP COLUMN IF EXISTS email;

    -- Restore NOT NULL constraints on github columns
    DELETE FROM users WHERE github_id IS NULL;
    ALTER TABLE users ALTER COLUMN github_id SET NOT NULL;
    ALTER TABLE users ALTER COLUMN github_username SET NOT NULL;
    """)
