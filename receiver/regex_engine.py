"""Linear-time regex engine for the redaction and credential-scanning paths.

Both run attacker-controlled span attribute values through regexes on the ingest
hot path. google-re2 evaluates in linear time; Python's stdlib ``re`` can
backtrack catastrophically, so a crafted value can pin a worker for tens of
seconds (a single-worker denial of service). google-re2 is therefore a hard
dependency, declared in the receiver's pyproject.

If it is somehow missing at runtime -- a musl/Alpine base or unusual arch where
the wheel did not build -- the old code silently fell back to stdlib ``re`` and
reintroduced the ReDoS it was there to prevent, with no log line and no health
signal. This module centralizes the import so the fallback is loud: it logs at
CRITICAL and records ``RE2_AVAILABLE = False`` so ``/ready`` can fail its probe.
Callers import ``engine`` from here instead of importing re2/re themselves.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("strathon.regex_engine")

try:
    import re2 as engine  # type: ignore[import]  # linear-time, no backtracking

    RE2_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only where the wheel is absent
    import re as engine  # type: ignore[assignment, no-redef]  # noqa: F401

    RE2_AVAILABLE = False
    logger.critical(
        "google-re2 is not importable; falling back to the stdlib 're' engine. "
        "This reintroduces catastrophic-backtracking (ReDoS) risk on the ingest "
        "path, where span attribute values are attacker-controlled. Install "
        "google-re2 (a declared dependency). /ready will report not_ready until "
        "it is present."
    )


def regex_engine_ok() -> bool:
    """True when the linear-time engine (re2) is active. Used by /ready."""
    return RE2_AVAILABLE
