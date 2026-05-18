"""017 — Policy evaluation metrics + cost attribution.

Add match_count and last_matched_at to policies table so operators
can see which policies fire most often without querying policy_matches.
These are updated atomically in the ingest path alongside record_match.

No schema changes needed for the cost attribution endpoint — it queries
existing span columns (cost_usd, agent_name, request_model, start_time_unix_nano).

Revision ID: 017
Revises: 016
"""

from alembic import op
import sqlalchemy as sa

revision = "017"
down_revision = "016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "policies",
        sa.Column(
            "match_count",
            sa.BigInteger,
            nullable=False,
            server_default=sa.text("0"),
            comment="Cumulative count of spans this policy matched.",
        ),
    )
    op.add_column(
        "policies",
        sa.Column(
            "last_matched_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
            comment="Timestamp of the most recent match.",
        ),
    )


def downgrade() -> None:
    op.drop_column("policies", "last_matched_at")
    op.drop_column("policies", "match_count")
