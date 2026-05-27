"""Policy version history table.

Stores a snapshot of the policy state after every create, update, or
delete. Enables operators to see what changed, when, and restore a
previous version.

The audit log already captures before/after state for every mutation,
but a dedicated versions table provides:
  - Sequential version numbering (v1, v2, v3)
  - Faster queries (no SCIM filter needed)
  - Structured rollback via the API
  - Works independently of audit log configuration
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "013"
down_revision: Union[str, Sequence[str], None] = "012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE policy_versions (
    id BIGSERIAL PRIMARY KEY,
    policy_id UUID NOT NULL REFERENCES policies(id) ON DELETE CASCADE,
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    version INT NOT NULL,

    -- Snapshot of the policy at this version
    name TEXT NOT NULL,
    description TEXT,
    match_expression TEXT NOT NULL,
    action TEXT NOT NULL,
    action_config JSONB NOT NULL DEFAULT '{}',
    applies_to TEXT[] NOT NULL DEFAULT '{}',
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    priority INT NOT NULL DEFAULT 0,

    -- Change metadata
    change_type TEXT NOT NULL
        CHECK (change_type IN ('create', 'update', 'delete')),
    changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (policy_id, version)
)
""")
    op.execute("""
CREATE INDEX idx_policy_versions_policy
    ON policy_versions (policy_id, version DESC)
""")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS policy_versions CASCADE")
