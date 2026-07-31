"""The default-org/project identity must never silently drift.

``bootstrap_identity`` is the single source of truth the runtime uses. The
migrations that first seeded these rows (001, 026) keep their own literal
copies on purpose -- a migration is a frozen historical snapshot and must not
import app code. That means the two could drift if someone edited one and not
the other, which would break a fresh runtime bootstrap against a database the
migrations built. These tests pin them together: if the runtime constants ever
change, this fails, forcing a data migration rather than a silent divergence.
"""

from __future__ import annotations

import pathlib
import re

import bootstrap_identity as ident

_MIGRATIONS = pathlib.Path(__file__).resolve().parent.parent / "alembic" / "versions"


def _read(name: str) -> str:
    return (_MIGRATIONS / name).read_text()


def test_org_id_matches_migration_026():
    """The runtime default-org UUID must equal the one migration 026 seeded."""
    text = _read("026_organizations.py")
    assert f'DEFAULT_ORG_ID = "{ident.DEFAULT_ORG_ID}"' in text, (
        "bootstrap_identity.DEFAULT_ORG_ID no longer matches migration 026. "
        "Existing databases hold data under the migration's value; changing "
        "the default org id requires a data migration, not just an edit."
    )


def test_org_id_is_frozen_sentinel():
    """Guard the exact value, so a change is a deliberate, reviewed event."""
    assert ident.DEFAULT_ORG_ID == "00000000-0000-0000-0000-0000000000aa"


def test_default_project_seeded_by_migration_001():
    """Migration 001 seeds the Default project with the runtime name/slug."""
    text = _read("001_initial_schema.py")
    # Migration 001 seeds: VALUES (<uuid>, 'Default', 'default')
    assert re.search(
        rf"'[0-9a-f-]+',\s*'{ident.DEFAULT_PROJECT_NAME}',\s*'{ident.DEFAULT_PROJECT_SLUG}'",
        text,
    ), (
        "migration 001's seeded Default project name/slug no longer matches "
        "bootstrap_identity."
    )


def test_org_name_and_slug_match_migration_026():
    """Migration 026 seeds the org with the runtime name/slug."""
    text = _read("026_organizations.py")
    assert f"'{ident.DEFAULT_ORG_NAME}', '{ident.DEFAULT_ORG_SLUG}'" in text, (
        "migration 026's seeded org name/slug no longer matches "
        "bootstrap_identity."
    )
