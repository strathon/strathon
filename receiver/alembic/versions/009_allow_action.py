"""Extend the policies.action CHECK constraint to allow 'allow'

The 'allow' action is the dual of 'block': it explicitly admits a
matching call and short-circuits further policy evaluation. On its
own, 'allow' is a no-op (the default is already to admit anything
the policies don't explicitly deny). It becomes meaningful when
combined with the project-level ``intervention_default_action``
column (added in migration 001, finally wired into request flow in
this same commit). When that column is set to ``'block'``, the
project enters allow-list mode — a call must be explicitly admitted
by an 'allow' policy or it is denied at the tool boundary.

Migration 008 extended the same CHECK constraint to permit
'throttle'. This migration extends it again to permit 'allow'.
Drops and re-adds inside one BEGIN/COMMIT so the constraint is never
observably absent.

Downgrade refuses with a clear RAISE EXCEPTION if 'allow' rows
exist, mirroring the safety guard in migration 008. Operators who
downgrade past this migration must first delete or rewrite their
'allow' policies.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "009"
down_revision: Union[str, Sequence[str], None] = "008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_UPGRADE_SQL = r"""
ALTER TABLE policies
    DROP CONSTRAINT policies_action_check;

ALTER TABLE policies
    ADD CONSTRAINT policies_action_check
    CHECK (action IN ('log', 'alert', 'block', 'steer', 'throttle', 'allow'));
"""


_DOWNGRADE_SQL = r"""
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM policies WHERE action = 'allow') THEN
        RAISE EXCEPTION
            'Cannot downgrade: allow policies exist. '
            'Delete or rewrite them first, then re-run the downgrade.';
    END IF;
END $$;

ALTER TABLE policies
    DROP CONSTRAINT policies_action_check;

ALTER TABLE policies
    ADD CONSTRAINT policies_action_check
    CHECK (action IN ('log', 'alert', 'block', 'steer', 'throttle'));
"""


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    op.execute(_DOWNGRADE_SQL)
