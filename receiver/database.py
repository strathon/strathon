"""Async database engine and session factory.

Single source of all database connectivity for the receiver. Everything
else in the codebase that touches the DB does so through `async_session_maker`
either directly (background tasks) or via the `get_db_session` FastAPI
dependency (request handlers).

Driver: psycopg3 in async mode. Same driver as Alembic uses in sync mode,
so we have one DB library across the whole stack.

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

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from config import settings


def _make_engine() -> AsyncEngine:
    """Build the async engine from settings. Called once at module import."""
    return create_async_engine(
        settings.async_database_url,
        echo=settings.db_echo,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_recycle=settings.db_pool_recycle,
        pool_pre_ping=True,
    )


engine: AsyncEngine = _make_engine()


async_session_maker: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine,
    class_=AsyncSession,
    # Critical for async: don't auto-expire objects on commit, otherwise
    # any attribute access after commit triggers a lazy SELECT that can't
    # run outside a session context.
    expire_on_commit=False,
    autoflush=False,
)


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency. One session per request, auto-rollback on error.

    Usage in endpoints:

        @router.get("/foo")
        async def foo(session: AsyncSession = Depends(get_db_session)):
            ...

    The session is open for the duration of the request and closed in the
    finally clause. Exceptions trigger a rollback before re-raising so the
    next request doesn't inherit a poisoned session.
    """
    async with async_session_maker() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Close the engine's pool. Called from the receiver's lifespan shutdown."""
    await engine.dispose()
