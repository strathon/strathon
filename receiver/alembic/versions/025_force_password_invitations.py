"""Force password change flag and pending invitations.

Revision ID: 025
Revises: 024
"""

from __future__ import annotations
from typing import Sequence, Union
from alembic import op


revision: str = "025"
down_revision: Union[str, Sequence[str], None] = "024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE users "
        "ADD COLUMN IF NOT EXISTS force_password_change BOOLEAN NOT NULL DEFAULT false"
    )

    op.execute("""
        CREATE TABLE IF NOT EXISTS pending_invitations (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            email TEXT NOT NULL,
            project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            role TEXT NOT NULL DEFAULT 'member',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(email, project_id)
        )
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS force_password_change")
    op.execute("DROP TABLE IF EXISTS pending_invitations")
