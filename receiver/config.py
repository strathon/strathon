"""Typed configuration for the receiver.

Everything env-driven goes through a single `Settings` object, loaded once
at import time. Beats scattering `os.getenv("X", "default")` calls across
the codebase: types are checked, defaults are explicit, malformed values
fail fast at startup rather than at the moment they're first read.

Naming follows the receiver's existing env-var convention:
    STRATHON_<KNOB>  → setting attribute
    DATABASE_URL     → unprefixed (matches Postgres convention)
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All env-driven config for the receiver.

    Loaded once at process start. Access via the module-level `settings`
    singleton. To override for tests, instantiate `Settings(...)` directly
    or use monkeypatching of `settings.attr`.
    """

    model_config = SettingsConfigDict(
        env_prefix="",            # explicit env names per-field via Field(...)
        env_file=None,            # docker-compose handles env injection
        case_sensitive=False,
        extra="ignore",           # unknown env vars are silently ignored, not an error
    )

    # ---- Database ----

    database_url: str = Field(
        ...,                       # required, no default
        alias="DATABASE_URL",
        description=(
            "Postgres connection URL. Accepted formats:\n"
            "  postgresql://user:pw@host:5432/db          (auto-upgraded to psycopg3)\n"
            "  postgresql+psycopg://user:pw@host:5432/db\n"
            "  postgresql+asyncpg://user:pw@host:5432/db  (rewritten to psycopg3)"
        ),
    )
    db_pool_size: int = Field(default=10, alias="STRATHON_DB_POOL_SIZE", ge=1)
    db_max_overflow: int = Field(default=20, alias="STRATHON_DB_MAX_OVERFLOW", ge=0)
    db_pool_timeout: float = Field(default=30.0, alias="STRATHON_DB_POOL_TIMEOUT", gt=0)
    db_pool_recycle: int = Field(default=1800, alias="STRATHON_DB_POOL_RECYCLE", ge=60)
    db_echo: bool = Field(default=False, alias="STRATHON_DB_ECHO")

    # ---- Logging ----

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO", alias="STRATHON_LOG_LEVEL"
    )
    log_format: Literal["text", "json"] = Field(default="text", alias="STRATHON_LOG_FORMAT")

    # ---- Migrations ----

    auto_migrate: bool = Field(default=True, alias="STRATHON_AUTO_MIGRATE")

    # ---- Sampling ----

    sampling_rate: float = Field(
        default=1.0, alias="STRATHON_SAMPLING_RATE", ge=0.0, le=1.0
    )
    expensive_llm_token_threshold: int = Field(
        default=5000, alias="STRATHON_EXPENSIVE_LLM_TOKEN_THRESHOLD", ge=0
    )

    # ---- Retention ----

    retention_enabled: bool = Field(default=True, alias="STRATHON_RETENTION_ENABLED")
    retention_interval_seconds: int = Field(
        default=3600, alias="STRATHON_RETENTION_INTERVAL_SECONDS", ge=60
    )
    retention_batch_size: int = Field(
        default=5000, alias="STRATHON_RETENTION_BATCH_SIZE", ge=1
    )

    # ---- Webhook delivery (alert action) ----
    #
    # The receiver fires webhooks for matched `alert`-action policies. The
    # delivery layer uses Dramatiq + Redis to retry with exponential
    # backoff, with the durable state mirrored in webhook_deliveries.
    #
    # If webhook_redis_url is left empty, Dramatiq's StubBroker is used:
    # actor invocations run inline on the calling thread. That keeps
    # local development and CI free of a Redis dependency — at the cost
    # of putting webhook send latency on the OTLP ingest hot path. Set
    # this URL in any production deployment.

    webhook_redis_url: str = Field(
        default="", alias="STRATHON_WEBHOOK_REDIS_URL",
        description=(
            "Redis connection URL for the Dramatiq webhook broker, e.g.\n"
            "  redis://localhost:6379/0\n"
            "When empty, the in-process StubBroker is used (dev/CI only)."
        ),
    )
    webhook_max_attempts: int = Field(
        default=8, alias="STRATHON_WEBHOOK_MAX_ATTEMPTS", ge=1, le=20,
        description=(
            "Maximum delivery attempts per webhook before dead-lettering. "
            "With min_backoff=1s and max_backoff=6h, 8 attempts covers ~24h "
            "of retry window — matching the recommended 1-3 day total window "
            "from Standard Webhooks operational guidance."
        ),
    )
    webhook_min_backoff_ms: int = Field(
        default=1_000, alias="STRATHON_WEBHOOK_MIN_BACKOFF_MS", ge=100,
    )
    webhook_max_backoff_ms: int = Field(
        default=6 * 60 * 60 * 1000,   # 6 hours
        alias="STRATHON_WEBHOOK_MAX_BACKOFF_MS", ge=1_000,
    )
    webhook_request_timeout_sec: float = Field(
        default=10.0, alias="STRATHON_WEBHOOK_REQUEST_TIMEOUT_SEC", gt=0,
        description=(
            "Per-attempt HTTP timeout. GitHub recommends ~10s windows for "
            "webhook ACKs; we match that. Operators with slow consumers "
            "can extend, but increase max_attempts proportionally."
        ),
    )

    # ---- Rate limiting ----
    # Per-identifier token bucket. Identifier is the API key (hashed
    # from the Authorization header) when present, the client IP
    # otherwise. /health, /ready, and /metrics are always exempt.
    # State is per-process: in a multi-replica deploy each replica
    # holds its own buckets, so the effective ceiling is N replicas x
    # rate_limit_requests_per_second per key. The docs note this.
    rate_limit_enabled: bool = Field(
        default=True, alias="STRATHON_RATE_LIMIT_ENABLED",
        description=(
            "Enable the in-memory per-key token-bucket rate limiter. "
            "Set to false to bypass entirely (useful when running "
            "behind a rate-limiting reverse proxy that already enforces "
            "limits)."
        ),
    )
    rate_limit_requests_per_second: int = Field(
        default=100, alias="STRATHON_RATE_LIMIT_REQUESTS_PER_SECOND", ge=1,
        description=(
            "Sustained per-key throughput. The token bucket refills at "
            "this rate. Default 100/s catches runaway agent loops "
            "(which emit hundreds of spans per second) while leaving "
            "comfortable headroom for normal multi-agent traffic."
        ),
    )
    rate_limit_burst: int = Field(
        default=200, alias="STRATHON_RATE_LIMIT_BURST", ge=1,
        description=(
            "Token-bucket capacity. The maximum momentary burst a key "
            "is allowed before it has to wait for the bucket to refill. "
            "Set higher than requests_per_second to absorb startup "
            "spikes when an agent dispatches several traces in rapid "
            "succession."
        ),
    )

    # ---- Derived properties ----

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, v: str) -> str:
        """Sanity-check the URL has a scheme we can work with."""
        if not v.startswith(("postgresql://", "postgresql+", "postgres://")):
            raise ValueError(
                f"DATABASE_URL must start with postgresql:// or postgresql+driver://, "
                f"got: {v[:30]!r}"
            )
        return v

    @property
    def async_database_url(self) -> str:
        """URL for the async runtime engine. Always normalized to psycopg3 async.

        Accepts asyncpg-style URLs (the receiver previously used asyncpg) and
        rewrites them so a deployment doesn't have to update env vars when we
        change drivers.
        """
        url = self.database_url
        if url.startswith("postgresql+asyncpg://"):
            return "postgresql+psycopg://" + url[len("postgresql+asyncpg://"):]
        if url.startswith("postgres://"):
            return "postgresql+psycopg://" + url[len("postgres://"):]
        if url.startswith("postgresql://") and "+" not in url.split("://", 1)[0]:
            return "postgresql+psycopg://" + url[len("postgresql://"):]
        return url

    @property
    def sync_database_url(self) -> str:
        """URL for sync tooling (Alembic). Same psycopg3 driver in sync mode."""
        url = self.async_database_url
        # psycopg3 is both sync and async with the same package — the SQLAlchemy
        # dialect is the same string. So sync URL == async URL for psycopg3.
        return url


