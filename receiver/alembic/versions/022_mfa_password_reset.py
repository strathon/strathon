"""022 — MFA (TOTP) and password reset.

Add TOTP MFA support to users table and create password_reset_tokens
table for secure password reset flow.

Revision ID: 022
Revises: 021
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, UUID

revision = "022"
down_revision = "021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # MFA columns on users.
    op.add_column(
        "users",
        sa.Column(
            "totp_secret", sa.Text, nullable=True,
            comment="Base32-encoded TOTP secret. Null when MFA not set up.",
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "mfa_enabled", sa.Boolean, nullable=False,
            server_default=sa.text("FALSE"),
            comment="Whether TOTP MFA is active for this account.",
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "backup_codes", ARRAY(sa.Text), nullable=True,
            comment=(
                "SHA-256 hashed single-use backup codes. "
                "Null when MFA not set up."
            ),
        ),
    )

    # Password reset tokens table.
    op.create_table(
        "password_reset_tokens",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id", UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.Text, nullable=False),
        sa.Column(
            "expires_at", sa.TIMESTAMP(timezone=True), nullable=False,
        ),
        sa.Column(
            "used_at", sa.TIMESTAMP(timezone=True), nullable=True,
        ),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "idx_password_reset_tokens_user",
        "password_reset_tokens",
        ["user_id"],
    )
    op.create_index(
        "idx_password_reset_tokens_hash",
        "password_reset_tokens",
        ["token_hash"],
    )


def downgrade() -> None:
    op.drop_index("idx_password_reset_tokens_hash",
                  table_name="password_reset_tokens")
    op.drop_index("idx_password_reset_tokens_user",
                  table_name="password_reset_tokens")
    op.drop_table("password_reset_tokens")
    op.drop_column("users", "backup_codes")
    op.drop_column("users", "mfa_enabled")
    op.drop_column("users", "totp_secret")
