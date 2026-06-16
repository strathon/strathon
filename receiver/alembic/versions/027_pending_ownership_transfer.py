"""Pending ownership transfer: owner-initiated, recipient-accepted.

Ownership transfer is a two-step, consent-based flow. The current owner
initiates a transfer to an existing admin member; the recipient must
explicitly accept before any role change happens. This avoids a unilateral
identity/control move and gives the recipient a chance to decline.

At most one pending transfer may exist per project at a time.

Revises: 026
"""
from alembic import op


revision: str = "027"
down_revision: str = "026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_ownership_transfers (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id UUID NOT NULL UNIQUE
                REFERENCES projects(id) ON DELETE CASCADE,
            from_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            to_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_pending_transfer_to_user "
        "ON pending_ownership_transfers(to_user_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS pending_ownership_transfers")