# Lazy singleton via FastAPI's recommended @lru_cache(get_settings) pattern.
#
# We previously did `settings = Settings()` at module load. That made every
# `import config` (transitively, `import database`, `import main`) fail when
# DATABASE_URL wasn't set — including in CI's Docker smoke check, IDE
# indexing, docs generation, anything that imports the app graph without
# wanting to actually run it. The failure was technically correct (URL is
# required at runtime) but applied at the wrong layer of the lifecycle.
#
# The fix per FastAPI's official guidance
# (https://fastapi.tiangolo.com/advanced/settings/) is a cached factory:
# Settings() runs on first call, not on module import. The fail-fast
# contract is preserved — anything that actually needs settings will
# trigger validation on the first real access — but module loading
# becomes side-effect-free.
#
# We additionally expose `settings` as a module-level attribute via PEP 562
# __getattr__ so existing `from config import settings` callsites keep
# working unchanged.

from functools import lru_cache  # noqa: E402  -- placed near use site for clarity


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the validated Settings singleton, building it on first call.

    Cached: subsequent calls return the same instance. To force a rebuild
    (typically only in tests), call ``get_settings.cache_clear()``.
    """
    # database_url is declared required at the Pydantic-field level but
    # is sourced from the DATABASE_URL env var at construction; mypy
    # doesn't see the BaseSettings env-loading and flags it as missing.
    return Settings()  # type: ignore[call-arg]


def __getattr__(name: str):
    """PEP 562 module attribute hook.

    Lets `from config import settings` keep working transparently while
    deferring the actual Settings() construction to first access. Without
    this, every old import site would have to change to call get_settings().
    """
    if name == "settings":
        return get_settings()
    raise AttributeError(f"module 'config' has no attribute {name!r}")
