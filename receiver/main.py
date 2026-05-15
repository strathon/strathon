"""Strathon receiver entrypoint.

After stage 6a of the refactor, this module owns three things and
nothing else:

  1. Lifespan: migrations, default project, retention task, quickstart
     banner, engine disposal.
  2. App construction: FastAPI app, router mounting, global exception
     handler.
  3. Two small startup helpers (_run_migrations, _print_quickstart_banner)
     that are tightly coupled to the lifespan and don't fit anywhere else.

All endpoints live in receiver/api/<resource>.py. Shared FastAPI
dependencies (auth, project resolution) live in receiver/api/_deps.py.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import logging_config
import metrics as metrics_mod
import retention
import sampling


# Set up logging FIRST so any subsequent module-level logger.info()s use our format
_active_log_format = logging_config.configure_logging()

logger = logging.getLogger("strathon.receiver")
logger.info("Logging configured: format=%s", _active_log_format)


# Default project slug used when seeding a fresh deployment
DEFAULT_PROJECT_SLUG = "default"

# Well-known UUID of the dev key seeded by migration 003. If this row is
# present and non-revoked, we print a quickstart banner at startup so new
# users immediately see what key to use and how to rotate it.
_SEEDED_DEV_KEY_ID = "00000000-0000-0000-0000-000000000010"
_SEEDED_DEV_KEY_VALUE = "stra_dev_local_default_project_do_not_use_in_production"


async def _print_quickstart_banner() -> None:
    """Print a one-time-readable banner when the seeded dev key is active.

    Looks up the well-known dev key by id. If present and not revoked,
    prints the value, the endpoint, and the rotation reminder. Silent in
    production deployments where the dev key has been revoked.
    """
    from database import async_session_maker
    from repositories.traces import is_dev_key_active

    try:
        async with async_session_maker() as session:
            active = await is_dev_key_active(session, UUID(_SEEDED_DEV_KEY_ID))
    except Exception:
        logger.debug("quickstart banner: failed to check for dev key", exc_info=True)
        return

    if not active:
        return  # Production deployment; nothing to surface

    banner = (
        "\n"
        "============================================================\n"
        "  Strathon receiver ready\n"
        "============================================================\n"
        "  Endpoint:   http://localhost:4318\n"
        "  Dev API key (rotate before production!):\n"
        f"      {_SEEDED_DEV_KEY_VALUE}\n"
        "\n"
        "  Quick test:\n"
        '      curl -H "Authorization: Bearer ' f'{_SEEDED_DEV_KEY_VALUE}" \\\n'
        "           http://localhost:4318/v1/policies\n"
        "\n"
        "  Run a demo:\n"
        "      python examples/intervention_demo.py\n"
        "\n"
        "  To rotate this key, see docs/api_keys.md\n"
        "============================================================\n"
    )
    # Use a fresh stderr write rather than logger.info so the banner reads
    # the same regardless of LOG_FORMAT=json or text.
    sys.stderr.write(banner)
    sys.stderr.flush()


async def _run_migrations() -> None:
    """Run `alembic upgrade head` synchronously, offloaded to a thread.

    Why at startup, not separately: zero-extra-step deploys. The receiver
    container/process boots and is immediately ready — no separate
    migration step required, no operator forgetting to run it. Cost: a
    second of startup time, and in multi-replica deployments the
    migration must be idempotent (Alembic is, by design).

    Disable with STRATHON_AUTO_MIGRATE=false for environments where
    migrations are managed externally.
    """
    import os
    from pathlib import Path

    if os.getenv("STRATHON_AUTO_MIGRATE", "true").lower() in ("false", "0", "no", "off"):
        logger.info("Skipping auto-migration (STRATHON_AUTO_MIGRATE=false)")
        return

    logger.info("Running database migrations (alembic upgrade head)...")

    # Pre-Alembic self-heal: if the database already has tables but no
    # alembic_version row, stamp it at the earliest migration so the
    # subsequent upgrade is a no-op for existing tables. This handles
    # the common case where someone upgrades from a pre-Alembic version.
    try:
        from sqlalchemy import text
        from database import async_session_maker

        async with async_session_maker() as session:
            has_alembic = (await session.execute(text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'alembic_version')"
            ))).scalar()
            has_tables = (await session.execute(text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'projects')"
            ))).scalar()

            if has_tables and not has_alembic:
                logger.warning(
                    "Detected pre-Alembic schema; stamping baseline before upgrade"
                )
                # Run a synchronous stamp inline via a fresh sync connection.
                # We don't need async here; this is a one-time startup op.
                import alembic.command
                from alembic.config import Config as AlembicConfig
                alembic_ini = Path(__file__).parent / "alembic.ini"
                cfg = AlembicConfig(str(alembic_ini))
                await asyncio.to_thread(alembic.command.stamp, cfg, "001")
    except Exception:
        logger.exception(
            "Self-heal stamp failed; continuing to alembic upgrade and hoping for the best"
        )

    try:
        import alembic.command
        from alembic.config import Config as AlembicConfig
        alembic_ini = Path(__file__).parent / "alembic.ini"
        cfg = AlembicConfig(str(alembic_ini))
        await asyncio.to_thread(alembic.command.upgrade, cfg, "head")
        logger.info("Migrations complete")
    except Exception:
        logger.exception("Migrations FAILED — receiver will not start cleanly")
        raise


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start up the receiver: migrations, default project, retention task."""
    # Run migrations FIRST. Background ingest paths assume the schema is
    # current; nothing in this lifespan should run before the DB is ready.
    await _run_migrations()

    logger.info("Strathon receiver starting")

    # Ensure the default project (and its settings row) exists so a fresh
    # deployment has somewhere to send traces before any user creates a
    # real project. Uses its own short-lived session that commits
    # explicitly — we don't yet have a request-scoped session here.
    from database import async_session_maker
    from repositories.traces import ensure_default_project

    async with async_session_maker() as session:
        app.state.default_project_id = await ensure_default_project(
            session, DEFAULT_PROJECT_SLUG
        )
        await session.commit()
    logger.info("Default project id: %s", app.state.default_project_id)

    # Sampling config (env-driven) + counters for /metrics in C4
    app.state.sampling_config = sampling.SamplingConfig.from_env()
    app.state.sampling_counters = sampling.SamplingCounters()
    logger.info(
        "Sampling rate: %.3f (expensive LLM threshold: %d tokens)",
        app.state.sampling_config.sample_rate,
        app.state.sampling_config.expensive_llm_token_threshold,
    )

    # Prometheus metrics container — exposed at /metrics
    app.state.metrics = metrics_mod.StrathonMetrics()
    app.state.metrics.sampling_rate.set(app.state.sampling_config.sample_rate)

    # Retention background task
    app.state.retention_config = retention.RetentionConfig.from_env()
    app.state.retention_shutdown = asyncio.Event()
    retention_counters = metrics_mod.RetentionCounters(app.state.metrics)
    app.state.retention_task = asyncio.create_task(
        retention.retention_loop(
            app.state.retention_config,
            app.state.retention_shutdown,
            metrics_counters=retention_counters,
        ),
        name="strathon.retention",
    )

    # Quickstart banner: when the dev key seeded by migration 003 is still
    # active, surface it loudly. New users see "here's your key, here's the
    # endpoint, here's how to rotate" the moment the container starts.
    await _print_quickstart_banner()

    yield

    logger.info("Strathon receiver shutting down")

    # Stop the retention loop cleanly
    app.state.retention_shutdown.set()
    try:
        await asyncio.wait_for(app.state.retention_task, timeout=10)
    except asyncio.TimeoutError:
        logger.warning("retention task did not stop in 10s; cancelling")
        app.state.retention_task.cancel()
        try:
            await app.state.retention_task
        except (asyncio.CancelledError, Exception):
            pass

    # Close the SQLAlchemy engine pool.
    from database import dispose_engine
    await dispose_engine()


app = FastAPI(
    title="Strathon Receiver",
    version="0.0.1",
    lifespan=lifespan,
)


# Mount routers. Import here (after `app` exists) so router modules can
# stay decoupled from main.py and not see import-order issues.
from api import api_keys, health, intervention, policies, traces  # noqa: E402

app.include_router(health.router)
app.include_router(traces.router)
app.include_router(policies.router)
app.include_router(api_keys.router)
app.include_router(intervention.router)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception) -> JSONResponse:
    """Last-resort exception handler.

    FastAPI's default would return a 500 with a generic body. We log the
    full exception with the request path so it shows up in our structured
    logs and return a small, opaque error body so we don't leak internals
    to callers.
    """
    logger.exception("Unhandled error processing %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"error": "internal_server_error"},
    )
