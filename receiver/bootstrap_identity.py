"""Frozen identity of the single default organization and project.

In self-hosted mode Strathon runs single-tenant: one default organization
holding one default project, so a fresh deployment has somewhere to send
traces before anyone creates a real project.

These values are LOAD-BEARING and FROZEN. Existing self-hosted databases
already hold data under these exact IDs and slugs (seeded by migrations 001
and 026), so changing any of them would orphan that data. Changing the default
org identity therefore requires a data migration, not just an edit here.

Single source of truth for the *runtime* bootstrap (``ensure_default_project``
and the lifespan). The migrations deliberately keep their own literal copies:
an Alembic migration is a frozen historical snapshot and must not import app
code, because if this module later changed, replaying an old migration would
seed a different value than the one existing databases were built with.
``tests/test_bootstrap_identity.py`` asserts the migration literals and these
constants still agree, so the two cannot silently drift.
"""

from __future__ import annotations

# Default organization (self-hosted single tenant). Matches migration 026.
DEFAULT_ORG_ID = "00000000-0000-0000-0000-0000000000aa"
DEFAULT_ORG_NAME = "Default"
DEFAULT_ORG_SLUG = "default"

# Default project under that org. Matches migrations 001 and 026.
DEFAULT_PROJECT_NAME = "Default"
DEFAULT_PROJECT_SLUG = "default"

__all__ = [
    "DEFAULT_ORG_ID",
    "DEFAULT_ORG_NAME",
    "DEFAULT_ORG_SLUG",
    "DEFAULT_PROJECT_NAME",
    "DEFAULT_PROJECT_SLUG",
]
