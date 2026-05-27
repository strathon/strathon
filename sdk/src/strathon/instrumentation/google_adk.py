"""Google Agent Development Kit (ADK) instrumentation for Strathon.

Provides ``StrathonFirewallPlugin``, a ``BasePlugin`` that enforces
CEL-based policies on tool calls and emits OpenTelemetry spans for every
tool execution and model request in a Google ADK agent.

The user registers the plugin on their ADK Runner:

    from strathon import Client
    from strathon.instrumentation.google_adk import create_firewall_plugin

    client = Client(api_key="...", endpoint="http://localhost:4318")
    plugin = create_firewall_plugin(client)

    runner = Runner(
        agent=my_agent,
        app_name="my-app",
        session_service=session_service,
        plugins=[plugin],
    )

Like the Pydantic AI integration, this uses the framework's first-class
plugin system — no monkey-patching. The plugin hooks are:

- ``before_tool_callback``: evaluates policies, returns a dict to
  short-circuit the tool call on block/steer/throttle.
- ``after_tool_callback``: emits an OTel span with tool name, args,
  result, and timing.
- ``on_tool_error_callback``: records tool errors in OTel spans.
- ``before_model_callback``: starts an OTel span for the LLM request.
- ``after_model_callback``: finalizes the LLM span with response data.

Known caveats (as of google-adk 1.33.0):
- VertexAiRagRetrieval tools may bypass plugin callbacks (issue #2629).
- Plugins don't propagate to sub-agents invoked via AgentTool (issue
  #2809). The sub-agent's Runner needs its own plugin registration.
- InMemoryRunner may not fire non-tool plugin callbacks in some
  configurations (issue #4464). Tool callbacks are unaffected.

Requires google-adk >= 1.7.0 (the release that shipped the plugin system).
"""

from __future__ import annotations

import json
import logging
import time
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


