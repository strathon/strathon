"""Framework auto-instrumentation modules.

Ten frameworks have full implementations: LangGraph, CrewAI,
OpenAI Agents SDK, OpenAI (direct), Anthropic (direct), LangChain,
AutoGen, Claude Agent SDK, Pydantic AI, and Google ADK. No stubs remain.

Calling ``auto_instrument(client)`` discovers and instruments every
installed framework that has a real implementation. Explicitly
requesting a planned-but-not-yet-implemented framework raises
``NotImplementedError`` with guidance on which frameworks are
available today.
"""

import logging
from typing import List, Optional

logger = logging.getLogger(__name__)


# Frameworks with full, tested instrumentation modules.
SUPPORTED_FRAMEWORKS: list[str] = [
    "langgraph",
    "crewai",
    "openai_agents",
    "openai",
    "anthropic",
    "langchain",
    "autogen",
    "claude_agent",
    "pydantic_ai",
    "google_adk",
]

# No planned-but-unimplemented frameworks remain. All eight have
# real instrumentation modules. PLANNED_FRAMEWORKS is kept as an
# empty list for API compatibility (tests reference it).
PLANNED_FRAMEWORKS: list[str] = []


def auto_instrument(client, frameworks: Optional[List[str]] = None) -> List[str]:
    """Auto-instrument the given frameworks for the client.

    Args:
        client: Strathon Client instance.
        frameworks: List of framework names to instrument. If None,
            instruments all installed frameworks that have a real
            implementation (currently: langgraph, crewai,
            openai_agents).

    Returns:
        List of frameworks that were successfully instrumented.

    Raises:
        NotImplementedError: If a framework in ``frameworks`` is
            planned but not yet implemented.
        ValueError: If a framework name is completely unknown.
    """
    if frameworks is None:
        frameworks = list(SUPPORTED_FRAMEWORKS)

    instrumented = []
    for fw in frameworks:
        if fw in PLANNED_FRAMEWORKS:
            raise NotImplementedError(
                f"{fw!r} instrumentation is not yet implemented. "
                f"Supported frameworks: {SUPPORTED_FRAMEWORKS}. "
                f"Use one of those, or open an issue at "
                f"github.com/strathon/strathon to request priority "
                f"for {fw!r}."
            )
        if fw not in SUPPORTED_FRAMEWORKS:
            logger.warning("Unknown framework %r; skipping", fw)
            continue

        module_name = f"strathon.instrumentation.{fw}"
        try:
            module = __import__(module_name, fromlist=["instrument"])
            if module.instrument(client):
                instrumented.append(fw)
        except NotImplementedError:
            raise
        except Exception as e:
            logger.error("Failed to instrument %s: %s", fw, e)

    return instrumented
