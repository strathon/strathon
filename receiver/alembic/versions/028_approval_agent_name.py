"""Add agent_name to approvals.

Approval cards need the agent that triggered the call (gen_ai.agent.name),
not just the operation name. The receiver now records it at approval creation;
this adds the column to store it. Existing rows get NULL and the API/UI fall
back gracefully.

Revises: 027
"""
from alembic import op
import sqlalchemy as sa


revision: str = "028"
down_revision: str = "027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "approvals",
        sa.Column(
            "agent_name", sa.Text, nullable=True,
            comment="Agent that triggered the call (gen_ai.agent.name). "
                    "Null for older rows created before this column existed.",
        ),
    )


def downgrade() -> None:
    op.drop_column("approvals", "agent_name")
