"""AutoGen instrumentation for Strathon.

Two-layer instrumentation:

1. **Agent/team-level (monkey-patch)**: wraps
   ``BaseChatAgent.on_messages`` and ``BaseGroupChat.run`` /
   ``run_stream`` to emit OpenTelemetry spans for agent executions
   and team runs.

2. **Tool-level (monkey-patch)**: wraps ``BaseTool.run_json`` to
   evaluate CEL policies before each tool call. Block/steer/throttle
   decisions prevent tool execution. Emits per-tool OTel spans.

AutoGen 0.4+ (``autogen-agentchat``) is in maintenance mode as of
May 2026. Microsoft Agent Framework 1.0 GA is the successor. This
module continues to support AutoGen for existing deployments.

This instrumentation captures:
- Agent name + type
- Input/output messages
- Token usage from ``RequestUsage`` in responses
- Team-level task spans (from ``run``/``run_stream``)
- Per-tool policy enforcement and spans (from ``run_json``)
"""

from __future__ import annotations

import functools
import json
import logging
import time
from typing import Any, Dict, Optional

from opentelemetry.trace import Status, StatusCode

logger = logging.getLogger(__name__)

_MAX_ATTR_LEN = 2000
_PATCHED = False
_TOOL_PATCHED = False
_ORIGINAL_RUN_JSON = None
_PATCHED_CLIENT: Optional[Any] = None


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


# ---------------------------------------------------------------------------
# Layer 2: Tool-level policy enforcement (BaseTool.run_json)
# ---------------------------------------------------------------------------


def _safe_str(value: Any) -> str:
    try:
        return str(value) if value is not None else ""
    except Exception:
        return "<unrepr>"


def _json_or_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, default=_safe_str)
        except Exception:
            return _safe_str(value)
    return _safe_str(value)


