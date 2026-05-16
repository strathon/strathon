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
import os
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

    Idempotent — if the database is already at head, this is a no-op
    that costs a few hundred ms. Controllable via STRATHON_AUTO_MIGRATE
    (default true). Set to false if you run migrations as a separate
    deploy step (e.g. a Kubernetes initContainer or a release pipeline
    step), which is the recommended pattern for multi-replica deployments
    where you don't want every replica racing on the upgrade lock.

    Self-healing for pre-Alembic deployments: if the database was
    provisioned by the old raw-SQL migrations (tables exist, but
    alembic_version table is empty or missing), we stamp it to head
    instead of trying to re-run 001. This is a one-time fixup that
    runs automatically on the first restart after upgrading to the
    Alembic-managed schema. Subsequent starts see alembic_version
    populated and run the normal idempotent upgrade.
    """
    auto = os.getenv("STRATHON_AUTO_MIGRATE", "true").lower()
    if auto in ("false", "0", "no", "off"):
        logger.info("Auto-migrate disabled (STRATHON_AUTO_MIGRATE=false); "
                    "skipping alembic upgrade")
        return

    def _migrate_sync() -> None:
        # Imported lazily so the receiver still imports cleanly if alembic
        # ever needs to be optional (e.g. for tests that bypass migrations)
        from alembic import command as alembic_command
        from alembic.config import Config as AlembicConfig
        from sqlalchemy import create_engine, inspect, text as sql_text

        # alembic.ini sits next to main.py in receiver/. We resolve relative
        # to this file so the receiver works regardless of cwd.
        ini_path = os.path.join(os.path.dirname(__file__), "alembic.ini")
        if not os.path.exists(ini_path):
            raise RuntimeError(
                f"alembic.ini not found at {ini_path}. "
                "If you're running migrations separately, set "
                "STRATHON_AUTO_MIGRATE=false to skip this step."
            )

        cfg = AlembicConfig(ini_path)

        # Detect "pre-Alembic" databases that need stamping rather than
        # upgrading. Signal: known table (projects) exists, but no
        # alembic_version row. If we tried to upgrade in that state,
        # 001 would fail with `relation "projects" already exists`.
        # We use a sync engine since this is the sync Alembic context.
        from config import settings as receiver_settings
        sync_engine = create_engine(receiver_settings.sync_database_url, pool_pre_ping=True)
        try:
            with sync_engine.connect() as conn:
                inspector = inspect(conn)
                tables = set(inspector.get_table_names())
                has_pre_alembic_schema = "projects" in tables
                has_alembic_version = "alembic_version" in tables

                version_row_count = 0
                if has_alembic_version:
                    version_row_count = conn.execute(
                        sql_text("SELECT COUNT(*) FROM alembic_version")
                    ).scalar_one()

                needs_stamp = has_pre_alembic_schema and version_row_count == 0
        finally:
            sync_engine.dispose()

        if needs_stamp:
            logger.warning(
                "Detected pre-Alembic database (existing tables, no alembic_version "
                "row). Stamping to head — one-time fixup for the raw-SQL to Alembic "
                "migration. No schema changes applied."
            )
            alembic_command.stamp(cfg, "head")
            logger.info("Database stamped at head")
            return

        # Normal path: empty DB or already-stamped DB. Upgrade is a no-op
        # in the latter case.
        alembic_command.upgrade(cfg, "head")

    logger.info("Running database migrations (alembic upgrade head)...")
    await asyncio.to_thread(_migrate_sync)
    logger.info("Database migrations complete")


async def _restore_webhook_keystore() -> None:
    """Restore the in-memory webhook signing-key cache from operator-supplied env.

    The receiver never persists plaintext signing secrets (only their
    SHA-256 hashes — see webhooks/signing.py). After a process restart
    the keystore is empty and outbound deliveries go unsigned until the
    operator does one of:

      (a) Pass STRATHON_WEBHOOK_SIGNING_SECRETS at boot, a comma-separated
          list of plaintext whsec_* values. This function looks up each
          one's hash against the active rows in webhook_signing_keys,
          finds the matching project_id and id, and registers it in the
          keystore.

      (b) Create new keys via POST /v1/webhook_signing_keys after boot.
          The plaintext is returned in the response and remembered in
          the keystore directly.

    This is the boot path for (a). The env variable is the recommended
    way to keep signed delivery working across restarts in any
    deployment that doesn't have an external secret store.

    Any plaintext we can't match against an active row is logged at
    WARNING and skipped. Common causes: the operator typo'd a value,
    the corresponding key has been revoked, or the env var contains a
    secret from a different deployment.
    """
    raw = os.getenv("STRATHON_WEBHOOK_SIGNING_SECRETS", "").strip()
    if not raw:
        return

    secrets = [s.strip() for s in raw.split(",") if s.strip()]
    if not secrets:
        return

    from database import async_session_maker
    from webhooks.keystore import remember_secret
    from webhooks.signing import hash_secret
    from sqlalchemy import select
    from models.webhooks import WebhookSigningKey

    restored = 0
    skipped = 0

    async with async_session_maker() as session:
        for plaintext in secrets:
            if not plaintext.startswith("whsec_"):
                logger.warning(
                    "STRATHON_WEBHOOK_SIGNING_SECRETS entry does not start with "
                    "'whsec_'; skipping"
                )
                skipped += 1
                continue
            h = hash_secret(plaintext)
            row = await session.scalar(
                select(WebhookSigningKey).where(
                    WebhookSigningKey.secret_hash == h,
                    WebhookSigningKey.revoked_at.is_(None),
                )
            )
            if row is None:
                logger.warning(
                    "STRATHON_WEBHOOK_SIGNING_SECRETS contains a value with "
                    "no matching active signing-key row; skipping. "
                    "Was the key revoked or rotated since this env was set?"
                )
                skipped += 1
                continue
            remember_secret(row.project_id, plaintext, key_id=row.id)
            restored += 1

    logger.info(
        "Webhook keystore restored from env: %d secret(s) loaded, %d skipped",
        restored, skipped,
    )


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
    # Publish the metrics object as a module-level singleton so the
    # Dramatiq actor (which runs outside the FastAPI request cycle and
    # has no access to app.state) can emit webhook send/dlq counters.
    metrics_mod.set_global_metrics(app.state.metrics)

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

    # Webhook sweeper background task. Periodically scans for `pending`
    # delivery rows whose Dramatiq message never landed (Redis blip
    # during dispatch, receiver crash between insert and send, etc.)
    # and re-dispatches them. Without this, the architecture's promise
    # of "durability survives queue outages" is just a comment.
    from webhooks.sweeper import SweeperConfig, SweeperMetrics, sweeper_loop
    from database import async_session_maker
    app.state.webhook_sweeper_config = SweeperConfig.from_env()
    app.state.webhook_sweeper_shutdown = asyncio.Event()
    sweeper_metrics = SweeperMetrics(app.state.metrics)
    app.state.webhook_sweeper_task = asyncio.create_task(
        sweeper_loop(
            app.state.webhook_sweeper_config,
            app.state.webhook_sweeper_shutdown,
            session_maker=async_session_maker,
            metrics=sweeper_metrics,
        ),
        name="strathon.webhook_sweeper",
    )

    # Budget monitor background task. Ticks every N seconds (5 by
    # default), evaluates every active budget across every project,
    # and produces or clears halts depending on whether spend has
    # crossed the threshold. Operator halts are not auto-cleared;
    # only halts the monitor itself produced.
    #
    # Multi-replica safety is via Postgres advisory lock; if multiple
    # receivers run concurrently, only one acquires the lock per tick
    # and the others skip. No new infrastructure dependency.
    import budget_monitor
    app.state.budget_monitor_config = budget_monitor.MonitorConfig.from_env()
    app.state.budget_monitor_shutdown = asyncio.Event()
    app.state.budget_monitor_task = asyncio.create_task(
        budget_monitor.monitor_loop(
            app.state.budget_monitor_config,
            app.state.budget_monitor_shutdown,
            session_maker=async_session_maker,
            metrics=app.state.metrics,
        ),
        name="strathon.budget_monitor",
    )

    # Restore the in-memory webhook signing-key cache from operator-supplied
    # plaintexts. The DB stores only hashes; plaintexts are not recoverable
    # from disk, by design. Operators that want signed deliveries to
    # survive a receiver restart pass the plaintexts via the env var
    # STRATHON_WEBHOOK_SIGNING_SECRETS (comma-separated whsec_*). At boot
    # we hash each one and map it to the matching active row in
    # webhook_signing_keys; if no match, we log and skip (the operator
    # either typo'd a secret or referenced a revoked key).
    await _restore_webhook_keystore()

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

    # Stop the webhook sweeper loop cleanly
    app.state.webhook_sweeper_shutdown.set()
    try:
        await asyncio.wait_for(app.state.webhook_sweeper_task, timeout=10)
    except asyncio.TimeoutError:
        logger.warning("webhook sweeper did not stop in 10s; cancelling")
        app.state.webhook_sweeper_task.cancel()
        try:
            await app.state.webhook_sweeper_task
        except (asyncio.CancelledError, Exception):
            pass

    # Stop the budget monitor loop cleanly
    app.state.budget_monitor_shutdown.set()
    try:
        await asyncio.wait_for(app.state.budget_monitor_task, timeout=10)
    except asyncio.TimeoutError:
        logger.warning("budget monitor did not stop in 10s; cancelling")
        app.state.budget_monitor_task.cancel()
        try:
            await app.state.budget_monitor_task
        except (asyncio.CancelledError, Exception):
            pass

    # Clear the metrics singleton so a subsequent import doesn't see
    # state from a previous lifespan.
    metrics_mod.reset_global_metrics_for_testing()

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
from api import (  # noqa: E402
    api_keys, budgets, halts, health, intervention, model_prices,
    policies, traces, webhook_deliveries, webhook_signing_keys,
)

app.include_router(health.router)
app.include_router(traces.router)
app.include_router(policies.router)
app.include_router(api_keys.router)
app.include_router(intervention.router)
app.include_router(halts.router)
app.include_router(webhook_signing_keys.router)
app.include_router(webhook_deliveries.router)
app.include_router(budgets.router)
app.include_router(model_prices.router)


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
