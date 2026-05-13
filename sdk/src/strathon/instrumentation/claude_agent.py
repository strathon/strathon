"""Claude Agent SDK instrumentation for Strathon."""

import logging

logger = logging.getLogger(__name__)


def instrument(client) -> bool:
    """
    Instrument Claude Agent SDK for trace capture.

    Args:
        client: Strathon Client instance.

    Returns:
        True if instrumentation was successful (framework is installed), False otherwise.
    """
    try:
        import anthropic  # noqa: F401
    except ImportError:
        logger.debug("Claude Agent SDK not installed; skipping instrumentation")
        return False

    # TODO: monkey-patch Claude Agent SDK APIs to emit Strathon spans
    # - Capture LLM call args, response, tokens, cost
    # - Capture tool/function call args and results
    # - Capture parent-child agent relationships
    # - Emit OpenTelemetry spans with strathon.agent.* attributes

    logger.info("Claude Agent SDK instrumentation registered (stub)")
    return True
