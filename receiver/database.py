"""Async database engine and session factory.

Single source of all database connectivity for the receiver. Everything
else in the codebase that touches the DB does so through `async_session_maker`
either directly (background tasks) or via the `get_db_session` FastAPI
dependency (request handlers).

Driver: psycopg3 in async mode. Same driver as Alembic uses in sync mode,
so we have one DB library across the whole stack.

Lazy initialization:
    The engine and session maker are NOT constructed at module import.
    They're built on first access via cached factory functions, and
    exposed at module level via PEP 562 __getattr__ so the existing
    `from database import async_session_maker` and `from database import
    engine` import sites keep working unchanged.

    Why: at module import we don't know whether the caller actually
    intends to use the DB. Pre-fix, `import main` required DATABASE_URL
    to be set because constructing the engine at module load read
    settings.async_database_url. That broke Docker image smoke checks,
    IDE indexing, doc generation, and similar "import the graph but
    don't run it" workflows. Deferring construction until first real
    use preserves the fail-fast contract at the right layer of the
    lifecycle: the receiver still won't actually serve requests
    without a configured URL, it just doesn't refuse to import.

Pool sizing notes — chosen for "many small queries" (the receiver's actual
workload, not "few long-running transactions"):

  pool_size=10     baseline connections kept open
  max_overflow=20  burst headroom (30 total under load)
  pool_pre_ping    cheap SELECT 1 before checkout — survives Postgres restarts
  pool_recycle=1800 close+reopen after 30 min so hosted Postgres connection
                    limits don't bite (RDS, Aiven, Neon all enforce these)

expire_on_commit=False is mandatory for async — the default True triggers
lazy-loads after commit which need a session context, raising MissingGreenlet.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


# Cached engine reference, used by dispose_engine() at shutdown. The
# @lru_cache on get_engine() handles the singleton semantics; this
# module global is just so dispose_engine() can check "did we ever build
# one?" without forcing construction during shutdown of an app that
# never actually used the DB.
_engine_built: bool = False


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    """Build (or return cached) async engine.

    First call validates DATABASE_URL via Settings construction; if the
    URL is missing or malformed, the Pydantic ValidationError propagates
    from here with a clear message. Subsequent calls return the cached
    instance.

    Tests that need a fresh engine call ``get_engine.cache_clear()`` and
    similarly ``get_session_maker.cache_clear()``.
    """
    # Imported inside the function so `import database` doesn't trigger
    # Settings construction at module load time. This is the whole point
    # of the lazy pattern.
    from config import get_settings
    settings = get_settings()

    global _engine_built
    _engine_built = True

    return create_async_engine(
        settings.async_database_url,
        echo=settings.db_echo,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_recycle=settings.db_pool_recycle,
        pool_pre_ping=True,
    )


@lru_cache(maxsize=1)
def get_session_maker() -> async_sessionmaker[AsyncSession]:
    """Build (or return cached) async session factory bound to the engine."""
    return async_sessionmaker(
        get_engine(),
        class_=AsyncSession,
        # Critical for async: don't auto-expire objects on commit, otherwise
        # any attribute access after commit triggers a lazy SELECT that can't
        # run outside a session context.
        expire_on_commit=False,
        autoflush=False,
    )


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency. One session per request — one transaction per request.

    Usage in endpoints:

        @router.get("/foo")
        async def foo(session: AsyncSession = Depends(get_db_session)):
            ...

    Transaction model:
        success path → commit
        exception   → rollback, then re-raise

    Repository functions never call session.commit() themselves; that's the
    endpoint boundary's responsibility. This is the standard FastAPI pattern
    and means a single endpoint can compose multiple repository calls into
    one atomic operation without coordinating commits.

    Background tasks (not under FastAPI's Depends) construct their own
    session via `async with async_session_maker() as session:` and commit
    explicitly inside that block.
    """
    session_maker = get_session_maker()
    async with session_maker() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()


async def dispose_engine() -> None:
    """Close the engine's pool. Called from the receiver's lifespan shutdown.

    No-op if no engine was ever built — relevant for tests and for any
    aborted startup that never reached the DB.
    """
    if not _engine_built:
        return
    engine = get_engine()
    await engine.dispose()


def __getattr__(name: str):
    """PEP 562 module attribute hook.

    Existing code does ``from database import async_session_maker`` and
    treats it as a callable factory; we keep that surface working by
    routing the attribute access through the lazy getter. Same for
    ``engine`` — used by any code that needs the engine directly.
    """
    if name == "async_session_maker":
        return get_session_maker()
    if name == "engine":
        return get_engine()
    raise AttributeError(f"module 'database' has no attribute {name!r}")
