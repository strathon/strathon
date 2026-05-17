"""AutoGen instrumentation for Strathon.

Monkey-patches ``BaseChatAgent.on_messages`` and
``BaseGroupChat.run`` / ``run_stream`` to emit OpenTelemetry
spans for agent executions and team runs.

AutoGen 0.4+ (``autogen-agentchat``) uses an event-driven
architecture. The core extension point for instrumentation is
wrapping the agent's ``on_messages`` method (which processes
incoming messages and returns a response) and the team's
``run`` method (which orchestrates multi-agent collaboration).

This instrumentation captures:
- Agent name + type
- Input/output messages
- Token usage from ``RequestUsage`` in responses
- Team-level task spans (from ``run``/``run_stream``)

For deeper per-LLM-call granularity, AutoGen's native OTel
support (``autogen_core`` tracing) can be used alongside this
integration.
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


def _extract_message_content(messages) -> str:
    """Best-effort extraction of text content from AutoGen messages."""
    parts = []
    for msg in (messages or []):
        content = getattr(msg, "content", None)
        if content:
            parts.append(str(content))
    return _truncate("\n".join(parts))


def _extract_usage(response) -> Dict[str, Any]:
    """Extract token usage from an AutoGen Response."""
    attrs: Dict[str, Any] = {}
    chat_msg = getattr(response, "chat_message", None)
    if chat_msg is None:
        return attrs
    # AutoGen messages may carry model_usage metadata.
    models_usage = getattr(chat_msg, "models_usage", None)
    if models_usage:
        total_input = sum(
            getattr(u, "prompt_tokens", 0) or 0 for u in models_usage
        )
        total_output = sum(
            getattr(u, "completion_tokens", 0) or 0 for u in models_usage
        )
        if total_input:
            attrs["gen_ai.usage.input_tokens"] = total_input
        if total_output:
            attrs["gen_ai.usage.output_tokens"] = total_output
    return attrs


def _wrap_on_messages(original, tracer):
    """Wrap BaseChatAgent.on_messages to emit a span per agent call."""

    @functools.wraps(original)
    async def wrapper(self, messages, cancellation_token=None):
        agent_name = getattr(self, "name", None) or type(self).__name__
        span_attrs: Dict[str, Any] = {
            "strathon.framework": "autogen",
            "gen_ai.agent.name": str(agent_name),
            "strathon.agent.name": str(agent_name),
            "gen_ai.operation.name": "agent_call",
            "gen_ai.provider.name": "autogen",
        }
        if messages:
            span_attrs["gen_ai.prompt"] = _extract_message_content(messages)

        span = tracer.start_span(
            name=f"autogen.agent.{agent_name}",
            attributes=span_attrs,
        )
        try:
            response = await original(self, messages, cancellation_token)
        except Exception as exc:
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.end()
            raise

        # Extract response content and usage.
        chat_msg = getattr(response, "chat_message", None)
        if chat_msg:
            content = getattr(chat_msg, "content", None)
            if content:
                span.set_attribute(
                    "gen_ai.completion", _truncate(str(content))
                )
        for k, v in _extract_usage(response).items():
            span.set_attribute(k, v)

        span.set_status(Status(StatusCode.OK))
        span.end()
        return response

    return wrapper


def _wrap_team_run(original, tracer):
    """Wrap BaseGroupChat.run to emit a span per team execution."""

    @functools.wraps(original)
    async def wrapper(self, *args, **kwargs):
        team_name = getattr(self, "_team_id", None) or type(self).__name__
        task = None
        if args:
            task = args[0]
        elif "task" in kwargs:
            task = kwargs["task"]

        span_attrs: Dict[str, Any] = {
            "strathon.framework": "autogen",
            "gen_ai.workflow.name": str(team_name),
            "gen_ai.operation.name": "team_run",
        }
        if task:
            span_attrs["gen_ai.prompt"] = _truncate(str(task))

        span = tracer.start_span(
            name=f"autogen.team.{team_name}",
            attributes=span_attrs,
        )
        try:
            result = await original(self, *args, **kwargs)
        except Exception as exc:
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.end()
            raise

        # Extract final message from TaskResult.
        messages = getattr(result, "messages", None)
        if messages:
            span.set_attribute(
                "gen_ai.completion",
                _extract_message_content(messages[-3:]),
            )
        stop_reason = getattr(result, "stop_reason", None)
        if stop_reason:
            span.set_attribute(
                "gen_ai.response.finish_reason", _truncate(str(stop_reason))
            )
        span.set_status(Status(StatusCode.OK))
        span.end()
        return result

    return wrapper


def instrument(client) -> bool:
    """Instrument AutoGen AgentChat for trace capture.

    Monkey-patches ``BaseChatAgent.on_messages`` and
    ``BaseGroupChat.run`` to emit OpenTelemetry spans.

    Args:
        client: Strathon Client instance.

    Returns:
        True if instrumentation was successful, False if AutoGen
        is not installed.
    """
    global _PATCHED
    try:
        from autogen_agentchat.agents import (  # type: ignore[import-not-found]
            BaseChatAgent,
        )
    except ImportError:
        logger.debug("AutoGen not installed; skipping instrumentation")
        return False

    if _PATCHED:
        logger.debug("AutoGen already instrumented; skipping")
        return True

    tracer = client.tracer

    # Patch agent-level on_messages.
    BaseChatAgent.on_messages = _wrap_on_messages(
        BaseChatAgent.on_messages, tracer
    )

    # Patch team-level run if available.
    try:
        from autogen_agentchat.teams import BaseGroupChat
        original_run = BaseGroupChat.run
        BaseGroupChat.run = _wrap_team_run(original_run, tracer)
    except (ImportError, AttributeError):
        logger.debug("BaseGroupChat.run not available; skipping team patch")

    _PATCHED = True
    logger.info("AutoGen instrumentation registered")
    return True
