"""Add full-text search vector to spans.

Adds a stored GENERATED tsvector column combining span name,
agent_name, tool_name, operation_name, and request_model with
weighted ranking (A for name, B for agent/tool, C for operation/model).

Uses 'simple' text search config rather than 'english' because
span names and attributes are identifiers (send_email, gpt-4o,
research-bot) not natural language prose. 'simple' doesn't stem
or remove stop words, which is correct for identifier matching.

The GIN index on the tsvector column enables sub-millisecond
full-text search over spans.

Research: Postgres 18 docs on generated tsvector columns, pganalyze
GIN index guide, and the stored-column approach from danielabaron.me
which showed 100x speedup vs expression-index tsvector on 100K rows.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "014"
down_revision: Union[str, Sequence[str], None] = "013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add stored generated tsvector column to the partitioned parent.
    # This propagates to all existing and future partitions.
    op.execute("""
ALTER TABLE spans ADD COLUMN search_vector tsvector
    GENERATED ALWAYS AS (
        setweight(to_tsvector('simple', coalesce(name, '')), 'A') ||
        setweight(to_tsvector('simple', coalesce(agent_name, '')), 'B') ||
        setweight(to_tsvector('simple', coalesce(tool_name, '')), 'B') ||
        setweight(to_tsvector('simple', coalesce(operation_name, '')), 'C') ||
        setweight(to_tsvector('simple', coalesce(request_model, '')), 'C')
    ) STORED
""")
    # GIN index on the parent propagates to partitions.
    op.execute("""
CREATE INDEX idx_spans_search_vector ON spans USING GIN (search_vector)
""")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_spans_search_vector")
    op.execute("ALTER TABLE spans DROP COLUMN IF EXISTS search_vector")
