"""Shared span-nesting helper for the framework adapters.

Several adapters translate a framework's own run/session/invocation hierarchy into
OpenTelemetry spans. A span started with no parent context begins a new root trace,
so if an adapter does not parent its child spans, one logical agent run shows up in
the dashboard as several separate agents.

This gives those adapters one correct implementation of "keep a map of open spans by
the framework's own id, and start each child span under its parent's context" -- the
pattern langgraph and openai_agents already use by hand. Adapters whose framework
hands them a stable id per step (google_adk's invocation_id, pydantic_ai's run_id,
claude_agent's session_id) use this directly; autogen, whose tools run in a separate
asyncio task, stashes the parent by an agent/team key and passes it the same way.

The helper is import-safe with no framework installed: it only touches
opentelemetry, which the SDK already depends on.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from opentelemetry import trace as _otel_trace
from opentelemetry.trace import Span as _Span

logger = logging.getLogger("strathon.instrumentation")


class SpanTree:
    """A map of open spans keyed by a framework id, with parent-aware starts.

    Each adapter owns one instance. ``start(key, name, attributes, parent_key)``
    opens a span, parents it under ``parent_key``'s span when that key is still
    open, and remembers it under ``key``. ``end(key, ...)`` closes and forgets it.

    Keys are whatever stable id the framework provides (a run id, invocation id,
    session id, tool-use id, or an adapter-chosen composite). A missing parent_key,
    or a parent that has already closed, yields a root span rather than an error --
    a degraded but valid trace beats a crash inside instrumentation.
    """

    def __init__(self, tracer: Any) -> None:
        self._tracer = tracer
        self._spans: Dict[str, _Span] = {}

    def start(
        self,
        key: str,
        name: str,
        attributes: Optional[Dict[str, Any]] = None,
        parent_key: Optional[str] = None,
    ) -> Optional[_Span]:
        """Start a span under ``parent_key``'s span (or as a root) and record it.

        Returns the span, or None if span creation itself failed. Instrumentation
        must never raise into the framework, so all failure is swallowed and logged.
        """
        try:
            parent_span = self._spans.get(parent_key) if parent_key else None
            ctx = (
                _otel_trace.set_span_in_context(parent_span)
                if parent_span is not None
                else None
            )
            attrs = {k: v for k, v in (attributes or {}).items() if v is not None}
            span = self._tracer.start_span(name=name, context=ctx, attributes=attrs)
            self._spans[key] = span
            return span
        except Exception:
            logger.exception("SpanTree.start failed for key %s", key)
            return None

    def get(self, key: str) -> Optional[_Span]:
        return self._spans.get(key)

    def context_for(self, parent_key: Optional[str]):
        """Return an OTel context parented at ``parent_key``'s span, or None.

        For adapters that start their span through the framework's own tracer but
        still need the Strathon parent (or that must pass a context across an async
        task boundary, as autogen does).
        """
        parent_span = self._spans.get(parent_key) if parent_key else None
        if parent_span is None:
            return None
        return _otel_trace.set_span_in_context(parent_span)

    def end(
        self,
        key: str,
        set_attributes: Optional[Dict[str, Any]] = None,
        status: Any = None,
    ) -> None:
        """Finalize and forget the span for ``key``. Safe if the key is unknown."""
        span = self._spans.pop(key, None)
        if span is None:
            return
        try:
            if set_attributes:
                for k, v in set_attributes.items():
                    if v is not None:
                        span.set_attribute(k, v)
            if status is not None:
                span.set_status(status)
        except Exception:
            logger.exception("SpanTree.end attrs failed for key %s", key)
        finally:
            try:
                span.end()
            except Exception:
                logger.exception("SpanTree.end failed for key %s", key)

    def discard(self, key: str) -> None:
        """End and forget without recording anything (cleanup on error paths)."""
        self.end(key)