def _tool_span_attrs(
    tool_name: str,
    tool_args: Any,
    agent_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Build span attributes for a tool call boundary."""
    attrs: Dict[str, Any] = {
        "strathon.framework": "google_adk",
        "gen_ai.tool.name": tool_name,
        "strathon.tool.name": tool_name,
    }
    if tool_args is not None:
        attrs["strathon.tool.args"] = _truncate(_json_or_str(tool_args))
    if agent_name:
        attrs["strathon.agent.name"] = agent_name
    return attrs


def _model_request_attrs(llm_request: Any) -> Dict[str, Any]:
    """Build span attributes from an ADK LlmRequest."""
    attrs: Dict[str, Any] = {
        "strathon.framework": "google_adk",
        "gen_ai.operation.name": "chat",
    }
    # LlmRequest has model, contents, config.
    model = getattr(llm_request, "model", None)
    if model:
        attrs["gen_ai.request.model"] = _safe_str(model)
        attrs["gen_ai.provider.name"] = "google"
    contents = getattr(llm_request, "contents", None)
    if contents:
        attrs["gen_ai.prompt.message_count"] = len(contents)
    config = getattr(llm_request, "config", None)
    if config:
        temperature = getattr(config, "temperature", None)
        if temperature is not None:
            attrs["gen_ai.request.temperature"] = float(temperature)
        max_tokens = getattr(config, "max_output_tokens", None)
        if max_tokens is not None:
            attrs["gen_ai.request.max_tokens"] = int(max_tokens)
    return attrs


def _model_response_attrs(llm_response: Any) -> Dict[str, Any]:
    """Extract response attributes from an ADK LlmResponse."""
    attrs: Dict[str, Any] = {}
    # LlmResponse has content, usage_metadata.
    content = getattr(llm_response, "content", None)
    if content:
        parts = getattr(content, "parts", None)
        if parts:
            text_parts = []
            for part in parts:
                text = getattr(part, "text", None)
                if text:
                    text_parts.append(text)
            if text_parts:
                attrs["gen_ai.completion"] = _truncate("\n".join(text_parts))
    usage = getattr(llm_response, "usage_metadata", None)
    if usage:
        prompt_tokens = getattr(usage, "prompt_token_count", None)
        if prompt_tokens is not None:
            attrs["gen_ai.usage.input_tokens"] = prompt_tokens
        candidates_tokens = getattr(usage, "candidates_token_count", None)
        if candidates_tokens is not None:
            attrs["gen_ai.usage.output_tokens"] = candidates_tokens
        total_tokens = getattr(usage, "total_token_count", None)
        if total_tokens is not None:
            attrs["gen_ai.usage.total_tokens"] = total_tokens
    return attrs


# ---------------------------------------------------------------------------
# StrathonFirewallPlugin
# ---------------------------------------------------------------------------

def _build_plugin_class():
    """Lazily construct the StrathonFirewallPlugin class.

    Returns None if google-adk is not installed.
    """
    try:
        from google.adk.plugins.base_plugin import BasePlugin  # type: ignore[import-not-found]
    except ImportError:
        return None

    class StrathonFirewallPlugin(BasePlugin):
        """Strathon agent firewall plugin for Google ADK.

        Evaluates CEL-based policies before every tool call and emits
        OpenTelemetry spans for observability. Register on your ADK
        Runner's ``plugins`` list.

        Parameters
        ----------
        client : strathon.Client
            Strathon client with tracer and optional policy enforcer.
        block_message : str
            Default message returned as tool result when a policy blocks.
        name : str
            Plugin name for ADK's plugin registry.
        """

        def __init__(
            self,
            client: Any,
            block_message: str = "[Strathon: tool call blocked by policy]",
            name: str = "strathon_firewall",
        ):
            super().__init__(name=name)
            self.client = client
            self.block_message = block_message
            # Track in-flight model spans by id(callback_context).
            self._active_model_spans: Dict[int, Any] = {}
            # Track tool start times by (tool_name, id(tool_context)).
            self._tool_start_times: Dict[tuple, float] = {}

        # ---- Tool hooks: policy enforcement + observability ----

        async def before_tool_callback(
            self,
            *,
            tool,
            tool_args: dict,
            tool_context,
        ) -> Optional[dict]:
            """Evaluate policies before tool execution.

            Returns a dict to short-circuit the tool call (block/steer/
            throttle), or None to allow execution.
            """
            tool_name = getattr(tool, "name", None) or "unknown"
            agent_name = getattr(tool_context, "agent_name", None)

            # Record start time for after_tool_callback span.
            key = (tool_name, id(tool_context))
            self._tool_start_times[key] = time.monotonic()

            if self.client is None:
                return None

            enforcer = getattr(self.client, "_policy_enforcer", None)
            if enforcer is None:
                return None

            span_attrs = _tool_span_attrs(tool_name, tool_args, agent_name)

            try:
                span_context = {
                    "name": f"google_adk.tool.{tool_name}",
                    "attrs": span_attrs,
                }
                decision = self.client.check_policy(span_context)
            except Exception:
                logger.exception(
                    "Policy check failed for tool %s; allowing execution",
                    tool_name,
                )
                return None

            if decision.is_block:
                from strathon.policy.steer import _emit_intervention_span
                _emit_intervention_span(
                    self.client,
                    span_name=f"google_adk.tool.{tool_name}",
                    attrs=span_attrs,
                    decision_kind="blocked",
                    decision=decision,
                )
                message = decision.message or self.block_message
                return {"error": message, "blocked_by": "strathon_policy"}

            if decision.is_throttle:
                from strathon.policy.steer import _emit_intervention_span
                _emit_intervention_span(
                    self.client,
                    span_name=f"google_adk.tool.{tool_name}",
                    attrs=span_attrs,
                    decision_kind="throttled",
                    decision=decision,
                )
                message = (
                    decision.message
                    or f"[Strathon: tool '{tool_name}' rate-limited by policy]"
                )
                return {"error": message, "throttled_by": "strathon_policy"}

            if decision.is_steer:
                from strathon.policy.steer import _emit_intervention_span
                replacement = decision.replacement or (
                    f"[Strathon: tool '{tool_name}' redirected by policy"
                    + (f" '{decision.policy_name}'" if decision.policy_name else "")
                    + "]"
                )
                _emit_intervention_span(
                    self.client,
                    span_name=f"google_adk.tool.{tool_name}",
                    attrs=span_attrs,
                    decision_kind="steered",
                    decision=decision,
                    replacement=replacement,
                )
                return {"result": replacement, "steered_by": "strathon_policy"}

            # Allow.
            return None

        async def after_tool_callback(
            self,
            *,
            tool,
            tool_args: dict,
            tool_context,
            result: dict,
        ) -> Optional[dict]:
            """Emit an OTel span after tool execution completes."""
            if self.client is None:
                return None

            tool_name = getattr(tool, "name", None) or "unknown"
            agent_name = getattr(tool_context, "agent_name", None)
            span_attrs = _tool_span_attrs(tool_name, tool_args, agent_name)

            # Calculate duration from before_tool_callback.
            key = (tool_name, id(tool_context))
            start = self._tool_start_times.pop(key, None)
            if start is not None:
                elapsed_ms = (time.monotonic() - start) * 1000
                span_attrs["strathon.tool.duration_ms"] = round(elapsed_ms, 2)

            if result is not None:
                span_attrs["strathon.tool.result"] = _truncate(
                    _json_or_str(result)
                )

            tracer = self.client.tracer
            span = tracer.start_span(
                name=f"google_adk.tool.{tool_name}",
                attributes=span_attrs,
            )
            span.set_status(Status(StatusCode.OK))
            span.end()

            # Return None to pass the original result through unchanged.
            return None

        async def on_tool_error_callback(
            self,
            *,
            tool,
            tool_args: dict,
            tool_context,
            error: Exception,
        ) -> Optional[dict]:
            """Record tool errors in OTel spans."""
            if self.client is None:
                return None

            tool_name = getattr(tool, "name", None) or "unknown"
            agent_name = getattr(tool_context, "agent_name", None)
            span_attrs = _tool_span_attrs(tool_name, tool_args, agent_name)
            span_attrs["strathon.tool.error"] = _truncate(str(error))

            key = (tool_name, id(tool_context))
            start = self._tool_start_times.pop(key, None)
            if start is not None:
                elapsed_ms = (time.monotonic() - start) * 1000
                span_attrs["strathon.tool.duration_ms"] = round(elapsed_ms, 2)

            tracer = self.client.tracer
            span = tracer.start_span(
                name=f"google_adk.tool.{tool_name}",
                attributes=span_attrs,
            )
            span.set_status(Status(StatusCode.ERROR, str(error)))
            span.end()

            # Return None to let the error propagate normally.
            return None

        # ---- Model hooks: LLM call observability ----

        async def before_model_callback(
            self,
            *,
            callback_context,
            llm_request,
        ):
            """Start an OTel span for the LLM request."""
            if self.client is None:
                return None

            span_attrs = _model_request_attrs(llm_request)
            model_name = span_attrs.get("gen_ai.request.model", "unknown")

            tracer = self.client.tracer
            span = tracer.start_span(
                name=f"google_adk.model.{model_name}",
                attributes=span_attrs,
            )
            self._active_model_spans[id(callback_context)] = span

            # Return None to allow the model call to proceed.
            return None

        async def after_model_callback(
            self,
            *,
            callback_context,
            llm_response,
        ):
            """Finalize the LLM span with response data."""
            span = self._active_model_spans.pop(id(callback_context), None)
            if span is None:
                return None

            try:
                resp_attrs = _model_response_attrs(llm_response)
                for k, v in resp_attrs.items():
                    span.set_attribute(k, v)
                span.set_status(Status(StatusCode.OK))
            except Exception:
                logger.debug(
                    "Failed to extract model response attributes",
                    exc_info=True,
                )
                span.set_status(Status(StatusCode.OK))
            finally:
                span.end()

            # Return None to pass the response through unchanged.
            return None

        async def on_model_error_callback(
            self,
            *,
            callback_context,
            error: Exception,
        ):
            """Mark the model span as errored."""
            span = self._active_model_spans.pop(id(callback_context), None)
            if span is not None:
                try:
                    span.set_status(Status(StatusCode.ERROR, str(error)))
                finally:
                    span.end()
            # Return None to let the error propagate.
            return None

    return StrathonFirewallPlugin


# Cache the class.
_StrathonFirewallPlugin = None


def _get_plugin_class():
    global _StrathonFirewallPlugin
    if _StrathonFirewallPlugin is None:
        _StrathonFirewallPlugin = _build_plugin_class()
    return _StrathonFirewallPlugin


def create_firewall_plugin(client) -> Any:
    """Create a StrathonFirewallPlugin for a Google ADK Runner.

    Returns a ``BasePlugin`` instance to pass in the Runner's
    ``plugins=[]`` list.

    Args:
        client: Strathon Client instance.

    Returns:
        StrathonFirewallPlugin instance.

    Raises:
        ImportError: If google-adk is not installed.

    Example::

        from strathon import Client
        from strathon.instrumentation.google_adk import create_firewall_plugin

        client = Client(api_key="...", endpoint="http://localhost:4318")
        plugin = create_firewall_plugin(client)

        runner = Runner(agent=agent, plugins=[plugin], ...)
    """
    cls = _get_plugin_class()
    if cls is None:
        raise ImportError(
            "google-adk >= 1.7.0 is required for StrathonFirewallPlugin. "
            "Install with: pip install 'google-adk>=1.7.0'"
        )
    return cls(client=client)


def instrument(client) -> bool:
    """Register Google ADK instrumentation.

    Unlike monkey-patching integrations, Google ADK uses a plugin system.
    This function validates that google-adk is installed and stores the
    client reference.

    The user must still create and register the plugin on their Runner.
    Use ``create_firewall_plugin(client)`` to get the plugin instance.

    Args:
        client: Strathon Client instance.

    Returns:
        True if google-adk is installed with plugin support.
        False if google-adk is not installed.
    """
    global _CLIENT_REF

    cls = _get_plugin_class()
    if cls is None:
        logger.debug(
            "google-adk not installed; skipping instrumentation"
        )
        return False

    _CLIENT_REF = client
    logger.info(
        "Google ADK instrumentation registered. "
        "Use create_firewall_plugin(client) and pass to "
        "Runner(plugins=[...])."
    )
    return True


# Convenience re-export.
StrathonFirewallPlugin = _get_plugin_class()

__all__ = [
    "StrathonFirewallPlugin",
    "create_firewall_plugin",
    "instrument",
]
