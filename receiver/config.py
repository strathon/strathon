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


# Singleton, loaded once at import. Tests can override by passing
# overrides=Settings(database_url=...) where needed.
settings = Settings()
