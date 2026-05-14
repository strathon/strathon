"""Structured JSON logging for the Strathon receiver.

Default behavior is unchanged: human-readable logs to stderr via the
standard Python logging config. Setting ``STRATHON_LOG_FORMAT=json``
swaps in a JSON formatter that produces one log record per line — suitable
for Loki, Datadog, CloudWatch Logs Insights, or any other log aggregator
that parses NDJSON.

Each JSON record contains:
    - time     RFC3339 / ISO-8601 with milliseconds
    - level    DEBUG / INFO / WARNING / ERROR / CRITICAL
    - logger   logger name (e.g. strathon.receiver, strathon.receiver.auth)
    - msg      formatted message
    - <extras> any ``extra={...}`` keys the call site passed

Exceptions get serialized into a single string under ``exc_info`` to keep
the record one-line-per-event.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import traceback
from datetime import datetime, timezone


# Keys present on every LogRecord by default. Any attribute on a LogRecord
# not in this set is treated as ``extra`` and serialized into the JSON.
_RESERVED_LOGRECORD_KEYS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "taskName",
}


class JsonFormatter(logging.Formatter):
    """Emit each log record as a single JSON object on one line."""

    def format(self, record: logging.LogRecord) -> str:
        # ISO-8601 with milliseconds, UTC. Matches what Datadog / Loki expect.
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(
            timespec="milliseconds"
        )

        out: dict[str, object] = {
            "time": ts,
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        # Surface caller-attached `extra` kwargs
        for key, value in record.__dict__.items():
            if key in _RESERVED_LOGRECORD_KEYS or key.startswith("_"):
                continue
            try:
                json.dumps(value)  # only include serializable extras
                out[key] = value
            except (TypeError, ValueError):
                out[key] = repr(value)

        if record.exc_info:
            out["exc_info"] = "".join(
                traceback.format_exception(*record.exc_info)
            ).rstrip()

        return json.dumps(out, default=str)


def configure_logging() -> str:
    """Configure root logging based on ``STRATHON_LOG_FORMAT`` env.

    Returns the active format name (``"json"`` or ``"text"``) for telemetry.
    """
    fmt_env = os.getenv("STRATHON_LOG_FORMAT", "text").lower().strip()
    level_env = os.getenv("STRATHON_LOG_LEVEL", "INFO").upper().strip()

    try:
        level = getattr(logging, level_env)
    except AttributeError:
        level = logging.INFO

    # Drop any existing handlers (uvicorn may have configured the root before
    # us; we want a clean single handler we control)
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stderr)
    if fmt_env == "json":
        handler.setFormatter(JsonFormatter())
        active = "json"
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(name)s %(levelname)s %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        active = "text"

    root.addHandler(handler)
    root.setLevel(level)

    # uvicorn sets up its own loggers; let them propagate to root so they
    # get our formatter too.
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        lg = logging.getLogger(name)
        for h in list(lg.handlers):
            lg.removeHandler(h)
        lg.propagate = True

    return active


__all__ = ["JsonFormatter", "configure_logging"]
