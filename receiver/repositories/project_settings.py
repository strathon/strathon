"""Project settings repository — loads per-project knobs.

Today this is single-purpose: ``load_redaction_config`` reads the
PII redaction columns from project_settings and returns the
``RedactionConfig`` shape the redactor wants.

Why a dedicated module
======================

The redactor module (``receiver/redaction.py``) is intentionally pure:
no DB, no SQLAlchemy, no FastAPI — just regex over strings. The
ingest path (``api/traces.py``) needs to bridge "row from DB" to
"redactor-ready config." That bridging belongs neither in the redactor
(it would import DB) nor in the ingest handler (every endpoint that
ever wants redaction would re-implement the conversion). Hence this
module.

Compilation of operator-provided regexes happens here. The DB stores
patterns as plain strings; we compile to ``re.Pattern`` once per
config-load and pass the compiled tuple into the redactor. A bad regex
(syntax error in the operator's string) logs and is skipped — the
ingest path must never fail because of a misconfigured pattern.

Caching
=======

For v1 we don't cache. Each ingest request loads the row fresh. The
project_settings table has one row per project and the typical
read-volume is modest; the simplicity is worth the cost. If profiling
later shows this is a hot path, the right cache layer is at the
SQLAlchemy session-bind level with a short TTL, not an in-process
LRU (which gets stale on multi-receiver deploys).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Tuple
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.core import ProjectSettings
from redaction import RedactionConfig, validate_key_actions, validate_strategy

logger = logging.getLogger("strathon.receiver.repositories.project_settings")


def _compile_custom_patterns(
    raw_patterns: Any,
) -> Tuple[Tuple[str, re.Pattern[str]], ...]:
    """Compile a list of operator-supplied regex strings.

    The DB column ``pii_redaction_patterns`` is JSONB. Two shapes are
    accepted:

      [{"name": "ENTITY_NAME", "regex": "..."}]   — preferred
      ["...", "...", ...]                          — legacy / shorthand;
                                                     auto-named CUSTOM_{N}

    Anything that fails to compile is logged and skipped. We never
    raise from here: a typo in one pattern must not block ingest for
    the whole project.
    """
    if not raw_patterns:
        return ()

    if not isinstance(raw_patterns, list):
        logger.warning(
            "pii_redaction_patterns is not a list (got %r); ignoring",
            type(raw_patterns).__name__,
        )
        return ()

    compiled: list[Tuple[str, re.Pattern[str]]] = []
    for i, entry in enumerate(raw_patterns):
        name: str
        regex: str
        if isinstance(entry, dict):
            name = str(entry.get("name") or f"CUSTOM_{i + 1}")
            regex = str(entry.get("regex") or "")
        elif isinstance(entry, str):
            name = f"CUSTOM_{i + 1}"
            regex = entry
        else:
            logger.warning(
                "pii_redaction_patterns entry %d has unexpected type %r; skipping",
                i, type(entry).__name__,
            )
            continue

        if not regex:
            continue
        try:
            compiled.append((name, re.compile(regex)))
        except re.error as exc:
            logger.warning(
                "pii_redaction_patterns entry %d (name=%r) has invalid regex: %s",
                i, name, exc,
            )

    return tuple(compiled)


async def load_redaction_config(
    session: AsyncSession,
    project_id: UUID,
) -> RedactionConfig:
    """Load the redaction config for one project.

    If the project has no settings row (shouldn't happen — migration
    001 inserts one per project — but defensive) or redaction is
    disabled, returns the passthrough config so the redactor short-
    circuits to a no-op.

    Validation of the strategy / key_actions JSON happens here. A
    malformed config (unknown action name) logs and falls back to the
    safe default of "redact" for every entity / drops the bad key
    rule. Same principle as bad regex: ingest never fails on
    misconfigured redaction settings.
    """
    row = await session.scalar(
        select(ProjectSettings).where(ProjectSettings.project_id == project_id)
    )
    if row is None or not row.pii_redaction_enabled:
        return RedactionConfig.disabled()

    strategy = row.pii_redaction_strategy or {}
    key_actions = row.pii_redaction_key_actions or {}
    allowlist = row.pii_attribute_allowlist or []

    # Defensive: the DB columns are JSONB so they could contain
    # anything. Normalize to the shapes the redactor expects.
    if not isinstance(strategy, dict):
        logger.warning(
            "project %s pii_redaction_strategy is not a dict; using {}",
            project_id,
        )
        strategy = {}
    if not isinstance(key_actions, dict):
        logger.warning(
            "project %s pii_redaction_key_actions is not a dict; using {}",
            project_id,
        )
        key_actions = {}
    if not isinstance(allowlist, list):
        logger.warning(
            "project %s pii_attribute_allowlist is not a list; using []",
            project_id,
        )
        allowlist = []

    # Reject bad action names but don't fail; drop the bad entries
    # and continue with the good ones. Operators see the warning in
    # logs and can fix at their leisure.
    try:
        validate_strategy(strategy)
    except ValueError as exc:
        logger.warning("project %s strategy invalid: %s; clearing", project_id, exc)
        strategy = {}
    try:
        validate_key_actions(key_actions)
    except ValueError as exc:
        logger.warning(
            "project %s key_actions invalid: %s; clearing", project_id, exc,
        )
        key_actions = {}

    custom_patterns = _compile_custom_patterns(row.pii_redaction_patterns)

    return RedactionConfig(
        enabled=True,
        strategy=strategy,
        key_actions=key_actions,
        allowlist=tuple(str(x) for x in allowlist),
        custom_patterns=custom_patterns,
    )


__all__ = ["load_redaction_config"]
