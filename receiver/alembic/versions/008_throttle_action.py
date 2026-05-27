"""Extend the policies.action CHECK constraint to allow 'throttle'

The policies table was created in migration 002 with

    action TEXT NOT NULL CHECK (action IN ('log','alert','block','steer'))

The new 'throttle' action lets operators write rate-limit rules as
ordinary CEL policies (action_config carries max_calls,
window_seconds, scope). The SDK enforces the bucket at the tool
boundary; the receiver only needs to accept and persist the new
action value, which means extending the CHECK constraint.

Postgres has no first-class enum-extension operation for a TEXT +
CHECK pattern, so we drop and re-add the constraint atomically
within the migration's transaction. The window where the constraint
is absent doesn't exist because alembic wraps the upgrade() body in
BEGIN/COMMIT; no other process can sneak an invalid INSERT through.

Constraint name comes from Postgres's default
``<table>_<column>_check`` convention since the original migration
didn't pass an explicit name to the CHECK clause.

Downgrade reverts to the four-action set. Any 'throttle' rows that
exist at downgrade time would violate the old constraint; the
downgrade explicitly bails with a clear error in that case rather
than silently corrupting data — operators who downgrade past this
migration must first delete or rewrite throttle policies.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "008"
down_revision: Union[str, Sequence[str], None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_UPGRADE_SQL = r"""
ALTER TABLE policies
    DROP CONSTRAINT policies_action_check;

ALTER TABLE policies
    ADD CONSTRAINT policies_action_check
    CHECK (action IN ('log', 'alert', 'block', 'steer', 'throttle'));
"""


_DOWNGRADE_SQL = r"""
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM policies WHERE action = 'throttle') THEN
        RAISE EXCEPTION
            'Cannot downgrade: throttle policies exist. '
            'Delete or rewrite them first, then re-run the downgrade.';
    END IF;
END $$;

ALTER TABLE policies
    DROP CONSTRAINT policies_action_check;

ALTER TABLE policies
    ADD CONSTRAINT policies_action_check
    CHECK (action IN ('log', 'alert', 'block', 'steer'));
"""


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    op.execute(_DOWNGRADE_SQL)
