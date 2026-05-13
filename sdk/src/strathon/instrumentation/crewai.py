"""CrewAI instrumentation for Strathon."""

import logging

logger = logging.getLogger(__name__)


def instrument(client) -> bool:
    """
    Instrument CrewAI for trace capture.

    Args:
        client: Strathon Client instance.

    Returns:
        True if instrumentation was successful (framework is installed), False otherwise.
    """
    try:
        import crewai  # noqa: F401
    except ImportError:
        logger.debug("CrewAI not installed; skipping instrumentation")
        return False

    # TODO: monkey-patch CrewAI APIs to emit Strathon spans
    # - Capture LLM call args, response, tokens, cost
    # - Capture tool/function call args and results
    # - Capture parent-child agent relationships
    # - Emit OpenTelemetry spans with strathon.agent.* attributes

    logger.info("CrewAI instrumentation registered (stub)")
    return True
