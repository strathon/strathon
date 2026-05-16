"""
Strathon: Open-source observability and runtime control for AI agents.

Quick start:

    from strathon import Client, instrument

    client = Client(
        api_key="your-api-key",
        endpoint="http://localhost:4318",
    )

    instrument(client, frameworks=["openai_agents", "anthropic"])

    # Your agent code runs as normal; Strathon captures traces automatically.
"""

from strathon._version import __version__
from strathon.client import Client
from strathon.config import Config
from strathon.exceptions import (
    StrathonError,
    AuthenticationError,
    ExportError,
    InterventionError,
)
from strathon.policy.types import (
    StrathonHaltExceeded,
    StrathonPolicyBlocked,
)

__all__ = [
    "__version__",
    "Client",
    "Config",
    "StrathonError",
    "AuthenticationError",
    "ExportError",
    "InterventionError",
    "StrathonPolicyBlocked",
    "StrathonHaltExceeded",
    "instrument",
]


def instrument(client, frameworks=None):
    """
    Auto-instrument supported frameworks for the given client.

    Args:
        client: Strathon Client instance.
        frameworks: List of framework names. If None, instruments all installed frameworks.
                    Options: "openai_agents", "claude_agent", "langchain",
                    "crewai", "autogen", "openai", "anthropic".

    Returns:
        List of frameworks that were successfully instrumented.
    """
    from strathon.instrumentation import auto_instrument
    return auto_instrument(client, frameworks)