def _install_tool_patch(client) -> bool:
    """Patch BaseTool.run_json so policy enforcement runs before each tool.

    Idempotent: subsequent calls only update which client is used.
    Returns True if a patch is in place.
    """
    global _ORIGINAL_RUN_JSON, _TOOL_PATCHED, _PATCHED_CLIENT

    if getattr(client, "_policy_enforcer", None) is None:
        return False

    try:
        from autogen_core.tools import BaseTool as CoreBaseTool  # type: ignore[import-not-found]
    except ImportError:
        return False

    if _TOOL_PATCHED:
        _PATCHED_CLIENT = client
        return True

    _ORIGINAL_RUN_JSON = CoreBaseTool.run_json
    _PATCHED_CLIENT = client

    from strathon.policy.types import (
        StrathonPolicyBlocked,
        StrathonPolicyThrottled,
    )

    async def _policy_aware_run_json(self, args, cancellation_token, call_id=None):
        current_client = _PATCHED_CLIENT
        if (
            current_client is None
            or getattr(current_client, "_policy_enforcer", None) is None
        ):
            return await _ORIGINAL_RUN_JSON(self, args, cancellation_token, call_id=call_id)

        tool_name = getattr(self, "name", None) or "tool"
        span_attrs: Dict[str, Any] = {
            "strathon.framework": "autogen",
            "gen_ai.tool.name": tool_name,
            "strathon.tool.name": tool_name,
        }
        # Always set strathon.tool.args (default "") so an args-based policy
        # matches consistently across every surface — a missing key makes a
        # CEL index error and the policy silently never matches here.
        span_attrs["strathon.tool.args"] = _truncate(
            _json_or_str(dict(args) if hasattr(args, "items") else args)
        ) if args else ""

        # Halt check OUTSIDE the policy try/except: an operator kill-switch
        # must propagate, not be swallowed by the fail-open policy handler.
        from strathon.policy.steer import check_halt_or_raise
        check_halt_or_raise(current_client, f"autogen.tool.{tool_name}", span_attrs)

        try:
            decision = current_client.check_policy({
                "name": f"autogen.tool.{tool_name}",
                "attrs": span_attrs,
            })
        except Exception:
            logger.exception(
                "Policy check failed for tool %s; allowing", tool_name
            )
            return await _ORIGINAL_RUN_JSON(self, args, cancellation_token, call_id=call_id)

        if decision.is_block:
            from strathon.policy.steer import _emit_intervention_span
            _emit_intervention_span(
                current_client,
                span_name=f"autogen.tool.{tool_name}",
                attrs=span_attrs,
                decision_kind="blocked",
                decision=decision,
            )
            raise StrathonPolicyBlocked(
                decision.message or f"Tool '{tool_name}' blocked by Strathon policy",
                policy_id=decision.policy_id,
                policy_name=decision.policy_name,
            )

        if decision.is_throttle:
            from strathon.policy.steer import _emit_intervention_span
            _emit_intervention_span(
                current_client,
                span_name=f"autogen.tool.{tool_name}",
                attrs=span_attrs,
                decision_kind="throttled",
                decision=decision,
            )
            raise StrathonPolicyThrottled(
                decision.message
                or f"Tool '{tool_name}' rate-limited by Strathon policy",
                policy_id=decision.policy_id,
                policy_name=decision.policy_name,
                retry_after_seconds=decision.retry_after_seconds,
            )

        if decision.is_require_approval:
            # This patch wraps the async run_json and is awaited before the
            # tool body executes, so we can block for a human decision without
            # freezing the event loop (await_for_approval polls off-loop).
            # Approved -> fall through to run the real tool. Denied/expired/
            # timed out -> StrathonApprovalDenied propagates and the tool body
            # never runs. Real approval enforcement.
            from strathon.policy import await_for_approval
            from strathon.policy.steer import _emit_intervention_span
            try:
                await await_for_approval(
                    current_client,
                    decision,
                    {"name": f"autogen.tool.{tool_name}", "attrs": span_attrs},
                )
            except Exception:
                _emit_intervention_span(
                    current_client,
                    span_name=f"autogen.tool.{tool_name}",
                    attrs=span_attrs,
                    decision_kind="approval_denied",
                    decision=decision,
                )
                raise
            _emit_intervention_span(
                current_client,
                span_name=f"autogen.tool.{tool_name}",
                attrs=span_attrs,
                decision_kind="approval_granted",
                decision=decision,
            )
            # Approved: fall through to run the real tool below.

        if decision.is_steer:
            from strathon.policy.steer import _emit_intervention_span
            replacement = decision.replacement or (
                f"[Strathon: tool '{tool_name}' redirected by policy"
                + (f" '{decision.policy_name}'" if decision.policy_name else "")
                + "]"
            )
            _emit_intervention_span(
                current_client,
                span_name=f"autogen.tool.{tool_name}",
                attrs=span_attrs,
                decision_kind="steered",
                decision=decision,
                replacement=replacement,
            )
            return replacement

        # Allow: run the real tool and emit a span.
        tracer = current_client.tracer
        span = tracer.start_span(
            name=f"autogen.tool.{tool_name}",
            attributes=span_attrs,
        )
        start = time.monotonic()
        try:
            result = await _ORIGINAL_RUN_JSON(self, args, cancellation_token, call_id=call_id)
        except Exception as exc:
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.set_attribute("strathon.tool.error", _truncate(str(exc)))
            span.end()
            raise
        elapsed_ms = (time.monotonic() - start) * 1000
        span.set_attribute("strathon.tool.duration_ms", round(elapsed_ms, 2))
        if result is not None:
            span.set_attribute("strathon.tool.output", _truncate(_json_or_str(result)))
        span.set_status(Status(StatusCode.OK))
        span.end()
        return result

    CoreBaseTool.run_json = _policy_aware_run_json  # type: ignore[method-assign]
    _TOOL_PATCHED = True
    logger.info("AutoGen tool policy enforcement patch installed on BaseTool.run_json")
    return True


def _uninstall_tool_patch() -> None:
    """Restore the original BaseTool.run_json. For tests and cleanup."""
    global _ORIGINAL_RUN_JSON, _TOOL_PATCHED, _PATCHED_CLIENT

    if _ORIGINAL_RUN_JSON is None:
        return
    try:
        from autogen_core.tools import BaseTool as CoreBaseTool
        CoreBaseTool.run_json = _ORIGINAL_RUN_JSON  # type: ignore[method-assign]
    except ImportError:
        pass
    _ORIGINAL_RUN_JSON = None
    _TOOL_PATCHED = False
    _PATCHED_CLIENT = None


def instrument(client) -> bool:
    """Instrument AutoGen AgentChat for trace capture.

    Monkey-patches ``BaseChatAgent.on_messages`` and
    ``BaseGroupChat.run`` for agent/team-level spans, and
    ``BaseTool.run_json`` for per-tool policy enforcement.

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
        # Update tool patch client ref on re-instrument.
        _install_tool_patch(client)
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

    # Patch tool-level run_json for policy enforcement.
    _install_tool_patch(client)

    _PATCHED = True
    logger.info("AutoGen instrumentation registered")
    return True
