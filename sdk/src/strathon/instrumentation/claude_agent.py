"""Claude Agent SDK instrumentation for Strathon.

Two-layer instrumentation:

1. **Session-level (monkey-patch)**: wraps ``query()`` and
   ``ClaudeSDKClient.query()`` to emit OpenTelemetry spans for every
   agent session (prompt, response, tool usage, session metadata).

2. **Tool-level (hooks)**: ``create_strathon_hooks(client)`` returns a
   hooks dict for ``ClaudeAgentOptions``. PreToolUse evaluates CEL
   policies before each tool call (deny to block, allow to proceed).
   PostToolUse emits per-tool OTel spans.

Usage for tool-level enforcement (hooks on ClaudeSDKClient)::

    from strathon import Client
    from strathon.instrumentation.claude_agent import create_strathon_hooks

    client = Client(api_key="...", endpoint="http://localhost:4318")
    hooks = create_strathon_hooks(client)

    async with ClaudeSDKClient(
        options=ClaudeAgentOptions(hooks=hooks)
    ) as sdk_client:
        await sdk_client.query("...")

Note: hooks require ``ClaudeSDKClient``. The module-level ``query()``
function does not support hooks. The session-level monkey-patch on
``query()`` still provides observability for users not using
``ClaudeSDKClient``.

The Claude Agent SDK (``pip install claude-agent-sdk``, formerly
``claude-code-sdk``) wraps the Claude Code CLI. As of v0.1.81+, the
SDK exposes PreToolUse/PostToolUse hooks on ClaudeAgentOptions for
first-class tool-call interception.

For raw Anthropic API instrumentation (``anthropic.messages.create``),
use ``strathon.instrumentation.anthropic`` instead.
"""

from __future__ import annotations

import functools
import json
import logging
import time
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


# ---------------------------------------------------------------------------
# Layer 1: Session-level monkey-patches (query + ClaudeSDKClient.query)
# ---------------------------------------------------------------------------


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

        response_text = _extract_messages(result)
        if response_text:
            span.set_attribute("gen_ai.completion", response_text)

        session_id = getattr(result, "session_id", None)
        if session_id:
            span.set_attribute("gen_ai.conversation.id", str(session_id))

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


# ---------------------------------------------------------------------------
# Layer 2: Tool-level hooks (PreToolUse / PostToolUse)
# ---------------------------------------------------------------------------

# Module-level state for tool timing (keyed by tool_use_id).
_TOOL_START_TIMES: Dict[str, float] = {}


def _build_pre_tool_use_hook(client):
    """Build a PreToolUse hook that evaluates Strathon policies."""

    async def strathon_pre_tool_use(input_data, tool_use_id, context):
        tool_name = input_data.get("tool_name", "unknown")
        tool_input = input_data.get("tool_input", {})

        if tool_use_id:
            _TOOL_START_TIMES[tool_use_id] = time.monotonic()

        enforcer = getattr(client, "_policy_enforcer", None)
        if enforcer is None:
            return {}

        span_attrs: Dict[str, Any] = {
            "strathon.framework": "claude_agent_sdk",
            "gen_ai.tool.name": tool_name,
            "strathon.tool.name": tool_name,
        }
        # Always set strathon.tool.args (default "") for consistent matching.
        span_attrs["strathon.tool.args"] = _truncate(
            _json_or_str(tool_input)
        ) if tool_input else ""

        # Halt check before the policy try/except so an operator kill-switch
        # propagates rather than being swallowed by the fail-open handler.
        from strathon.policy.steer import check_halt_or_raise
        check_halt_or_raise(client, f"claude_agent.tool.{tool_name}", span_attrs)

        try:
            decision = client.check_policy({
                "name": f"claude_agent.tool.{tool_name}",
                "attrs": span_attrs,
            })
        except Exception:
            logger.exception(
                "Policy check failed for tool %s; allowing", tool_name
            )
            return {}

        if decision.is_block or decision.is_throttle:
            from strathon.policy.steer import _emit_intervention_span
            kind = "blocked" if decision.is_block else "throttled"
            _emit_intervention_span(
                client,
                span_name=f"claude_agent.tool.{tool_name}",
                attrs=span_attrs,
                decision_kind=kind,
                decision=decision,
            )
            reason = (
                decision.message
                or f"Tool '{tool_name}' {kind} by Strathon policy"
            )
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }

        if decision.is_require_approval:
            # PreToolUse is async and runs before the tool executes; its
            # permissionDecision controls whether the tool runs. So we await a
            # human decision off-loop: approved -> return {} (proceed); denied/
            # expired/timed out -> return a deny decision so the tool never
            # runs. Real approval enforcement (never a silent allow).
            from strathon.policy import await_for_approval
            from strathon.policy.steer import _emit_intervention_span
            try:
                await await_for_approval(
                    client,
                    decision,
                    {"name": f"claude_agent.tool.{tool_name}", "attrs": span_attrs},
                )
            except Exception as approval_exc:
                _emit_intervention_span(
                    client,
                    span_name=f"claude_agent.tool.{tool_name}",
                    attrs=span_attrs,
                    decision_kind="approval_denied",
                    decision=decision,
                )
                reason = (
                    decision.message
                    or str(approval_exc)
                    or f"Tool '{tool_name}' denied by Strathon approval policy"
                )
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": reason,
                    }
                }
            _emit_intervention_span(
                client,
                span_name=f"claude_agent.tool.{tool_name}",
                attrs=span_attrs,
                decision_kind="approval_granted",
                decision=decision,
            )
            # Approved: fall through to proceed (return {} below).

        if decision.is_steer:
            from strathon.policy.steer import _emit_intervention_span
            replacement = decision.replacement or (
                f"[Strathon: tool '{tool_name}' redirected by policy"
                + (f" '{decision.policy_name}'" if decision.policy_name else "")
                + "]"
            )
            _emit_intervention_span(
                client,
                span_name=f"claude_agent.tool.{tool_name}",
                attrs=span_attrs,
                decision_kind="steered",
                decision=decision,
                replacement=replacement,
            )
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": replacement,
                }
            }

        return {}

    return strathon_pre_tool_use


