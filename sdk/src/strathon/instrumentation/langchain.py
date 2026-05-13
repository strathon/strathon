"""LangChain instrumentation for Strathon."""

import logging

logger = logging.getLogger(__name__)


def instrument(client) -> bool:
    """
    Instrument LangChain for trace capture.

    Args:
        client: Strathon Client instance.

    Returns:
        True if instrumentation was successful (framework is installed), False otherwise.
    """
    try:
        import langchain  # noqa: F401
    except ImportError:
        logger.debug("LangChain not installed; skipping instrumentation")
        return False

    # TODO: monkey-patch LangChain APIs to emit Strathon spans
    # - Capture LLM call args, response, tokens, cost
    # - Capture tool/function call args and results
    # - Capture parent-child agent relationships
    # - Emit OpenTelemetry spans with strathon.agent.* attributes

    logger.info("LangChain instrumentation registered (stub)")
    return True
