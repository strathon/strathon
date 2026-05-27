"""LangChain instrumentation for Strathon.

LangGraph builds on LangChain's callback system, and Strathon's
LangGraph instrumentation (``strathon.instrumentation.langgraph``)
hooks into LangChain's ``BaseCallbackHandler`` interface. This means
the same handler that instruments LangGraph also instruments pure
LangChain applications — chains, LLM calls, tool invocations,
retriever steps, and any custom ``Runnable`` that fires callbacks.

This module is a thin entry point: it delegates to the LangGraph
module's ``instrument()`` function, which registers the
``StrathonLangGraphHandler`` with the global callback manager. The
handler works identically for both frameworks.

If you're using LangChain, you can use either::

    from strathon.instrumentation.langchain import instrument
    instrument(client)

or::

    from strathon.instrumentation.langgraph import instrument
    instrument(client)

Both register the same handler.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def instrument(client) -> Optional[object]:
    """Instrument LangChain for trace capture via the LangGraph handler.

    This delegates to ``strathon.instrumentation.langgraph.instrument``
    which registers a ``BaseCallbackHandler`` subclass with LangChain's
    global callback manager. The handler captures chains, LLM calls,
    tool invocations, and retriever steps as OpenTelemetry spans.

    Args:
        client: Strathon Client instance.

    Returns:
        The handler instance if instrumentation succeeded, None if
        LangChain is not installed.
    """
    try:
        import langchain  # noqa: F401
    except ImportError:
        logger.debug("LangChain not installed; skipping instrumentation")
        return None

    try:
        from strathon.instrumentation.langgraph import instrument as _instrument
        return _instrument(client)
    except ImportError:
        logger.warning(
            "LangChain is installed but langgraph instrumentation module "
            "failed to import. Ensure langchain and langgraph are both "
            "installed."
        )
        return None