def _build_post_tool_use_hook(client):
    """Build a PostToolUse hook that emits OTel tool spans."""

    async def strathon_post_tool_use(input_data, tool_use_id, context):
        tool_name = input_data.get("tool_name", "unknown")
        tool_input = input_data.get("tool_input", {})

        span_attrs: Dict[str, Any] = {
            "strathon.framework": "claude_agent_sdk",
            "gen_ai.tool.name": tool_name,
            "strathon.tool.name": tool_name,
        }
        # Always set strathon.tool.args (default "") for consistent matching.
        span_attrs["strathon.tool.args"] = _truncate(
            _json_or_str(tool_input)
        ) if tool_input else ""

        start = _TOOL_START_TIMES.pop(tool_use_id or "", None)
        if start is not None:
            elapsed_ms = (time.monotonic() - start) * 1000
            span_attrs["strathon.tool.duration_ms"] = round(elapsed_ms, 2)

        result = input_data.get("result")
        if result is not None:
            span_attrs["strathon.tool.result"] = _truncate(
                _json_or_str(result)
            )

        tracer = client.tracer
        span = tracer.start_span(
            name=f"claude_agent.tool.{tool_name}",
            attributes=span_attrs,
        )
        span.set_status(Status(StatusCode.OK))
        span.end()

        return {}

    return strathon_post_tool_use


def create_strathon_hooks(client) -> Dict[str, Any]:
    """Create PreToolUse/PostToolUse hooks for ClaudeAgentOptions.

    Returns a hooks dict to pass to ``ClaudeAgentOptions(hooks=...)``.
    PreToolUse evaluates Strathon policies and denies tool calls that
    match block/steer/throttle rules. PostToolUse emits OTel spans.

    Note: hooks only work with ``ClaudeSDKClient``, not ``query()``.

    Args:
        client: Strathon Client instance.

    Returns:
        Dict suitable for ``ClaudeAgentOptions(hooks=...)``.

    Example::

        from strathon.instrumentation.claude_agent import create_strathon_hooks
        hooks = create_strathon_hooks(client)
        options = ClaudeAgentOptions(hooks=hooks)
    """
    try:
        from claude_agent_sdk import HookMatcher  # type: ignore[import-not-found]
    except ImportError:
        return {
            "PreToolUse": [
                {"hooks": [_build_pre_tool_use_hook(client)]}
            ],
            "PostToolUse": [
                {"hooks": [_build_post_tool_use_hook(client)]}
            ],
        }

    return {
        "PreToolUse": [
            HookMatcher(hooks=[_build_pre_tool_use_hook(client)])
        ],
        "PostToolUse": [
            HookMatcher(hooks=[_build_post_tool_use_hook(client)])
        ],
    }


# ---------------------------------------------------------------------------
# instrument() — registers session-level patches
# ---------------------------------------------------------------------------


def instrument(client) -> bool:
    """Instrument the Claude Agent SDK for trace capture.

    Wraps ``claude_agent_sdk.query()`` and ``ClaudeSDKClient.query()``
    to emit session-level OpenTelemetry spans.

    For tool-level policy enforcement, also call
    ``create_strathon_hooks(client)`` and pass the result to
    ``ClaudeAgentOptions(hooks=...)``.

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

    if hasattr(claude_agent_sdk, "query"):
        claude_agent_sdk.query = _wrap_query(
            claude_agent_sdk.query, tracer
        )

    try:
        from claude_agent_sdk import ClaudeSDKClient
        if hasattr(ClaudeSDKClient, "query"):
            ClaudeSDKClient.query = _wrap_client_query(
                ClaudeSDKClient.query, tracer
            )
    except (ImportError, AttributeError):
        logger.debug("ClaudeSDKClient not available; skipping client patch")

    _PATCHED = True
    logger.info(
        "Claude Agent SDK instrumentation registered. "
        "For tool-level policy enforcement, pass "
        "create_strathon_hooks(client) to ClaudeAgentOptions."
    )
    return True


__all__ = [
    "create_strathon_hooks",
    "instrument",
]
