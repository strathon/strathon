"""Partition spans, span_events, span_links by RANGE on start_time_unix_nano.

Pre-v1 destructive migration: drops and recreates all three tables as
declarative RANGE partitions on start_time_unix_nano (monthly).

Why destructive: Postgres cannot ALTER an existing table to add
partitioning (PG 16 docs §5.12.2.2). Since there is no production
data yet, the cleanest path is drop-and-recreate.

Key changes from the unpartitioned schema:

  spans PK:        (trace_id, span_id)
                 → (start_time_unix_nano, trace_id, span_id)

  span_events:     adds start_time_unix_nano column;
                   FK becomes composite (start_time_unix_nano, trace_id, span_id);
                   co-partitioned by same monthly RANGE

  span_links:      same treatment as span_events

  No default partition — inserts into missing ranges fail hard
  (``ERROR: no partition of relation "spans" found for row``)
  so the maintenance job surfaces gaps as alerts, not silent data loss.

Partition bounds are [FROM, TO) on BIGINT nanoseconds at UTC
midnight of each month. Indexes are declared on the parent and
propagate to every child partition automatically.

Researched against: PG 16 declarative partitioning docs, Opus
research on FK-to-partitioned-tables (PG 12+ Álvaro Herrera commit),
prepared-statement pruning interaction (Amit Langote / PostgresAI),
peer implementations (Jaeger/Tempo/SigNoz/Honeycomb).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op


revision: str = "012"
down_revision: Union[str, Sequence[str], None] = "011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _month_bounds_ns(year: int, month: int) -> tuple[int, int]:
    """Return [from_ns, to_ns) for a monthly partition at UTC."""
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    # Roll to next month manually to avoid dateutil dependency.
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    return (
        int(start.timestamp()) * 1_000_000_000,
        int(end.timestamp()) * 1_000_000_000,
    )


def _create_partitions_sql(year: int, month: int) -> str:
    """SQL to create one month's partitions for all 3 tables."""
    lo, hi = _month_bounds_ns(year, month)
    suffix = f"y{year}m{month:02d}"
    return f"""
CREATE TABLE IF NOT EXISTS spans_{suffix}
    PARTITION OF spans FOR VALUES FROM ({lo}) TO ({hi});
CREATE TABLE IF NOT EXISTS span_events_{suffix}
    PARTITION OF span_events FOR VALUES FROM ({lo}) TO ({hi});
CREATE TABLE IF NOT EXISTS span_links_{suffix}
    PARTITION OF span_links FOR VALUES FROM ({lo}) TO ({hi});
"""


