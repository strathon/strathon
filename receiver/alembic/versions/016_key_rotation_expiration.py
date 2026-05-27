"""016 — Key rotation and expiration.

Add deprecated_at, expires_at, and rotated_from_id to api_keys for
graceful key rotation with configurable overlap windows and automatic
key expiration.

Revision ID: 016
Revises: 015
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers
revision = "016"
down_revision = "015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # New columns on api_keys.
    op.add_column(
        "api_keys",
        sa.Column(
            "deprecated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
            comment="Set on rotation. Key still works until expires_at.",
        ),
    )
    op.add_column(
        "api_keys",
        sa.Column(
            "expires_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
            comment="Hard expiry. Auth rejects after this timestamp.",
        ),
    )
    op.add_column(
        "api_keys",
        sa.Column(
            "rotated_from_id",
            UUID(as_uuid=True),
            sa.ForeignKey("api_keys.id", ondelete="SET NULL"),
            nullable=True,
            comment="Links replacement key to the deprecated key it replaced.",
        ),
    )

    # Update partial indexes to also exclude expired keys. The existing
    # indexes filter WHERE revoked_at IS NULL; we need to also filter
    # WHERE (expires_at IS NULL OR expires_at > NOW()). However, Postgres
    # partial indexes don't support NOW() (it's not immutable). Instead,
    # the auth hot path adds the expires_at check in the application
    # layer after the index-backed prefix lookup. We leave the existing
    # partial indexes as-is — they correctly exclude revoked keys, and
    # expired-but-not-yet-revoked keys are a small transient set that
    # the background reaper cleans up quickly.

    # Index on expires_at for the background reaper query.
    op.create_index(
        "idx_api_keys_expires_at",
        "api_keys",
        ["expires_at"],
        postgresql_where=sa.text(
            "expires_at IS NOT NULL AND revoked_at IS NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index("idx_api_keys_expires_at", table_name="api_keys")
    op.drop_column("api_keys", "rotated_from_id")
    op.drop_column("api_keys", "expires_at")
    op.drop_column("api_keys", "deprecated_at")
