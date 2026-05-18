"""Claude Agent SDK instrumentation for Strathon.

Wraps the Claude Agent SDK's ``query()`` function and
``ClaudeSDKClient`` session methods to emit OpenTelemetry spans
for every agent execution.

The Claude Agent SDK (``pip install claude-agent-sdk``, formerly
``claude-code-sdk``) wraps the Claude Code CLI, giving Python code
access to file operations, terminal commands, and multi-step
workflow chaining. This instrumentation captures the high-level
agent session: prompt, response messages, tool usage, and session
metadata.

Note: as of v0.1.81 (May 2026), the SDK exposes ``can_use_tool``
and ``PreToolUse``/``PostToolUse`` hooks on ``ClaudeAgentOptions``
for first-class tool-call interception. A future version of this
module may use those hooks instead of monkey-patching ``query()``.

For raw Anthropic API instrumentation (``anthropic.messages.create``),
use ``strathon.instrumentation.anthropic`` instead.
"""

from __future__ import annotations

import functools
import logging
from typing import Any, Dict

from opentelemetry.trace import Status, StatusCode

logger = logging.getLogger(__name__)

_MAX_ATTR_LEN = 2000
_PATCHED = False


def _truncate(value: Any, max_len: int = _MAX_ATTR_LEN) -> str:
    s = str(value) if value is not None else ""
    if len(s) <= max_len:
        return s
    return s[:max_len] + f"... [truncated {len(s) - max_len} chars]"


def _extract_messages(result) -> str:
    """Best-effort extraction of messages from a query result."""
    parts = []
    if hasattr(result, "__aiter__") or hasattr(result, "__iter__"):
        return ""  # streaming — can't extract without consuming
    messages = getattr(result, "messages", None)
    if messages:
        for msg in messages:
            text = getattr(msg, "content", None) or getattr(msg, "text", None)
            if text:
                parts.append(str(text))
    return _truncate("\n".join(parts))


def _wrap_query(original, tracer):
    """Wrap the module-level query() function."""

    @functools.wraps(original)
    async def wrapper(*args, **kwargs):
        prompt = args[0] if args else kwargs.get("prompt", "")
        span_attrs: Dict[str, Any] = {
            "strathon.framework": "claude_agent_sdk",
            "gen_ai.provider.name": "anthropic",
            "gen_ai.operation.name": "agent_session",
            "gen_ai.agent.name": "claude_agent",
            "strathon.agent.name": "claude_agent",
        }
        if prompt:
            span_attrs["gen_ai.prompt"] = _truncate(str(prompt))

        model = kwargs.get("model")
        if model:
            span_attrs["gen_ai.request.model"] = str(model)

        max_turns = kwargs.get("max_turns")
        if max_turns is not None:
            span_attrs["strathon.agent.max_turns"] = int(max_turns)

        span = tracer.start_span(
            name="claude_agent.query",
            attributes=span_attrs,
        )
        try:
            result = await original(*args, **kwargs)
        except Exception as exc:
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.end()
            raise

        # Extract response.
        response_text = _extract_messages(result)
        if response_text:
            span.set_attribute("gen_ai.completion", response_text)

        # Session metadata if available.
        session_id = getattr(result, "session_id", None)
        if session_id:
            span.set_attribute(
                "gen_ai.conversation.id", str(session_id)
            )

        span.set_status(Status(StatusCode.OK))
        span.end()
        return result

    return wrapper


def _wrap_client_query(original, tracer):
    """Wrap ClaudeSDKClient.query()."""

    @functools.wraps(original)
    async def wrapper(self, *args, **kwargs):
        prompt = args[0] if args else kwargs.get("prompt", "")
        client_name = getattr(self, "name", None) or "claude_agent"
        span_attrs: Dict[str, Any] = {
            "strathon.framework": "claude_agent_sdk",
            "gen_ai.provider.name": "anthropic",
            "gen_ai.operation.name": "client_query",
            "gen_ai.agent.name": str(client_name),
            "strathon.agent.name": str(client_name),
        }
        if prompt:
            span_attrs["gen_ai.prompt"] = _truncate(str(prompt))

        span = tracer.start_span(
            name=f"claude_agent.client.{client_name}",
            attributes=span_attrs,
        )
        try:
            result = await original(self, *args, **kwargs)
        except Exception as exc:
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.end()
            raise

        response_text = _extract_messages(result)
        if response_text:
            span.set_attribute("gen_ai.completion", response_text)

        span.set_status(Status(StatusCode.OK))
        span.end()
        return result

    return wrapper


def instrument(client) -> bool:
    """Instrument the Claude Agent SDK for trace capture.

    Wraps ``claude_agent_sdk.query()`` and
    ``ClaudeSDKClient.query()`` to emit OpenTelemetry spans.

    Args:
        client: Strathon Client instance.

    Returns:
        True if instrumentation was successful, False if the
        Claude Agent SDK is not installed.
    """
    global _PATCHED
    try:
        import claude_agent_sdk  # type: ignore[import-not-found]
    except ImportError:
        logger.debug(
            "Claude Agent SDK not installed; skipping instrumentation"
        )
        return False

    if _PATCHED:
        logger.debug("Claude Agent SDK already instrumented; skipping")
        return True

    tracer = client.tracer

    # Wrap module-level query().
    if hasattr(claude_agent_sdk, "query"):
        claude_agent_sdk.query = _wrap_query(
            claude_agent_sdk.query, tracer
        )

    # Wrap ClaudeSDKClient.query if it exists.
    try:
        from claude_agent_sdk import ClaudeSDKClient
        if hasattr(ClaudeSDKClient, "query"):
            ClaudeSDKClient.query = _wrap_client_query(
                ClaudeSDKClient.query, tracer
            )
    except (ImportError, AttributeError):
        logger.debug("ClaudeSDKClient not available; skipping client patch")

    _PATCHED = True
    logger.info("Claude Agent SDK instrumentation registered")
    return True
