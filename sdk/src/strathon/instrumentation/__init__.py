"""Framework auto-instrumentation modules."""

import logging
from typing import List, Optional

logger = logging.getLogger(__name__)


SUPPORTED_FRAMEWORKS = [
    "openai_agents",
    "claude_agent",
    "langchain",
    "crewai",
    "autogen",
    "openai",
    "anthropic",
]


def auto_instrument(client, frameworks: Optional[List[str]] = None) -> List[str]:
    """
    Auto-instrument the given frameworks for the client.

    Args:
        client: Strathon Client instance.
        frameworks: List of framework names. If None, instruments all installed frameworks.

    Returns:
        List of frameworks that were successfully instrumented.
    """
    if frameworks is None:
        frameworks = SUPPORTED_FRAMEWORKS

    instrumented = []
    for fw in frameworks:
        if fw not in SUPPORTED_FRAMEWORKS:
            logger.warning("Unknown framework %r; skipping", fw)
            continue

        module_name = f"strathon.instrumentation.{fw}"
        try:
            module = __import__(module_name, fromlist=["instrument"])
            if module.instrument(client):
                instrumented.append(fw)
        except Exception as e:
            logger.error("Failed to instrument %s: %s", fw, e)

    return instrumented
