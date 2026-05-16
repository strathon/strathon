"""Per-span cost + budget windows + per-project price overrides

Three concerns bundled because they ship together as part of the
cost-budget feature; splitting them into separate migrations would
just produce a partial schema between commits.

Changes:

1. spans.cost_usd
   NUMERIC(12, 8). Nullable because non-LLM spans (tool calls,
   generic operations) don't have a cost. Indexed on
   (project_id, end_time_unix_nano) WHERE cost_usd IS NOT NULL for
   the budget aggregation hot path. The partial index keeps the
   index small (most spans in a normal workload aren't LLM spans).

2. budgets extensions
   Adds scope/scope_value (project|agent|model), budget_duration
   ('1h'|'1d'|'7d'|'30d'), budget_reset_at, last_evaluated_at.
   The base table existed from migration 001 (schema-only artifact
   of an earlier design) but lacked the columns the v1 budget
   feature needs.

3. model_price_overrides
   Per-project per-model price overrides. The vendored catalog at
   receiver/data/model_prices.json is the default; this table is
   where operators express "we negotiated a discount with our
   provider, our gpt-4o is $2/M not $2.50/M". Unique
   (project_id, model_name) so each model has one override per
   project.

Revision ID: 007
Revises: 006
Create Date: 2026-05-16
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "007"
down_revision: Union[str, Sequence[str], None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_UPGRADE_SQL = r"""
-- ============================================================
-- 007: Per-span cost + budget windows + price overrides
-- ============================================================

-- Per-span cost in USD is already a column on spans (migration 001
-- declared it for the SDK's strathon.agent.cost.usd attribute, never
-- wired up). We reuse it instead of adding a new column. The
-- NUMERIC(12, 6) precision rounds at $10^-6 = 1 microdollar, which
-- is below the per-token cost of even the cheapest models — fine
-- for budget-grading aggregation (sums over thousands of spans).

-- Partial index on (project_id, end_time_unix_nano) where cost is
-- set. The budget monitor's aggregation query is exactly this
-- shape: SUM(cost_usd) WHERE project_id = ? AND end_time > N.
-- Partial keeps the index small — only LLM spans have cost, which
-- is a small fraction of total span volume.
CREATE INDEX idx_spans_cost_window
    ON spans(project_id, end_time_unix_nano)
    WHERE cost_usd IS NOT NULL;

-- Extend budgets table with the columns the v1 feature needs.
-- The base table has been schema-only since migration 001; we
-- add the operator-facing surface here.

-- Iteration budgets don't have a max_spend_usd. The legacy schema
-- declared it NOT NULL because cost was the only kind of budget
-- the early design contemplated. Drop the not-null so iteration
-- budgets can exist alongside cost budgets in the same table.
ALTER TABLE budgets ALTER COLUMN max_spend_usd DROP NOT NULL;

ALTER TABLE budgets ADD COLUMN scope TEXT NOT NULL DEFAULT 'project';
ALTER TABLE budgets ADD CONSTRAINT budgets_scope_check
    CHECK (scope IN ('project', 'agent', 'model'));

-- scope_value: agent_id for scope=agent, model_name for scope=model,
-- NULL for scope=project. We don't constrain shape per scope value
-- at the DB level; the repository validates.
ALTER TABLE budgets ADD COLUMN scope_value TEXT;

-- Window duration. '1h' / '1d' / '7d' / '30d' for v1; the column is
-- TEXT so a future commit can add e.g. '12h' or 'rolling-24h'
-- without a schema change.
ALTER TABLE budgets ADD COLUMN budget_duration TEXT;
ALTER TABLE budgets ADD CONSTRAINT budgets_duration_check
    CHECK (budget_duration IN ('1h', '1d', '7d', '30d') OR budget_duration IS NULL);

-- When this window's spend counter rolls over. Computed by the
-- repository on create, advanced by the monitor when crossed.
ALTER TABLE budgets ADD COLUMN budget_reset_at TIMESTAMPTZ;

-- Bookkeeping: monitor stamps this on each tick.
ALTER TABLE budgets ADD COLUMN last_evaluated_at TIMESTAMPTZ;

-- Index for the monitor's "active budgets that need evaluating"
-- query. We hit this every 5s on a single replica; if it ever
-- becomes a hot path the next step is to add a generation column
-- and dedupe across replicas.
CREATE INDEX idx_budgets_active_for_monitor
    ON budgets(project_id, last_evaluated_at NULLS FIRST)
    WHERE is_active = true;

-- ============================================================
-- model_price_overrides: per-project per-model price overrides
-- ============================================================
--
-- The vendored catalog in data/model_prices.json is the default.
-- This table lets operators override per project. Typical use case:
-- a customer who's negotiated a discount with their provider and
-- wants cost dashboards / budget enforcement to reflect that.

CREATE TABLE model_price_overrides (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,

    model_name TEXT NOT NULL,
    -- 16 digits, 12 decimal places: prices can be in the 10^-9 range
    -- (some cheap models charge per million tokens with prices like
    -- $0.00000005/token). 16 total covers $9999.999999999999, plenty.
    input_cost_per_token NUMERIC(16, 12) NOT NULL,
    output_cost_per_token NUMERIC(16, 12) NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT model_price_overrides_project_model_unique
        UNIQUE (project_id, model_name),

    CONSTRAINT model_price_overrides_nonnegative
        CHECK (input_cost_per_token >= 0 AND output_cost_per_token >= 0)
);

CREATE INDEX idx_model_price_overrides_project
    ON model_price_overrides(project_id);
"""


_DOWNGRADE_SQL = r"""
DROP TABLE IF EXISTS model_price_overrides;

DROP INDEX IF EXISTS idx_budgets_active_for_monitor;

ALTER TABLE budgets DROP COLUMN IF EXISTS last_evaluated_at;
ALTER TABLE budgets DROP COLUMN IF EXISTS budget_reset_at;
ALTER TABLE budgets DROP CONSTRAINT IF EXISTS budgets_duration_check;
ALTER TABLE budgets DROP COLUMN IF EXISTS budget_duration;
ALTER TABLE budgets DROP COLUMN IF EXISTS scope_value;
ALTER TABLE budgets DROP CONSTRAINT IF EXISTS budgets_scope_check;
ALTER TABLE budgets DROP COLUMN IF EXISTS scope;
-- Restore not-null on max_spend_usd. Will fail if iteration budgets
-- exist; operators downgrading need to delete them first.
ALTER TABLE budgets ALTER COLUMN max_spend_usd SET NOT NULL;

DROP INDEX IF EXISTS idx_spans_cost_window;
-- cost_usd column itself existed before this migration; don't drop it.
"""


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    op.execute(_DOWNGRADE_SQL)
