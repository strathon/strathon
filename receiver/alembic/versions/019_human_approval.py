"""019 — Human approval workflow.

Add approvals table for the require_approval policy action. When a policy
matches with action=require_approval, the tool call is held pending until
an operator approves or denies it (or it times out).

Also extends the policies.action CHECK constraint to include
'require_approval'.

Revision ID: 019
Revises: 018
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "019"
down_revision = "018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create approvals table.
    op.create_table(
        "approvals",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "project_id", UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("policy_id", UUID(as_uuid=True), nullable=False),
        sa.Column("trace_id", sa.LargeBinary, nullable=True),
        sa.Column("span_id", sa.LargeBinary, nullable=True),
        sa.Column("span_name", sa.Text, nullable=True),
        sa.Column("tool_name", sa.Text, nullable=True),
        sa.Column("tool_args", sa.Text, nullable=True),
        sa.Column("policy_name", sa.Text, nullable=True),
        sa.Column(
            "status", sa.Text, nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "requested_at", sa.TIMESTAMP(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.Text, nullable=True),
        sa.Column(
            "timeout_seconds", sa.Integer, nullable=False,
            server_default=sa.text("300"),
        ),
        sa.Column(
            "expires_at", sa.TIMESTAMP(timezone=True), nullable=False,
            comment="requested_at + timeout_seconds. Background reaper checks this.",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'denied', 'expired')",
            name="approvals_status_check",
        ),
    )
    op.create_index(
        "idx_approvals_project_status",
        "approvals",
        ["project_id", "status"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "idx_approvals_expires_at",
        "approvals",
        ["expires_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )

    # Extend the policies.action CHECK constraint.
    op.execute("ALTER TABLE policies DROP CONSTRAINT IF EXISTS policies_action_check")
    op.execute(
        "ALTER TABLE policies ADD CONSTRAINT policies_action_check "
        "CHECK (action IN ('log', 'alert', 'block', 'steer', 'throttle', "
        "'allow', 'require_approval'))"
    )


def downgrade() -> None:
    # Check for existing require_approval policies.
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM policies WHERE action = 'require_approval') THEN "
        "RAISE EXCEPTION 'Cannot downgrade: require_approval policies exist.'; "
        "END IF; END $$"
    )
    op.execute("ALTER TABLE policies DROP CONSTRAINT IF EXISTS policies_action_check")
    op.execute(
        "ALTER TABLE policies ADD CONSTRAINT policies_action_check "
        "CHECK (action IN ('log', 'alert', 'block', 'steer', 'throttle', 'allow'))"
    )
    op.drop_index("idx_approvals_expires_at", table_name="approvals")
    op.drop_index("idx_approvals_project_status", table_name="approvals")
    op.drop_table("approvals")
