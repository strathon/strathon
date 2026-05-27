"""018 — Shadow mode for policies.

Add shadow boolean to policies table. Shadow policies evaluate during
ingest and record matches, but never enforce (no block, steer, throttle).
Log and alert actions still fire for shadow policies so operators can
observe webhook alerts before promoting to enforcement.

Revision ID: 018
Revises: 017
"""

from alembic import op
import sqlalchemy as sa

revision = "018"
down_revision = "017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "policies",
        sa.Column(
            "shadow",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("FALSE"),
            comment=(
                "Shadow mode: policy evaluates and records matches "
                "but does not enforce block/steer/throttle actions. "
                "Log and alert actions still fire."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("policies", "shadow")
