"""Shared fixtures for cross-framework integration tests.

These tests boot a real receiver subprocess on a free port pointing at a
real Postgres, then exercise SDK framework integrations against it. The
goal is to catch regressions that pure unit tests can't see: OTLP serialization
over the wire, real auth header parsing, actual policy_matches DB writes,
and attribute name drift between frameworks.

Skipped cleanly if Postgres isn't reachable so contributors without
infrastructure can still run pytest.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Iterator

import psycopg
import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
RECEIVER_DIR = REPO_ROOT / "receiver"
SDK_SRC = REPO_ROOT / "sdk" / "src"

DEV_API_KEY = "stra_dev_local_default_project_do_not_use_in_production"


def _free_port() -> int:
    """Grab an unused TCP port. Race condition with port reuse is fine for
    test fixtures; if it fails we re-roll."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _postgres_reachable(db_url: str) -> bool:
    try:
        # psycopg uses postgresql://; strip any sqlalchemy/asyncpg suffix
        url = db_url.replace("postgresql+asyncpg://", "postgresql://")
        with psycopg.connect(url, connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def database_url() -> str:
    """Resolve DATABASE_URL from env or fall back to local dev default."""
    return os.getenv(
        "DATABASE_URL",
        "postgresql://strathon:strathon_dev@127.0.0.1:5432/strathon",
    )


@pytest.fixture(scope="session")
def receiver(database_url: str) -> Iterator[str]:
    """Boot a receiver subprocess on a free port. Yields the base URL.

    Skips the entire integration test session if Postgres isn't reachable.
    """
    if not _postgres_reachable(database_url):
        pytest.skip(
            "Postgres not reachable at DATABASE_URL — skipping integration tests. "
            "Run `brew services start postgresql@16` (or `docker compose up -d postgres`) "
            "to enable.",
            allow_module_level=False,
        )

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    # Keep the test receiver quiet; we'll grep specific log lines if needed
    env["STRATHON_LOG_LEVEL"] = env.get("STRATHON_LOG_LEVEL", "WARNING")
    # Don't make the test wait for a real retention sweep
    env["STRATHON_RETENTION_ENABLED"] = "false"
    # Speed up the budget monitor for e2e tests. Default in production
    # is 5s; for tests we want sub-second so the e2e test doesn't take
    # half a minute waiting for the natural tick.
    env["STRATHON_BUDGET_EVAL_INTERVAL_SECONDS"] = env.get(
        "STRATHON_BUDGET_EVAL_INTERVAL_SECONDS", "0.5",
    )

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "main:app",
            "--host", "127.0.0.1", "--port", str(port),
            "--log-level", "warning",
        ],
        cwd=str(RECEIVER_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    # Wait for the receiver to answer /health, up to 15s
    deadline = time.time() + 15
    ready = False
    while time.time() < deadline:
        if proc.poll() is not None:
            output = proc.stdout.read() if proc.stdout else "(no output)"
            pytest.fail(
                f"Receiver subprocess exited early with code {proc.returncode}:\n"
                f"{output}"
            )
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=1) as r:
                if r.status == 200:
                    ready = True
                    break
        except Exception:
            time.sleep(0.25)

    if not ready:
        proc.terminate()
        proc.wait(timeout=5)
        pytest.fail(f"Receiver did not become ready at {base_url} within 15s")

    yield base_url

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


@pytest.fixture(scope="session", autouse=True)
def _add_sdk_to_path():
    """Make sure `import strathon` resolves to the local checkout, not any
    pip-installed copy."""
    sdk_str = str(SDK_SRC)
    if sdk_str not in sys.path:
        sys.path.insert(0, sdk_str)
    yield
