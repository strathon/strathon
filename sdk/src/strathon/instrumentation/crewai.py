"""CrewAI auto-instrumentation for Strathon.

Subscribes a single BaseEventListener subclass to the CrewAI event bus and
translates the framework's emitted events into OpenTelemetry spans on the
Strathon Client's tracer.

Why event listeners (and not monkey-patching):
- crewai_event_bus is the documented public extension API; stable across versions
- Captures fine-grained spans we cannot see by wrapping methods alone:
  individual LLM calls with token usage, tool retries, agent reasoning steps
- Matches the architectural pattern of the OpenAI Agents SDK integration
  (TracingProcessor), so the codebase stays consistent

Captured events for v0.5:
- CrewKickoffStarted/Completed/Failed     -> crewai.crew      (root span)
- TaskStarted/Completed/Failed            -> crewai.task
- AgentExecutionStarted/Completed/Error   -> crewai.agent
- LLMCallStarted/Completed/Failed         -> crewai.llm       (with gen_ai.usage.*)
- ToolUsageStarted/Finished/Error         -> crewai.tool

Parent-child relationships are reconstructed via the event_id /
parent_event_id / started_event_id fields that every CrewAI event carries.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from opentelemetry import trace as otel_trace
from opentelemetry.trace import Span as OTelSpan, Status, StatusCode

logger = logging.getLogger(__name__)

_MAX_ATTR_LEN = 2000


def _truncate(value: Any, max_len: int = _MAX_ATTR_LEN) -> str:
    """Truncate large attribute values to keep span payload bounded."""
    s = str(value) if value is not None else ""
    if len(s) <= max_len:
        return s
    return s[:max_len] + f"... [truncated {len(s) - max_len} chars]"


def _safe_str(value: Any) -> str:
    """Best-effort str() that never raises."""
    try:
        return str(value) if value is not None else ""
    except Exception:
        return "<unrepr>"


def _json_or_str(value: Any) -> str:
    """Serialize dict/list as JSON, fall back to str() for other types."""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, default=str)
        except Exception:
            return _safe_str(value)
    return _safe_str(value)


def _provider_from_model(model: Optional[str]) -> Optional[str]:
    """Parse the provider prefix from a LiteLLM-style model string.

    Examples:
        'anthropic/claude-opus-4-7' -> 'anthropic'
        'openai/gpt-4o'             -> 'openai'
        'gpt-4o'                    -> 'openai' (heuristic)
        'claude-3-5-sonnet'         -> 'anthropic' (heuristic)
    """
    if not model:
        return None
    if "/" in model:
        return model.split("/", 1)[0].lower()
    lower = model.lower()
    if lower.startswith("gpt"):
        return "openai"
    if lower.startswith("claude"):
        return "anthropic"
    if lower.startswith("gemini"):
        return "google"
    if lower.startswith("mistral") or lower.startswith("mixtral"):
        return "mistral"
    return None


def _common_context_attrs(event) -> Dict[str, Any]:
    """Pull task_id/task_name/agent_id/agent_role from any event for cross-span context."""
    attrs: Dict[str, Any] = {"strathon.framework": "crewai"}
    for src_attr, otel_attr in [
        ("task_id", "strathon.task.id"),
        ("task_name", "strathon.task.name"),
        ("agent_id", "gen_ai.agent.id"),
        ("agent_role", "gen_ai.agent.name"),
    ]:
        value = getattr(event, src_attr, None)
        if value is not None:
            attrs[otel_attr] = _safe_str(value)
    # Mirror agent_role into strathon namespace for dashboard ergonomics
    if "gen_ai.agent.name" in attrs:
        attrs["strathon.agent.name"] = attrs["gen_ai.agent.name"]
    return attrs


def _extract_usage(usage: Any) -> Dict[str, Any]:
    """Pull token counts from a CrewAI usage dict-or-object (LiteLLM-style)."""
    if usage is None:
        return {}

    def _g(key):
        if isinstance(usage, dict):
            return usage.get(key)
        return getattr(usage, key, None)

    out: Dict[str, Any] = {}
    input_tokens = _g("prompt_tokens") or _g("input_tokens")
    output_tokens = _g("completion_tokens") or _g("output_tokens")
    total_tokens = _g("total_tokens")

    if input_tokens is not None:
        try:
            out["gen_ai.usage.input_tokens"] = int(input_tokens)
        except (TypeError, ValueError):
            pass
    if output_tokens is not None:
        try:
            out["gen_ai.usage.output_tokens"] = int(output_tokens)
        except (TypeError, ValueError):
            pass
    if total_tokens is not None:
        try:
            out["gen_ai.usage.total_tokens"] = int(total_tokens)
        except (TypeError, ValueError):
            pass
    return out


class StrathonCrewAIListener:
    """
    CrewAI BaseEventListener that mirrors events into Strathon OTel spans.

    Keys active OTel spans by the *event_id of the start event* so completion
    events can look up the right span via their started_event_id field.
    Parent relationships are reconstructed via parent_event_id.

    Designed so that one listener instance can survive many crew kickoffs;
    the internal map is cleared as each span ends.
    """

    # Do NOT subclass BaseEventListener at class-definition time; we resolve
    # the base class lazily in instrument() so the module can be imported
    # even when crewai is not installed.

    def __init__(self, client) -> None:
        self.client = client
        self._tracer = client.tracer
        # event_id (str) -> active OTel span
        self._spans: Dict[str, OTelSpan] = {}

    # ---- Lookup helpers ----

    def _start_span(
        self,
        name: str,
        event,
        extra_attrs: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Start an OTel span keyed by event.event_id, parented under parent_event_id."""
        attrs = _common_context_attrs(event)
        if extra_attrs:
            attrs.update({k: v for k, v in extra_attrs.items() if v is not None})

        parent_id = getattr(event, "parent_event_id", None)
        parent_span = self._spans.get(parent_id) if parent_id else None
        ctx = (
            otel_trace.set_span_in_context(parent_span)
            if parent_span is not None
            else None
        )

        span = self._tracer.start_span(name=name, context=ctx, attributes=attrs)
        self._spans[event.event_id] = span

    def _end_span(
        self,
        event,
        extra_attrs: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        """End the OTel span that matches this completion/error event."""
        started_id = getattr(event, "started_event_id", None) or event.event_id
        span = self._spans.pop(started_id, None)
        if span is None:
            return

        try:
            if extra_attrs:
                for k, v in extra_attrs.items():
                    if v is not None:
                        span.set_attribute(k, v)
            if error:
                span.set_status(Status(StatusCode.ERROR, error))
            else:
                span.set_status(Status(StatusCode.OK))
        except Exception:
            logger.exception(
                "Failed to set attributes on span for event %s", started_id
            )
        finally:
            try:
                span.end()
            except Exception:
                logger.exception("Failed to end span for event %s", started_id)

    # ---- BaseEventListener required override ----

    def setup_listeners(self, crewai_event_bus) -> None:
        """Register handlers on the CrewAI event bus."""
        # CrewAI ≥1.14 moved events from crewai.utilities.events to
        # crewai.events (PR #3425). Try new path first, fall back to
        # old for customers on older versions.
        try:
            from crewai.events import (
                CrewKickoffStartedEvent,
                CrewKickoffCompletedEvent,
                CrewKickoffFailedEvent,
                TaskStartedEvent,
                TaskCompletedEvent,
                TaskFailedEvent,
                AgentExecutionStartedEvent,
                AgentExecutionCompletedEvent,
                AgentExecutionErrorEvent,
                LLMCallStartedEvent,
                LLMCallCompletedEvent,
                LLMCallFailedEvent,
                ToolUsageStartedEvent,
                ToolUsageFinishedEvent,
                ToolUsageErrorEvent,
            )
        except ImportError:
            from crewai.utilities.events import (  # type: ignore[no-redef]
                CrewKickoffStartedEvent,
                CrewKickoffCompletedEvent,
                CrewKickoffFailedEvent,
                TaskStartedEvent,
                TaskCompletedEvent,
                TaskFailedEvent,
                AgentExecutionStartedEvent,
                AgentExecutionCompletedEvent,
                AgentExecutionErrorEvent,
                LLMCallStartedEvent,
                LLMCallCompletedEvent,
                LLMCallFailedEvent,
                ToolUsageStartedEvent,
                ToolUsageFinishedEvent,
                ToolUsageErrorEvent,
            )

        # ---- Crew ----

        @crewai_event_bus.on(CrewKickoffStartedEvent)
        def _on_crew_start(source, event):
            crew_name = getattr(event, "crew_name", None) or "crew"
            crew = getattr(event, "crew", None)
            attrs: Dict[str, Any] = {
                "gen_ai.workflow.name": _safe_str(crew_name),
                "strathon.crew.name": _safe_str(crew_name),
            }
            inputs = getattr(event, "inputs", None)
            if inputs:
                attrs["strathon.crew.inputs"] = _truncate(_json_or_str(inputs), 1000)
            if crew is not None:
                agents = getattr(crew, "agents", None)
                if agents:
                    attrs["strathon.crew.agent_count"] = len(agents)
                    attrs["strathon.crew.agent_roles"] = ",".join(
                        _safe_str(getattr(a, "role", "unknown")) for a in agents
                    )
                tasks = getattr(crew, "tasks", None)
                if tasks:
                    attrs["strathon.crew.task_count"] = len(tasks)
                process = getattr(crew, "process", None)
                if process is not None:
                    attrs["strathon.crew.process"] = _safe_str(process)
            self._start_span(f"crewai.crew.{crew_name}", event, attrs)

        @crewai_event_bus.on(CrewKickoffCompletedEvent)
        def _on_crew_complete(source, event):
            attrs: Dict[str, Any] = {}
            total_tokens = getattr(event, "total_tokens", None)
            if total_tokens is not None:
                try:
                    attrs["gen_ai.usage.total_tokens"] = int(total_tokens)
                except (TypeError, ValueError):
                    pass
            output = getattr(event, "output", None)
            if output is not None:
                attrs["strathon.crew.output"] = _truncate(_safe_str(output), 1500)
            self._end_span(event, attrs)

        @crewai_event_bus.on(CrewKickoffFailedEvent)
        def _on_crew_failed(source, event):
            err = getattr(event, "error", None) or "Crew kickoff failed"
            self._end_span(event, error=_safe_str(err))

        # ---- Task ----

        @crewai_event_bus.on(TaskStartedEvent)
        def _on_task_start(source, event):
            task_name = getattr(event, "task_name", None) or "task"
            attrs: Dict[str, Any] = {}
            task = getattr(event, "task", None)
            if task is not None:
                desc = getattr(task, "description", None)
                if desc:
                    attrs["strathon.task.description"] = _truncate(desc, 1000)
                expected = getattr(task, "expected_output", None)
                if expected:
                    attrs["strathon.task.expected_output"] = _truncate(expected, 500)
            ctx = getattr(event, "context", None)
            if ctx:
                attrs["strathon.task.context"] = _truncate(ctx, 1000)
            self._start_span(f"crewai.task.{task_name}", event, attrs)

        @crewai_event_bus.on(TaskCompletedEvent)
        def _on_task_complete(source, event):
            attrs: Dict[str, Any] = {}
            output = getattr(event, "output", None)
            if output is not None:
                raw = getattr(output, "raw", None) or output
                attrs["strathon.task.output"] = _truncate(_safe_str(raw), 1500)
            self._end_span(event, attrs)

        @crewai_event_bus.on(TaskFailedEvent)
        def _on_task_failed(source, event):
            err = getattr(event, "error", None) or "Task failed"
            self._end_span(event, error=_safe_str(err))

        # ---- Agent execution ----

        @crewai_event_bus.on(AgentExecutionStartedEvent)
        def _on_agent_start(source, event):
            attrs: Dict[str, Any] = {}
            agent = getattr(event, "agent", None)
            if agent is not None:
                goal = getattr(agent, "goal", None)
                if goal:
                    attrs["strathon.agent.goal"] = _truncate(goal, 500)
                backstory = getattr(agent, "backstory", None)
                if backstory:
                    attrs["strathon.agent.backstory"] = _truncate(backstory, 500)
                allow_delegation = getattr(agent, "allow_delegation", None)
                if allow_delegation is not None:
                    attrs["strathon.agent.allow_delegation"] = bool(allow_delegation)
            tools = getattr(event, "tools", None)
            if tools:
                tool_names = [_safe_str(getattr(t, "name", "tool")) for t in tools]
                attrs["strathon.agent.available_tools"] = ",".join(tool_names)
            prompt = getattr(event, "task_prompt", None)
            if prompt:
                attrs["strathon.agent.task_prompt"] = _truncate(prompt, 1500)

            # Fall back to agent.role if event.agent_role wasn't auto-populated
            role_from_agent = _safe_str(getattr(agent, "role", "")) if agent is not None else ""
            event_role = getattr(event, "agent_role", None)
            effective_role = event_role or role_from_agent or "agent"
            if not event_role and role_from_agent:
                attrs["gen_ai.agent.name"] = role_from_agent
                attrs["strathon.agent.name"] = role_from_agent
            self._start_span(f"crewai.agent.{effective_role}", event, attrs)

        @crewai_event_bus.on(AgentExecutionCompletedEvent)
        def _on_agent_complete(source, event):
            attrs: Dict[str, Any] = {}
            output = getattr(event, "output", None)
            if output is not None:
                attrs["strathon.agent.output"] = _truncate(_safe_str(output), 1500)
            self._end_span(event, attrs)

        @crewai_event_bus.on(AgentExecutionErrorEvent)
        def _on_agent_error(source, event):
            err = getattr(event, "error", None) or "Agent execution failed"
            self._end_span(event, error=_safe_str(err))

        # ---- LLM call ----

        @crewai_event_bus.on(LLMCallStartedEvent)
        def _on_llm_start(source, event):
            model = getattr(event, "model", None)
            attrs: Dict[str, Any] = {
                "gen_ai.operation.name": "chat",
            }
            if model:
                attrs["gen_ai.request.model"] = _safe_str(model)
                provider = _provider_from_model(model)
                if provider:
                    attrs["gen_ai.provider.name"] = provider
            call_id = getattr(event, "call_id", None)
            if call_id:
                attrs["strathon.llm.call_id"] = _safe_str(call_id)
            self._start_span("crewai.llm", event, attrs)

        @crewai_event_bus.on(LLMCallCompletedEvent)
        def _on_llm_complete(source, event):
            attrs: Dict[str, Any] = {}
            usage = getattr(event, "usage", None)
            attrs.update(_extract_usage(usage))
            call_type = getattr(event, "call_type", None)
            if call_type is not None:
                attrs["strathon.llm.call_type"] = _safe_str(call_type)
            # Newer field on completion; mirror as response model when present
            response = getattr(event, "response", None)
            if response is not None and not isinstance(response, str):
                model_field = getattr(response, "model", None)
                if model_field:
                    attrs["gen_ai.response.model"] = _safe_str(model_field)
            self._end_span(event, attrs)

        @crewai_event_bus.on(LLMCallFailedEvent)
        def _on_llm_failed(source, event):
            err = getattr(event, "error", None) or "LLM call failed"
            self._end_span(event, error=_safe_str(err))

        # ---- Tool usage ----

        @crewai_event_bus.on(ToolUsageStartedEvent)
        def _on_tool_start(source, event):
            tool_name = getattr(event, "tool_name", None) or "tool"
            attrs: Dict[str, Any] = {
                "gen_ai.tool.name": _safe_str(tool_name),
                "strathon.tool.name": _safe_str(tool_name),
            }
            tool_class = getattr(event, "tool_class", None)
            if tool_class:
                attrs["strathon.tool.class"] = _safe_str(tool_class)
            tool_args = getattr(event, "tool_args", None)
            if tool_args is not None:
                attrs["strathon.tool.args"] = _truncate(_json_or_str(tool_args), 1500)
            run_attempts = getattr(event, "run_attempts", None)
            if run_attempts is not None:
                attrs["strathon.tool.run_attempts"] = int(run_attempts)
            delegations = getattr(event, "delegations", None)
            if delegations is not None:
                attrs["strathon.tool.delegations"] = int(delegations)
            self._start_span(f"crewai.tool.{tool_name}", event, attrs)

        @crewai_event_bus.on(ToolUsageFinishedEvent)
        def _on_tool_complete(source, event):
            attrs: Dict[str, Any] = {}
            output = getattr(event, "output", None)
            if output is not None:
                attrs["strathon.tool.output"] = _truncate(_safe_str(output), 1500)
            from_cache = getattr(event, "from_cache", None)
            if from_cache is not None:
                attrs["strathon.tool.from_cache"] = bool(from_cache)
            self._end_span(event, attrs)

        @crewai_event_bus.on(ToolUsageErrorEvent)
        def _on_tool_error(source, event):
            err = getattr(event, "error", None) or "Tool usage failed"
            self._end_span(event, error=_safe_str(err))


# Module-level singleton to keep listener alive past instrument() return
_REGISTERED_LISTENER = None

# Policy enforcement: we patch CrewStructuredTool.invoke at instrument() time so
# that every CrewAI tool execution goes through client.check_policy() first.
# The event listener can't be used for blocking because the CrewAI event bus
# dispatches handlers in a thread pool, returns a Future without waiting on it,
# and catches handler exceptions internally — none of which can stop the
# tool from running.
#
# CrewStructuredTool.invoke() is the single entry point every tool call goes
# through; patching it once is far less risky than the 4-method monkey-patch we
# considered (and rejected) for observability. The original observability
# instrumentation continues via the event listener.
#
# Async tools are covered by this same patch, not a separate path. CrewAI does
# NOT expose a tool-level ainvoke that bypasses invoke: CrewStructuredTool.invoke
# itself detects a coroutine tool function and runs it (asyncio.run on the
# coroutine) inside invoke. The ainvoke() that exists in CrewAI is on the agent
# EXECUTOR (its async iteration loop), and that loop still dispatches tool calls
# through CrewStructuredTool.invoke. So patching invoke enforces policy on every
# tool call regardless of sync/async tool function or sync/async crew. (Verified
# against CrewAI's structured_tool.invoke and crew_agent_executor; revisit if a
# future CrewAI version adds a tool-level async entry point that skips invoke.)
_ORIGINAL_INVOKE = None
_PATCHED_CLIENT = None


def _install_policy_patch(client) -> bool:
    """Patch CrewStructuredTool.invoke so policy enforcement runs before each tool.

    The decision branching (block / steer / allow) and the intervention
    span emission live in ``strathon.policy.steer.dispatch_policy_decision``;
    this function just builds the per-tool span context and supplies the
    "run the real body" callback. That keeps CrewAI's policy behavior
    identical to LangGraph's enforce_steer path by construction.

    Idempotent: subsequent calls only update which client check_policy
    goes to. No-op if the client has no policy enforcer.

    Returns True if a patch is in place (newly applied or already there).
    """
    global _ORIGINAL_INVOKE, _PATCHED_CLIENT

    # If policies are disabled on this client, nothing to do.
    if getattr(client, "_policy_enforcer", None) is None:
        return False

    try:
        from crewai.tools.structured_tool import CrewStructuredTool
    except ImportError:
        return False

    # Already patched: just retarget which client check_policy queries.
    if _ORIGINAL_INVOKE is not None:
        _PATCHED_CLIENT = client
        return True

    _ORIGINAL_INVOKE = CrewStructuredTool.invoke
    _PATCHED_CLIENT = client

    # Import inside install so a missing crewai install at module load
    # doesn't break unrelated imports of strathon.policy.steer.
    from strathon.policy.steer import (
        build_tool_span_attrs,
        dispatch_policy_decision,
    )

    def _policy_aware_invoke(self, input, config=None, **kwargs):
        # Re-resolve client every call so retarget works without
        # rewriting the patch.
        current_client = _PATCHED_CLIENT
        if (
            current_client is None
            or getattr(current_client, "_policy_enforcer", None) is None
        ):
            return _ORIGINAL_INVOKE(self, input, config, **kwargs)

        attrs = build_tool_span_attrs(self, input, framework="crewai")
        tool_name = attrs["strathon.tool.name"]

        def _on_allow():
            return _ORIGINAL_INVOKE(self, input, config, **kwargs)

        return dispatch_policy_decision(
            current_client,
            span_name=f"crewai.tool.{tool_name}",
            attrs=attrs,
            on_allow=_on_allow,
        )

    # Dynamic method replacement on a class we don't own. Mypy doesn't
    # model this pattern (which is intentional for instrumentation
    # libraries that wrap third-party APIs); the install is tested and
    # the corresponding _uninstall_policy_patch restores the original.
    CrewStructuredTool.invoke = _policy_aware_invoke  # type: ignore[method-assign]
    logger.info(
        "CrewAI policy enforcement patch installed on CrewStructuredTool.invoke"
    )
    return True


def _uninstall_policy_patch() -> None:
    """Restore the original CrewStructuredTool.invoke. For tests and cleanup."""
    global _ORIGINAL_INVOKE, _PATCHED_CLIENT

    if _ORIGINAL_INVOKE is None:
        return
    try:
        from crewai.tools.structured_tool import CrewStructuredTool
        CrewStructuredTool.invoke = _ORIGINAL_INVOKE  # type: ignore[method-assign]
    except ImportError:
        pass
    _ORIGINAL_INVOKE = None
    _PATCHED_CLIENT = None


def instrument(client) -> bool:
    """
    Register the Strathon listener with CrewAI's event bus.

    Args:
        client: Strathon Client instance.

    Returns:
        True if crewai is installed and instrumentation was registered.
        False if crewai is not installed.
    """
    global _REGISTERED_LISTENER

    try:
        from crewai.events.base_event_listener import BaseEventListener
    except ImportError:
        logger.debug("crewai not installed; skipping instrumentation")
        return False

    if _REGISTERED_LISTENER is not None:
        # Replace existing listener's tracer to point at the new client without
        # double-registering handlers on the bus.
        _REGISTERED_LISTENER._tracer = client.tracer
        _REGISTERED_LISTENER.client = client
        # Update which client the policy patch routes through.
        _install_policy_patch(client)
        logger.info("CrewAI instrumentation updated to use new client")
        return True

    # Dynamically build a subclass so the module is importable when crewai
    # is missing. StrathonCrewAIListener provides setup_listeners.
    class _BoundListener(StrathonCrewAIListener, BaseEventListener):
        def __init__(self, client):
            StrathonCrewAIListener.__init__(self, client)
            BaseEventListener.__init__(self)  # triggers setup_listeners via the bus

    _REGISTERED_LISTENER = _BoundListener(client)

    # Install policy enforcement patch on CrewStructuredTool.invoke.
    # No-op if the client has policies disabled.
    _install_policy_patch(client)

    logger.info("CrewAI instrumentation registered")
    return True
