"""020 — Multi-party approval.

Add approvers_required, current_approvals, and approval_decisions to
the approvals table for N-of-M approval workflows.

Revision ID: 020
Revises: 019
"""

from alembic import op
import sqlalchemy as sa

revision = "020"
down_revision = "019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "approvals",
        sa.Column(
            "approvers_required",
            sa.Integer,
            nullable=False,
            server_default=sa.text("1"),
            comment="Number of approvals needed to resolve as approved.",
        ),
    )
    op.add_column(
        "approvals",
        sa.Column(
            "current_approvals",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
            comment="Running count of approve decisions received.",
        ),
    )
    op.add_column(
        "approvals",
        sa.Column(
            "approval_decisions",
            sa.JSON,
            nullable=False,
            server_default=sa.text("'[]'::json"),
            comment="Array of {actor, decision, timestamp} records.",
        ),
    )


def downgrade() -> None:
    op.drop_column("approvals", "approval_decisions")
    op.drop_column("approvals", "current_approvals")
    op.drop_column("approvals", "approvers_required")