def upgrade() -> None:
    # ── 1. Drop existing tables (CASCADE handles FK deps) ────────
    op.execute("DROP TABLE IF EXISTS span_events CASCADE")
    op.execute("DROP TABLE IF EXISTS span_links CASCADE")
    op.execute("DROP TABLE IF EXISTS spans CASCADE")

    # ── 2. Recreate spans as partitioned ─────────────────────────
    op.execute("""
CREATE TABLE spans (
    start_time_unix_nano BIGINT NOT NULL,
    trace_id BYTEA NOT NULL,
    span_id BYTEA NOT NULL,
    parent_span_id BYTEA,
    project_id UUID NOT NULL,

    name TEXT NOT NULL,
    kind TEXT NOT NULL
        CHECK (kind IN ('CLIENT', 'INTERNAL', 'SERVER', 'PRODUCER', 'CONSUMER', 'UNSPECIFIED')),
    end_time_unix_nano BIGINT,

    status_code TEXT CHECK (status_code IN ('OK', 'ERROR', 'UNSET')),
    status_message TEXT,

    operation_name TEXT,
    provider_name TEXT,
    request_model TEXT,
    response_model TEXT,
    agent_name TEXT,
    agent_id TEXT,
    tool_name TEXT,
    workflow_name TEXT,
    conversation_id TEXT,

    input_tokens INT,
    output_tokens INT,
    reasoning_tokens INT,
    cache_read_tokens INT,
    cache_creation_tokens INT,

    agent_depth INT,
    spawn_parent_agent_id TEXT,
    spawn_reason TEXT,
    cost_usd NUMERIC(12, 6),
    cost_cumulative_usd NUMERIC(12, 6),
    tokens_subtree_input INT,
    tokens_subtree_output INT,
    cost_subtree_usd NUMERIC(12, 6),

    intervention_state TEXT,
    halt_reason TEXT,

    attributes JSONB NOT NULL DEFAULT '{}',

    FOREIGN KEY (trace_id) REFERENCES traces(id) ON DELETE CASCADE,
    PRIMARY KEY (start_time_unix_nano, trace_id, span_id)
) PARTITION BY RANGE (start_time_unix_nano)
""")

    # ── 3. Recreate span_events, co-partitioned ──────────────────
    op.execute("""
CREATE TABLE span_events (
    start_time_unix_nano BIGINT NOT NULL,
    trace_id BYTEA NOT NULL,
    span_id BYTEA NOT NULL,
    id BIGINT GENERATED ALWAYS AS IDENTITY,
    project_id UUID NOT NULL,
    name TEXT NOT NULL,
    time_unix_nano BIGINT NOT NULL,
    attributes JSONB NOT NULL DEFAULT '{}',

    PRIMARY KEY (start_time_unix_nano, id),
    FOREIGN KEY (start_time_unix_nano, trace_id, span_id)
        REFERENCES spans (start_time_unix_nano, trace_id, span_id)
        ON DELETE CASCADE
) PARTITION BY RANGE (start_time_unix_nano)
""")

    # ── 4. Recreate span_links, co-partitioned ───────────────────
    op.execute("""
CREATE TABLE span_links (
    start_time_unix_nano BIGINT NOT NULL,
    trace_id BYTEA NOT NULL,
    span_id BYTEA NOT NULL,
    id BIGINT GENERATED ALWAYS AS IDENTITY,
    linked_trace_id BYTEA NOT NULL,
    linked_span_id BYTEA NOT NULL,
    attributes JSONB NOT NULL DEFAULT '{}',

    PRIMARY KEY (start_time_unix_nano, id),
    FOREIGN KEY (start_time_unix_nano, trace_id, span_id)
        REFERENCES spans (start_time_unix_nano, trace_id, span_id)
        ON DELETE CASCADE
) PARTITION BY RANGE (start_time_unix_nano)
""")

    # ── 5. Indexes on parents (propagate to all partitions) ──────

    # spans indexes
    op.execute("""
CREATE INDEX idx_spans_project_time
    ON spans (project_id, start_time_unix_nano DESC)
""")
    op.execute("""
CREATE INDEX idx_spans_trace_time
    ON spans (trace_id, start_time_unix_nano)
""")
    op.execute("""
CREATE INDEX idx_spans_parent
    ON spans (trace_id, parent_span_id)
""")
    op.execute("""
CREATE INDEX idx_spans_agent
    ON spans (project_id, agent_name, start_time_unix_nano DESC)
    WHERE agent_name IS NOT NULL
""")
    op.execute("""
CREATE INDEX idx_spans_tool
    ON spans (project_id, tool_name, start_time_unix_nano DESC)
    WHERE tool_name IS NOT NULL
""")
    op.execute("""
CREATE INDEX idx_spans_operation
    ON spans (project_id, operation_name, start_time_unix_nano DESC)
    WHERE operation_name IS NOT NULL
""")
    op.execute("""
CREATE INDEX idx_spans_intervention
    ON spans (project_id, intervention_state)
    WHERE intervention_state IS NOT NULL
      AND intervention_state != 'running'
""")
    op.execute("""
CREATE INDEX idx_spans_cost_window
    ON spans (project_id, end_time_unix_nano)
    WHERE cost_usd IS NOT NULL
""")
    op.execute("""
CREATE INDEX idx_spans_attributes_gin
    ON spans USING GIN (attributes jsonb_path_ops)
""")

    # span_events: index on FK columns for CASCADE performance
    op.execute("""
CREATE INDEX idx_span_events_span
    ON span_events (start_time_unix_nano, trace_id, span_id)
""")
    op.execute("""
CREATE INDEX idx_span_events_time
    ON span_events (time_unix_nano)
""")

    # span_links: index on FK columns
    op.execute("""
CREATE INDEX idx_span_links_span
    ON span_links (start_time_unix_nano, trace_id, span_id)
""")

    # ── 6. Create initial monthly partitions ─────────────────────
    # Current month ± 1 plus 3 months ahead.
    now = datetime.now(timezone.utc)
    year, month = now.year, now.month

    months_to_create = []
    # Previous month
    if month == 1:
        months_to_create.append((year - 1, 12))
    else:
        months_to_create.append((year, month - 1))
    # Current through +3
    for offset in range(5):  # 0..4 = current + 4 ahead
        m = month + offset
        y = year
        while m > 12:
            m -= 12
            y += 1
        months_to_create.append((y, m))

    for y, m in months_to_create:
        op.execute(_create_partitions_sql(y, m))


