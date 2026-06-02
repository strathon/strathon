"""Alembic migration environment.

Sync configuration. The receiver itself is async but migrations run as a
one-shot CLI command (or via asyncio.to_thread inside lifespan), so we
don't need the async-template complexity.

Reads DATABASE_URL from the environment so the same config works in dev,
CI, and production. Accepts URLs in any of the formats the receiver uses
internally (postgresql://, postgresql+asyncpg://, postgresql+psycopg://)
and normalizes them to a sync driver Alembic can use.

``target_metadata`` is ``Base.metadata`` (populated by importing the
``models`` package). Migrations are still authored by hand — autogenerate
is used as a drift *guard* (``alembic check``), not to generate migrations.
The ORM models mirror the hand-written schema so that check stays green.
Objects that cannot be round-tripped (the ``audit`` schema, partition child
tables, and the generated ``search_vector`` column) are excluded via
``_include_object`` below.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool


# Alembic Config object, gives access to alembic.ini
config = context.config

# Set up logging from alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _resolve_database_url() -> str:
    """Get the DATABASE_URL from env and convert to a sync-driver URL.

    Strathon's receiver uses psycopg3 async (postgresql+psycopg://). Alembic
    runs synchronously and uses the same psycopg3 driver in sync mode — same
    library, same URL scheme. We accept asyncpg-style URLs for backward
    compatibility and rewrite them.
    """
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL environment variable is required for Alembic. "
            "Example: DATABASE_URL=postgresql://strathon:strathon_dev@localhost:5432/strathon"
        )

    # Normalize to psycopg3. psycopg3 supports both sync (for Alembic) and
    # async (for runtime), so one driver works across the whole stack.
    if url.startswith("postgresql+asyncpg://"):
        url = "postgresql+psycopg://" + url[len("postgresql+asyncpg://"):]
    elif url.startswith("postgresql+psycopg2://"):
        url = "postgresql+psycopg://" + url[len("postgresql+psycopg2://"):]
    elif url.startswith("postgres://"):
        # Heroku-style; SQLAlchemy 2.x requires the full scheme name
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://") and "+" not in url.split("://", 1)[0]:
        url = "postgresql+psycopg://" + url[len("postgresql://"):]

    return url


# Override alembic.ini's sqlalchemy.url (intentionally blank) with the env-resolved value.
config.set_main_option("sqlalchemy.url", _resolve_database_url())


# Wire ORM metadata so `alembic revision --autogenerate` and `alembic check`
# can detect schema drift between the live database and our models.
# Importing the models package registers every table with Base.metadata.
# These imports must happen AFTER `config` is set up (which prepends the
# receiver/ directory to sys.path via alembic.ini), so they're intentionally
# not at the top of the file.
import models  # noqa: E402, F401  -- import for side-effect (registers tables)
from models import Base  # noqa: E402

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emits SQL to stdout, no DB connection.

    Useful for handing migrations to a DBA, or for `alembic upgrade head --sql`.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def _include_object(object_, name, type_, reflected, compare_to):
    """Exclude objects this project manages via hand-written raw SQL.

    Two categories are excluded from alembic check:

    1. The ``audit`` schema — managed entirely by raw SQL migrations
       with BRIN/partial/composite indexes, append-only triggers,
       REVOKE statements, and monthly partition child tables.

    2. Partition child tables for ``spans``, ``span_events``, and
       ``span_links``. Declarative partitioning creates child tables
       (``spans_y2026m05``, ``span_events_test``, etc.) that inherit
       from the parent and have auto-propagated indexes and FK
       constraints. These have no ORM model counterpart; alembic
       would mis-report all of them as drift.
    """
    import re

    schema = getattr(object_, "schema", None)
    if schema is None and hasattr(object_, "table"):
        schema = getattr(object_.table, "schema", None)
    if schema == "audit":
        return False

    # Exclude partition child tables and their indexes/constraints.
    _PARTITION_RE = re.compile(
        r"^(spans|span_events|span_links)_(y\d{4}m\d{2}|test)(_|$)"
    )
    tbl_name = None
    if type_ == "table" and name:
        tbl_name = name
    elif hasattr(object_, "table"):
        tbl_name = getattr(object_.table, "name", None)
    if tbl_name and _PARTITION_RE.match(tbl_name):
        return False

    # Also exclude per-partition FK constraints that reference partition
    # tables (they show up as FK diffs on the parent).
    if type_ == "foreign_key_constraint" and name:
        if re.search(r"fkey\d+$", name):
            return False

    # Exclude the spans.search_vector generated (STORED) tsvector column and
    # its GIN index. Postgres normalizes a GENERATED column's expression on
    # reflection, so autogenerate can never round-trip it cleanly and would
    # perpetually report drift. The column is owned by its raw-SQL migration
    # and still backs full-text search; we just don't let alembic manage it.
    if type_ == "column" and name == "search_vector":
        return False
    if type_ == "index" and name == "idx_spans_search_vector":
        return False

    return True


def run_migrations_online() -> None:
    """Run migrations against a live database connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Render server-side defaults like NOW() correctly
            render_as_batch=False,
            # See _include_object docstring: the audit schema is
            # owned by raw-SQL migration, not autogenerate, so we
            # exclude it from the drift check.
            include_schemas=True,
            include_object=_include_object,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
