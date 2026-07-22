"""Deduplicate policy match records on (policy_id, trace_id, span_id).

OTLP exporters retry batches on transient failures, which -- prior to this
migration -- caused the same span to be evaluated against the same policy
multiple times, recording a fresh policy_matches row every time and bumping
Policy.match_count each time. Dashboard "hits" then over-counted the true
number of policy fires by however many times the exporter retried.

A single append-only row per (policy_id, trace_id, span_id) is the intent:
one span, one policy fire, one audit row, regardless of transport retries.
The unique index makes ON CONFLICT DO NOTHING work in record_match, so a
duplicate submission no-ops instead of re-inserting.

Revises: 028
"""
from alembic import op


revision: str = "029"
down_revision: str = "028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent: safe to run against a fresh install where 002 already
    # created the table but no unique index existed. CONCURRENTLY would be
    # nice but can't run inside a transactional migration; policy_matches is
    # small enough at v1 scale that a brief exclusive lock is acceptable.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_policy_matches_policy_trace_span "
        "ON policy_matches(policy_id, trace_id, span_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_policy_matches_policy_trace_span")
