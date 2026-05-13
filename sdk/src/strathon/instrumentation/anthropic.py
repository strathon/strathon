"""Anthropic instrumentation for Strathon."""

import logging

logger = logging.getLogger(__name__)


def instrument(client) -> bool:
    """
    Instrument Anthropic for trace capture.

    Args:
        client: Strathon Client instance.

    Returns:
        True if instrumentation was successful (framework is installed), False otherwise.
    """
    try:
        import anthropic  # noqa: F401
    except ImportError:
        logger.debug("Anthropic not installed; skipping instrumentation")
        return False

    # TODO: monkey-patch Anthropic APIs to emit Strathon spans
    # - Capture LLM call args, response, tokens, cost
    # - Capture tool/function call args and results
    # - Capture parent-child agent relationships
    # - Emit OpenTelemetry spans with strathon.agent.* attributes

    logger.info("Anthropic instrumentation registered (stub)")
    return True
