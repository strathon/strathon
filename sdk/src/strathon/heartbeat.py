"""SDK heartbeat and integrity monitoring.

Heartbeat: background daemon thread sends a lightweight span every
30 seconds so the receiver can detect if the SDK is still running.

Integrity: computes SHA-256 of the calling module's source file on
initialization. Includes the hash in every span as an attribute.
If the hash changes mid-session, the receiver fires an alert.

Usage (automatic):
    client = strathon.Client(api_key="stra_...")
    # Heartbeat starts automatically.
    # Code hash computed automatically.
    # Both stop on client.shutdown() or process exit.
"""

from __future__ import annotations

import atexit
import hashlib
import inspect
import logging
import threading
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import TracerProvider

logger = logging.getLogger("strathon.heartbeat")

_VERSION = "1.1.0"


def compute_code_hash() -> str:
    """Compute SHA-256 of the file that created the Strathon client.

    Walks the call stack to find the first frame outside the strathon
    package. Hashes that file's contents. If the file changes at
    runtime (code injection), the hash will differ from subsequent
    spans, triggering a receiver-side alert.
    """
    try:
        for frame_info in inspect.stack():
            filename = frame_info.filename
            # Skip strathon internals and standard library.
            if "strathon" in filename or "site-packages" in filename:
                continue
            if filename.startswith("<"):
                continue
            try:
                with open(filename, "rb") as f:
                    return hashlib.sha256(f.read()).hexdigest()
            except (OSError, IOError):
                continue
    except Exception:
        pass
    return "unknown"


class HeartbeatThread:
    """Daemon thread that sends heartbeat spans to the receiver.

    The receiver's heartbeat monitor tracks these. If heartbeats
    stop for 2 minutes, the receiver fires an alert indicating
    the agent may have crashed or the SDK was bypassed.
    """

    def __init__(
        self,
        tracer_provider: TracerProvider,
        agent_name: str = "unknown",
        code_hash: str = "unknown",
        interval: float = 30.0,
    ):
        self._tracer = tracer_provider.get_tracer("strathon.heartbeat")
        self._agent_name = agent_name
        self._code_hash = code_hash
        self._interval = interval
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start the heartbeat thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="strathon-heartbeat",
        )
        self._thread.start()
        atexit.register(self.stop)
        logger.debug("Heartbeat thread started (interval=%.0fs)", self._interval)

    def stop(self) -> None:
        """Stop the heartbeat thread."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.debug("Heartbeat thread stopped")

    def _run(self) -> None:
        """Send heartbeat spans in a loop."""
        while not self._stop_event.is_set():
            try:
                with self._tracer.start_as_current_span("strathon.heartbeat") as span:
                    span.set_attribute("strathon.agent.name", self._agent_name)
                    span.set_attribute("strathon.sdk.version", _VERSION)
                    span.set_attribute("strathon.sdk.code_hash", self._code_hash)
                    span.set_attribute("strathon.heartbeat", True)
            except Exception:
                logger.debug("Heartbeat span failed", exc_info=True)

            self._stop_event.wait(self._interval)
