"""Pydantic AI instrumentation for Strathon.

Provides ``StrathonFirewall``, an ``AbstractCapability`` that enforces
CEL-based policies on tool calls and emits OpenTelemetry spans for every
tool execution and model request.

Unlike other Strathon framework integrations (which monkey-patch or
register event listeners globally), this integration uses Pydantic AI's
first-class capability system. The user explicitly passes the firewall
to their Agent's ``capabilities`` list:

    from strathon import Client
    from strathon.instrumentation.pydantic_ai import StrathonFirewall

    client = Client(api_key="...", endpoint="http://localhost:4318")
    firewall = StrathonFirewall(client)

    agent = Agent("openai:gpt-4o", capabilities=[firewall])

This is the cleanest integration of any framework — no monkey-patching,
no global state, no import-time side effects. The capability hooks are:

- ``before_tool_execute``: evaluates policies, raises SkipToolExecution
  to block or steer tool calls before execution.
- ``wrap_tool_execute``: emits an OTel span around each tool execution
  with timing, tool name, args, and result.
- ``wrap_model_request``: emits an OTel span around each LLM call with
  gen_ai.* attributes (model, tokens, cost).
- ``get_ordering``: returns outermost ordering so the firewall sees
  raw inputs before other capabilities modify them.

Requires pydantic-ai >= 1.80.0 (the release that shipped AbstractCapability
and the Hooks/SkipToolExecution API).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from opentelemetry.trace import Status, StatusCode

logger = logging.getLogger(__name__)

_MAX_ATTR_LEN = 2000
_CLIENT_REF: Optional[Any] = None


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


def _provider_from_model(model_name: Optional[str]) -> Optional[str]:
    """Parse provider prefix from a pydantic-ai model string.

    Examples:
        'openai:gpt-4o'            -> 'openai'
        'anthropic:claude-sonnet-4-6' -> 'anthropic'
        'google:gemini-2.0-pro'    -> 'google'
    """
    if not model_name:
        return None
    if ":" in model_name:
        return model_name.split(":", 1)[0].lower()
    lower = model_name.lower()
    if any(t in lower for t in ("gpt-", "o1", "o3", "o4")):
        return "openai"
    if any(t in lower for t in ("claude", "haiku", "sonnet", "opus")):
        return "anthropic"
    if "gemini" in lower:
        return "google"
    return None


def _tool_span_attrs(
    tool_name: str,
    tool_args: Any,
) -> Dict[str, Any]:
    """Build span attributes for a tool call boundary."""
    attrs: Dict[str, Any] = {
        "strathon.framework": "pydantic_ai",
        "gen_ai.tool.name": tool_name,
        "strathon.tool.name": tool_name,
    }
    if tool_args is not None:
        attrs["strathon.tool.args"] = _truncate(_json_or_str(tool_args))
    return attrs


def _model_request_attrs(model_name: Optional[str], message_count: int) -> Dict[str, Any]:
    """Build span attributes for a model request."""
    attrs: Dict[str, Any] = {
        "strathon.framework": "pydantic_ai",
        "gen_ai.operation.name": "chat",
    }
    if model_name:
        attrs["gen_ai.request.model"] = str(model_name)
        provider = _provider_from_model(model_name)
        if provider:
            attrs["gen_ai.provider.name"] = provider
    if message_count > 0:
        attrs["gen_ai.prompt.message_count"] = message_count
    return attrs


def _model_response_attrs(response: Any) -> Dict[str, Any]:
    """Extract response attributes from a pydantic-ai ModelResponse."""
    attrs: Dict[str, Any] = {}
    # ModelResponse has model_name, usage, parts, timestamp.
    model_name = getattr(response, "model_name", None)
    if model_name:
        attrs["gen_ai.response.model"] = str(model_name)
    # Usage is a Usage dataclass with request_tokens, response_tokens, total_tokens.
    usage = getattr(response, "usage", None)
    if usage:
        req_tokens = getattr(usage, "request_tokens", None) or getattr(usage, "input_tokens", None)
        if req_tokens is not None:
            attrs["gen_ai.usage.input_tokens"] = req_tokens
        resp_tokens = getattr(usage, "response_tokens", None) or getattr(usage, "output_tokens", None)
        if resp_tokens is not None:
            attrs["gen_ai.usage.output_tokens"] = resp_tokens
        total = getattr(usage, "total_tokens", None)
        if total is not None:
            attrs["gen_ai.usage.total_tokens"] = total
    return attrs


# ---------------------------------------------------------------------------
# StrathonFirewall capability
# ---------------------------------------------------------------------------
# Built as a factory-function-created dataclass so the module is importable
# even when pydantic-ai is not installed. The AbstractCapability subclass
# is created lazily inside create_firewall() / instrument().


def _build_firewall_class():
    """Lazily construct the StrathonFirewall class.

    Returns None if pydantic-ai is not installed or too old.
    """
    try:
        from pydantic_ai.capabilities import AbstractCapability, CapabilityOrdering
        from pydantic_ai.exceptions import SkipToolExecution
    except ImportError:
        return None

    # Check that the hooks API exists (requires >= 1.80.0).
    if not hasattr(AbstractCapability, "before_tool_execute"):
        logger.warning(
            "pydantic-ai is installed but too old for capabilities API "
            "(need >= 1.80.0); skipping instrumentation"
        )
        return None

    # strathon.policy.steer and strathon.policy.types are imported locally
    # inside class methods where they are used (before_tool_execute).

    @dataclass
    class StrathonFirewall(AbstractCapability):
        """Strathon agent firewall for Pydantic AI.

        Evaluates CEL-based policies before every tool call and emits
        OpenTelemetry spans for observability. Pass to your Agent's
        ``capabilities`` list.

        Parameters
        ----------
        client : strathon.Client
            Strathon client with tracer and optional policy enforcer.
        block_message : str
            Default message returned as tool result when a policy blocks.
        """

        client: Any = None
        block_message: str = "[Strathon: tool call blocked by policy]"
        # Internal: track in-flight model request spans by id.
        _active_model_spans: Dict[int, Any] = field(
            default_factory=dict, init=False, repr=False,
        )

        def get_ordering(self) -> CapabilityOrdering:
            """Outermost: firewall sees raw input before other capabilities."""
            return CapabilityOrdering.OUTERMOST

        # ---- Tool hooks: policy enforcement + observability ----

        def before_tool_execute(self, ctx, *, call, tool_def, args):
            """Evaluate policies before tool execution.

            If a policy matches with action 'block' or 'steer', raises
            SkipToolExecution with the appropriate result. The tool body
            never runs.

            Parameters
            ----------
            ctx : RunContext
                Pydantic AI run context.
            call : ToolCallPart
                The raw tool call from the model.
            tool_def : ToolDefinition
                Tool metadata (name, description, schema).
            args : dict
                Validated tool arguments.
            """
            if self.client is None:
                return args

            enforcer = getattr(self.client, "_policy_enforcer", None)
            if enforcer is None:
                return args

            tool_name = tool_def.name if tool_def else getattr(call, "tool_name", "unknown")
            span_attrs = _tool_span_attrs(tool_name, args)

            try:
                span_context = {"name": f"pydantic_ai.tool.{tool_name}", "attrs": span_attrs}
                decision = self.client.check_policy(span_context)
            except Exception:
                logger.exception(
                    "Policy check failed for tool %s; allowing execution", tool_name
                )
                return args

            if decision.is_block:
                # Emit intervention span for audit trail.
                from strathon.policy.steer import _emit_intervention_span
                _emit_intervention_span(
                    self.client,
                    span_name=f"pydantic_ai.tool.{tool_name}",
                    attrs=span_attrs,
                    decision_kind="blocked",
                    decision=decision,
                )
                message = decision.message or self.block_message
                raise SkipToolExecution(message)

            if decision.is_throttle:
                from strathon.policy.steer import _emit_intervention_span
                _emit_intervention_span(
                    self.client,
                    span_name=f"pydantic_ai.tool.{tool_name}",
                    attrs=span_attrs,
                    decision_kind="throttled",
                    decision=decision,
                )
                message = (
                    decision.message
                    or f"[Strathon: tool '{tool_name}' rate-limited by policy]"
                )
                raise SkipToolExecution(message)

            if decision.is_require_approval:
                # before_tool_execute is the SYNC enforcement hook and can only
                # raise — it cannot await a human decision. Re-deriving approval
                # in the async wrap_tool_execute would mean evaluating the policy
                # twice (risking divergent decisions and duplicate spans), so a
                # matched require_approval falls closed here: deny with a loud,
                # observable block rather than silently allowing. Interactive
                # approval on pydantic-ai needs a Tier-1 surface (enforce_steer /
                # the @enforcer decorator), which can await.
                from strathon.policy.steer import _emit_intervention_span
                _emit_intervention_span(
                    self.client,
                    span_name=f"pydantic_ai.tool.{tool_name}",
                    attrs=span_attrs,
                    decision_kind="approval_denied",
                    decision=decision,
                )
                message = (
                    decision.message
                    or f"Tool '{tool_name}' requires approval, which cannot be "
                    "served on the pydantic-ai auto-instrument path; blocked. "
                    "Use enforce_steer or the @enforcer decorator for "
                    "interactive approval."
                )
                raise SkipToolExecution(message)

            if decision.is_steer:
                from strathon.policy.steer import _emit_intervention_span
                replacement = decision.replacement or (
                    f"[Strathon: tool '{tool_name}' redirected by policy"
                    + (f" '{decision.policy_name}'" if decision.policy_name else "")
                    + "]"
                )
                _emit_intervention_span(
                    self.client,
                    span_name=f"pydantic_ai.tool.{tool_name}",
                    attrs=span_attrs,
                    decision_kind="steered",
                    decision=decision,
                    replacement=replacement,
                )
                raise SkipToolExecution(replacement)

            # Allow — pass args through unchanged.
            return args

        async def wrap_tool_execute(self, ctx, *, call, tool_def, args, handler):
            """Wrap tool execution with an OTel span.

            Parameters
            ----------
            handler : callable
                The next handler in the capability chain (eventually
                runs the actual tool function).
            """
            if self.client is None:
                return await handler(args)

            tool_name = tool_def.name if tool_def else getattr(call, "tool_name", "unknown")
            span_attrs = _tool_span_attrs(tool_name, args)

            tracer = self.client.tracer
            span = tracer.start_span(
                name=f"pydantic_ai.tool.{tool_name}",
                attributes=span_attrs,
            )
            start = time.monotonic()
            try:
                result = await handler(args)
            except Exception as exc:
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                span.set_attribute("strathon.tool.error", _truncate(str(exc)))
                span.end()
                raise
            elapsed_ms = (time.monotonic() - start) * 1000
            span.set_attribute("strathon.tool.duration_ms", round(elapsed_ms, 2))
            if result is not None:
                span.set_attribute("strathon.tool.result", _truncate(_json_or_str(result)))
            span.set_status(Status(StatusCode.OK))
            span.end()
            return result

        # ---- Model request hooks: LLM call observability ----

        def before_model_request(self, ctx, request_context):
            """Emit model request span attributes before the LLM call."""
            if self.client is None:
                return request_context

            model = request_context.model
            model_name = None
            if model is not None:
                model_name = getattr(model, "model_name", None) or str(model)

            message_count = len(request_context.messages) if request_context.messages else 0
            span_attrs = _model_request_attrs(model_name, message_count)

            tracer = self.client.tracer
            span = tracer.start_span(
                name=f"pydantic_ai.model.{model_name or 'unknown'}",
                attributes=span_attrs,
            )
            # Stash the span so after_model_request can finalize it.
            self._active_model_spans[id(request_context)] = span

            return request_context

        def after_model_request(self, ctx, response):
            """Finalize the model request span with response data."""
            # Find the stashed span. We iterate because we don't have
            # the request_context reference here — we use the most recently
            # opened span (LIFO).
            if not self._active_model_spans:
                return response

            # Pop the most recent span.
            span_key = next(reversed(self._active_model_spans))
            span = self._active_model_spans.pop(span_key, None)
            if span is None:
                return response

            try:
                resp_attrs = _model_response_attrs(response)
                for k, v in resp_attrs.items():
                    span.set_attribute(k, v)
                span.set_status(Status(StatusCode.OK))
            except Exception:
                logger.debug("Failed to extract model response attributes", exc_info=True)
                span.set_status(Status(StatusCode.OK))
            finally:
                span.end()

            return response

        def on_model_request_error(self, ctx, error):
            """Mark the model request span as errored."""
            if not self._active_model_spans:
                raise error

            span_key = next(reversed(self._active_model_spans))
            span = self._active_model_spans.pop(span_key, None)
            if span is not None:
                try:
                    span.set_status(Status(StatusCode.ERROR, str(error)))
                finally:
                    span.end()
            raise error

    return StrathonFirewall


# Cache the class so we build it once per process.
_StrathonFirewall = None


def _get_firewall_class():
    global _StrathonFirewall
    if _StrathonFirewall is None:
        _StrathonFirewall = _build_firewall_class()
    return _StrathonFirewall


def create_firewall(client) -> Any:
    """Create a StrathonFirewall capability for a Pydantic AI Agent.

    Returns an ``AbstractCapability`` instance to pass in the agent's
    ``capabilities=[]`` list.

    Args:
        client: Strathon Client instance.

    Returns:
        StrathonFirewall capability.

    Raises:
        ImportError: If pydantic-ai is not installed or too old.

    Example::

        from strathon import Client
        from strathon.instrumentation.pydantic_ai import create_firewall

        client = Client(api_key="...", endpoint="http://localhost:4318")
        firewall = create_firewall(client)

        agent = Agent("openai:gpt-4o", capabilities=[firewall])
    """
    cls = _get_firewall_class()
    if cls is None:
        raise ImportError(
            "pydantic-ai >= 1.80.0 is required for StrathonFirewall. "
            "Install with: pip install 'pydantic-ai>=1.80.0'"
        )
    return cls(client=client)


def instrument(client) -> bool:
    """Register Pydantic AI instrumentation.

    Unlike other framework integrations, Pydantic AI uses a capability-based
    system. This function validates that pydantic-ai is installed with the
    required version, stores the client reference, and returns True.

    The user must still create and pass the firewall capability to their
    agent. Use ``create_firewall(client)`` or instantiate
    ``StrathonFirewall(client=client)`` directly.

    Args:
        client: Strathon Client instance.

    Returns:
        True if pydantic-ai is installed with capabilities support.
        False if pydantic-ai is not installed or too old.
    """
    global _CLIENT_REF

    cls = _get_firewall_class()
    if cls is None:
        logger.debug(
            "pydantic-ai not installed or too old for capabilities; "
            "skipping instrumentation"
        )
        return False

    _CLIENT_REF = client
    logger.info(
        "Pydantic AI instrumentation registered. "
        "Use create_firewall(client) or StrathonFirewall(client=client) "
        "and pass to Agent(capabilities=[...])."
    )
    return True


# Convenience re-export: users can do
#   from strathon.instrumentation.pydantic_ai import StrathonFirewall
# when pydantic-ai is installed. When it's not, this is None.
StrathonFirewall = _get_firewall_class()

__all__ = [
    "StrathonFirewall",
    "create_firewall",
    "instrument",
]
