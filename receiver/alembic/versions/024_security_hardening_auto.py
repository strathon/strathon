"""Account lockout, concurrent session cap, approval optimistic locking.

- users: failed_login_attempts INT, locked_until TIMESTAMPTZ
- approvals: version INT for optimistic locking
- sessions: max concurrent enforced at application level (no schema change)

Revision ID: 024
Revises: 023
"""

from __future__ import annotations
from typing import Sequence, Union
from alembic import op


revision: str = "024"
down_revision: Union[str, Sequence[str], None] = "023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Account lockout columns.
    op.execute(
        "ALTER TABLE users "
        "ADD COLUMN IF NOT EXISTS failed_login_attempts INT NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE users "
        "ADD COLUMN IF NOT EXISTS locked_until TIMESTAMPTZ"
    )

    # Approval optimistic locking.
    op.execute(
        "ALTER TABLE approvals "
        "ADD COLUMN IF NOT EXISTS version INT NOT NULL DEFAULT 1"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS failed_login_attempts")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS locked_until")
    op.execute("ALTER TABLE approvals DROP COLUMN IF EXISTS version")
