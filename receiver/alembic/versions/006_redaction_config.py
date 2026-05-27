"""Extend pii redaction config on project_settings

Migration 001 created project_settings with two PII-related columns:
``pii_redaction_enabled`` (BOOL DEFAULT true) and
``pii_redaction_patterns`` (JSONB DEFAULT '[]'). Until now they were
dead columns — no code read them — and they only supported "match a
regex" with no per-entity action, no key-level action, and no
allowlist mode.

This migration extends the schema to support the full two-layer
redaction config that the new ``receiver/redaction.py`` module
implements:

  * ``pii_redaction_strategy``    {ENTITY_NAME: action} per-entity
                                  action when a value-pattern matches
  * ``pii_redaction_key_actions`` {attribute_key: action} whole-attribute
                                  handling, applied before value scans
  * ``pii_attribute_allowlist``   if non-empty, ONLY listed attribute
                                  keys survive — the strongest privacy
                                  posture

All three default to the empty / passthrough form so existing projects
behave the same after the migration (modulo the redactor module now
actually running, which is gated on ``pii_redaction_enabled`` and the
strategy being non-empty — see api/traces.py for the wiring).

Defaults explained:
  - strategy defaults to '{}': for any detected entity that has no
    explicit entry, the redactor falls back to "redact" (replace with
    [ENTITY_NAME]). Operators tighten by mapping entities to "delete"
    or loosen by mapping to "mask".
  - key_actions defaults to '{}': no key-based redaction unless
    operator opts in. Safest default — won't silently drop attributes.
  - allowlist defaults to '[]': allow all. Allowlist mode only
    activates when the list is non-empty.

The existing ``pii_redaction_patterns`` column remains for backward
compatibility but is now treated as the operator's custom_patterns
input — extra regexes appended after the defaults. Code in
repositories/project_settings.py merges them in.

Revision ID: 006
Revises: 005
Create Date: 2026-05-16

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "006"
down_revision: Union[str, Sequence[str], None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_UPGRADE_SQL = r"""
-- ============================================================
-- 006: Per-entity / per-key / allowlist redaction config
-- ============================================================
--
-- Adds three JSONB columns to project_settings. Each is shaped so the
-- application code (receiver/redaction.py) can consume it directly.

ALTER TABLE project_settings
    ADD COLUMN pii_redaction_strategy JSONB
        NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE project_settings
    ADD COLUMN pii_redaction_key_actions JSONB
        NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE project_settings
    ADD COLUMN pii_attribute_allowlist JSONB
        NOT NULL DEFAULT '[]'::jsonb;

-- We don't add a CHECK constraint validating action names ("redact",
-- "mask", "hash", "delete") at the DB level. The valid set evolves
-- as the redactor gains operators (e.g. v2 might add "encrypt") and
-- per-action validation lives in receiver/redaction.py:validate_*.
-- A bad action name surfaces as a 400 on the eventual API endpoint
-- that writes the config, not as an opaque DB error.
"""


_DOWNGRADE_SQL = r"""
ALTER TABLE project_settings DROP COLUMN IF EXISTS pii_attribute_allowlist;
ALTER TABLE project_settings DROP COLUMN IF EXISTS pii_redaction_key_actions;
ALTER TABLE project_settings DROP COLUMN IF EXISTS pii_redaction_strategy;
"""


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    op.execute(_DOWNGRADE_SQL)