def downgrade() -> None:
    # Downgrade: drop partitioned tables, recreate unpartitioned
    # originals. This is only safe pre-v1 (no production data).
    op.execute("DROP TABLE IF EXISTS span_events CASCADE")
    op.execute("DROP TABLE IF EXISTS span_links CASCADE")
    op.execute("DROP TABLE IF EXISTS spans CASCADE")

    # Recreate the original unpartitioned schema from migration 001.
    op.execute("""
CREATE TABLE spans (
    trace_id BYTEA NOT NULL,
    span_id BYTEA NOT NULL,
    parent_span_id BYTEA,
    project_id UUID NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL
        CHECK (kind IN ('CLIENT', 'INTERNAL', 'SERVER', 'PRODUCER', 'CONSUMER', 'UNSPECIFIED')),
    start_time_unix_nano BIGINT NOT NULL,
    end_time_unix_nano BIGINT,
    status_code TEXT CHECK (status_code IN ('OK', 'ERROR', 'UNSET')),
    status_message TEXT,
    operation_name TEXT,
    provider_name TEXT,
    request_model TEXT,
    response_model TEXT,
    agent_name TEXT,
    agent_id TEXT,
    tool_name TEXT,
    workflow_name TEXT,
    conversation_id TEXT,
    input_tokens INT,
    output_tokens INT,
    reasoning_tokens INT,
    cache_read_tokens INT,
    cache_creation_tokens INT,
    agent_depth INT,
    spawn_parent_agent_id TEXT,
    spawn_reason TEXT,
    cost_usd NUMERIC(12, 6),
    cost_cumulative_usd NUMERIC(12, 6),
    tokens_subtree_input INT,
    tokens_subtree_output INT,
    cost_subtree_usd NUMERIC(12, 6),
    intervention_state TEXT,
    halt_reason TEXT,
    attributes JSONB NOT NULL DEFAULT '{}',
    FOREIGN KEY (trace_id) REFERENCES traces(id) ON DELETE CASCADE,
    PRIMARY KEY (trace_id, span_id)
)
""")
    op.execute("""
CREATE TABLE span_events (
    id BIGSERIAL PRIMARY KEY,
    trace_id BYTEA NOT NULL,
    span_id BYTEA NOT NULL,
    project_id UUID NOT NULL,
    name TEXT NOT NULL,
    time_unix_nano BIGINT NOT NULL,
    attributes JSONB NOT NULL DEFAULT '{}',
    FOREIGN KEY (trace_id, span_id)
        REFERENCES spans(trace_id, span_id) ON DELETE CASCADE
)
""")
    op.execute("""
CREATE TABLE span_links (
    id BIGSERIAL PRIMARY KEY,
    trace_id BYTEA NOT NULL,
    span_id BYTEA NOT NULL,
    linked_trace_id BYTEA NOT NULL,
    linked_span_id BYTEA NOT NULL,
    attributes JSONB NOT NULL DEFAULT '{}',
    FOREIGN KEY (trace_id, span_id)
        REFERENCES spans(trace_id, span_id) ON DELETE CASCADE
)
""")
    # Recreate original indexes.
    op.execute("CREATE INDEX idx_spans_project_time ON spans(project_id, start_time_unix_nano DESC)")
    op.execute("CREATE INDEX idx_spans_trace_time ON spans(trace_id, start_time_unix_nano)")
    op.execute("CREATE INDEX idx_spans_parent ON spans(trace_id, parent_span_id)")
    op.execute("CREATE INDEX idx_spans_agent ON spans(project_id, agent_name, start_time_unix_nano DESC) WHERE agent_name IS NOT NULL")
    op.execute("CREATE INDEX idx_spans_tool ON spans(project_id, tool_name, start_time_unix_nano DESC) WHERE tool_name IS NOT NULL")
    op.execute("CREATE INDEX idx_spans_operation ON spans(project_id, operation_name, start_time_unix_nano DESC) WHERE operation_name IS NOT NULL")
    op.execute("CREATE INDEX idx_spans_intervention ON spans(project_id, intervention_state) WHERE intervention_state IS NOT NULL AND intervention_state != 'running'")
    op.execute("CREATE INDEX idx_spans_cost_window ON spans(project_id, end_time_unix_nano) WHERE cost_usd IS NOT NULL")
    op.execute("CREATE INDEX idx_spans_attributes_gin ON spans USING GIN (attributes jsonb_path_ops)")
