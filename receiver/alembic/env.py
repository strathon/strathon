"""Alembic migration environment.

Sync configuration. The receiver itself is async but migrations run as a
one-shot CLI command (or via asyncio.to_thread inside lifespan), so we
don't need the async-template complexity.

Reads DATABASE_URL from the environment so the same config works in dev,
CI, and production. Accepts URLs in any of the formats the receiver uses
internally (postgresql://, postgresql+asyncpg://, postgresql+psycopg://)
and normalizes them to a sync driver Alembic can use.

There is no target_metadata — we don't use SQLAlchemy ORM models in
Strathon. All migrations are hand-written using op.execute() with raw
SQL. Autogenerate is therefore not supported, which is intentional.
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
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
