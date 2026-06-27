"""OpenAI Agents SDK instrumentation for Strathon.

Registers a TracingProcessor with the OpenAI Agents SDK that mirrors every
trace and span the SDK emits into OpenTelemetry spans on the Strathon Client's
tracer, following OTel GenAI semantic conventions plus Strathon-specific
strathon.agent.* attributes for topology and intervention.

Integration uses the SDK's documented extension point (add_trace_processor)
rather than monkey-patching, so it stays compatible across SDK versions.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from opentelemetry import trace as otel_trace
from opentelemetry.trace import Span as OTelSpan, Status, StatusCode

logger = logging.getLogger(__name__)


class StrathonAgentsSDKProcessor:
    """
    OpenAI Agents SDK TracingProcessor that mirrors spans into Strathon via OTel.

    Maintains a map of the SDK's string-form span/trace IDs to active OTel
    spans so child spans can be parented correctly.
    """

    def __init__(self, client) -> None:
        self.client = client
        self._tracer = client.tracer
        # Map: SDK span_id (str) -> active OTel span
        self._otel_spans: Dict[str, OTelSpan] = {}
        # Map: SDK trace_id (str) -> root OTel span
        self._trace_roots: Dict[str, OTelSpan] = {}

    # ---- TracingProcessor interface ----

    def on_trace_start(self, trace) -> None:
        try:
            workflow_name = getattr(trace, "name", None) or "agent_workflow"
            attrs: Dict[str, Any] = {
                "strathon.framework": "openai_agents_sdk",
                "gen_ai.workflow.name": workflow_name,
                "openai_agents.trace_id": str(trace.trace_id),
            }
            group_id = getattr(trace, "group_id", None)
            if group_id:
                attrs["gen_ai.conversation.id"] = str(group_id)

            span = self._tracer.start_span(
                name=f"agents.workflow.{workflow_name}",
                attributes=attrs,
            )
            self._trace_roots[trace.trace_id] = span
            # Also register under span_id-style key so children with no parent_id can find it
            self._otel_spans[trace.trace_id] = span
        except Exception:
            logger.exception("on_trace_start failed for trace_id=%s", getattr(trace, "trace_id", None))

    def on_trace_end(self, trace) -> None:
        span = self._trace_roots.pop(trace.trace_id, None)
        self._otel_spans.pop(trace.trace_id, None)
        if span is None:
            return
        try:
            span.set_status(Status(StatusCode.OK))
            span.end()
        except Exception:
            logger.exception("on_trace_end failed for trace_id=%s", trace.trace_id)

    def on_span_start(self, span) -> None:
        try:
            parent_otel = self._lookup_parent(span)
            ctx = (
                otel_trace.set_span_in_context(parent_otel) if parent_otel is not None else None
            )

            name = self._span_name(span)
            attrs = self._initial_attrs(span)

            otel_span = self._tracer.start_span(
                name=name,
                context=ctx,
                attributes=attrs,
            )
            self._otel_spans[span.span_id] = otel_span
        except Exception:
            logger.exception("on_span_start failed for span_id=%s", getattr(span, "span_id", None))

    def on_span_end(self, span) -> None:
        otel_span = self._otel_spans.pop(span.span_id, None)
        if otel_span is None:
            return
        try:
            for key, value in self._final_attrs(span).items():
                if value is not None:
                    otel_span.set_attribute(key, value)

            err = getattr(span, "error", None)
            if err:
                otel_span.set_status(Status(StatusCode.ERROR, str(err)))
            else:
                otel_span.set_status(Status(StatusCode.OK))
        except Exception:
            logger.exception("on_span_end attribute set failed for span_id=%s", span.span_id)
        finally:
            try:
                otel_span.end()
            except Exception:
                logger.exception("on_span_end finalize failed for span_id=%s", span.span_id)

    def shutdown(self) -> None:
        """End any leftover open spans on shutdown."""
        for otel_span in list(self._otel_spans.values()):
            try:
                otel_span.end()
            except Exception:
                pass
        self._otel_spans.clear()
        self._trace_roots.clear()

    def force_flush(self) -> None:
        """Force a synchronous flush via the Strathon client."""
        try:
            self.client.flush()
        except Exception:
            logger.exception("force_flush failed")

    # ---- Internal helpers ----

    def _lookup_parent(self, span) -> Optional[OTelSpan]:
        """Find the parent OTel span via parent_id, falling back to the trace root."""
        parent_id = getattr(span, "parent_id", None)
        if parent_id and parent_id in self._otel_spans:
            return self._otel_spans[parent_id]
        return self._trace_roots.get(span.trace_id)

    @staticmethod
    def _span_name(span) -> str:
        """Derive a human-readable OTel span name from the SDK span_data type."""
        data = getattr(span, "span_data", None)
        if data is None:
            return "agents.span"

        type_name = type(data).__name__
        mapping = {
            "AgentSpanData": "agents.agent",
            "GenerationSpanData": "agents.generation",
            "ResponseSpanData": "agents.response",
            "FunctionSpanData": "agents.tool",
            "HandoffSpanData": "agents.handoff",
            "GuardrailSpanData": "agents.guardrail",
            "CustomSpanData": "agents.custom",
            "TurnSpanData": "agents.turn",
            "TaskSpanData": "agents.task",
            "MCPListToolsSpanData": "agents.mcp.list_tools",
            "SpeechSpanData": "agents.speech",
            "SpeechGroupSpanData": "agents.speech_group",
            "TranscriptionSpanData": "agents.transcription",
        }
        return mapping.get(type_name, f"agents.{type_name.lower().replace('spandata', '')}")

    @staticmethod
    def _initial_attrs(span) -> Dict[str, Any]:
        """Map SDK span_data fields known at span start to OTel attributes."""
        attrs: Dict[str, Any] = {
            "strathon.framework": "openai_agents_sdk",
            "openai_agents.span_id": str(span.span_id),
            "openai_agents.trace_id": str(span.trace_id),
        }

        data = getattr(span, "span_data", None)
        if data is None:
            return attrs

        type_name = type(data).__name__

        # Agent turn: an LLM-driven decision step
        if type_name == "AgentSpanData":
            name = getattr(data, "name", None)
            if name:
                attrs["gen_ai.agent.name"] = str(name)
                attrs["strathon.agent.name"] = str(name)
            handoffs = getattr(data, "handoffs", None)
            if handoffs:
                attrs["strathon.agent.handoff_targets"] = ",".join(str(h) for h in handoffs)
            tools = getattr(data, "tools", None)
            if tools:
                attrs["strathon.agent.available_tools"] = ",".join(str(t) for t in tools)
            output_type = getattr(data, "output_type", None)
            if output_type:
                attrs["strathon.agent.output_type"] = str(output_type)

        # LLM generation
        elif type_name == "GenerationSpanData":
            model = getattr(data, "model", None)
            if model:
                attrs["gen_ai.request.model"] = str(model)
            attrs["gen_ai.operation.name"] = "chat"
            attrs["gen_ai.provider.name"] = "openai"

        # Response API span (newer OAI agents path)
        elif type_name == "ResponseSpanData":
            attrs["gen_ai.operation.name"] = "responses"
            attrs["gen_ai.provider.name"] = "openai"

        # Tool / function call
        elif type_name == "FunctionSpanData":
            name = getattr(data, "name", None)
            if name:
                attrs["gen_ai.tool.name"] = str(name)
            tool_input = getattr(data, "input", None)
            if tool_input is not None:
                # Canonical attribute across all frameworks
                attrs["strathon.tool.args"] = _truncate(str(tool_input), 2000)

        # Agent-to-agent handoff
        elif type_name == "HandoffSpanData":
            frm = getattr(data, "from_agent", None)
            to = getattr(data, "to_agent", None)
            if frm:
                attrs["strathon.agent.handoff.from"] = str(frm)
            if to:
                attrs["strathon.agent.handoff.to"] = str(to)

        # Guardrail check
        elif type_name == "GuardrailSpanData":
            name = getattr(data, "name", None)
            if name:
                attrs["strathon.guardrail.name"] = str(name)

        # Per-turn span
        elif type_name == "TurnSpanData":
            turn = getattr(data, "turn", None)
            if turn is not None:
                attrs["strathon.agent.turn"] = int(turn)
            agent_name = getattr(data, "agent_name", None)
            if agent_name:
                attrs["gen_ai.agent.name"] = str(agent_name)
                attrs["strathon.agent.name"] = str(agent_name)

        # MCP server interaction
        elif type_name == "MCPListToolsSpanData":
            server = getattr(data, "server", None)
            if server:
                attrs["strathon.mcp.server"] = str(server)

        # Custom user span
        elif type_name == "CustomSpanData":
            name = getattr(data, "name", None)
            if name:
                attrs["strathon.custom.name"] = str(name)

        return attrs

    @staticmethod
    def _final_attrs(span) -> Dict[str, Any]:
        """Attributes only available at span end: usage, output, guardrail result."""
        attrs: Dict[str, Any] = {}
        data = getattr(span, "span_data", None)
        if data is None:
            return attrs

        type_name = type(data).__name__

        # Token usage on LLM-emitting spans
        usage = getattr(data, "usage", None) if hasattr(data, "usage") else None
        if usage:
            attrs.update(_extract_usage(usage))

        if type_name == "FunctionSpanData":
            output = getattr(data, "output", None)
            if output is not None:
                attrs["strathon.tool.output"] = _truncate(str(output), 2000)

        elif type_name == "GuardrailSpanData":
            triggered = getattr(data, "triggered", None)
            if triggered is not None:
                attrs["strathon.guardrail.triggered"] = bool(triggered)

        elif type_name == "ResponseSpanData":
            response = getattr(data, "response", None)
            if response is not None:
                model = getattr(response, "model", None)
                if model:
                    attrs["gen_ai.response.model"] = str(model)

        return attrs


def _extract_usage(usage: Any) -> Dict[str, Any]:
    """Pull token counts out of a usage dict-or-object."""
    out: Dict[str, Any] = {}

    def _g(key):
        if isinstance(usage, dict):
            return usage.get(key)
        return getattr(usage, key, None)

    # OpenAI canonical names
    input_tokens = _g("input_tokens") or _g("prompt_tokens")
    output_tokens = _g("output_tokens") or _g("completion_tokens")
    total_tokens = _g("total_tokens")

    if input_tokens is not None:
        out["gen_ai.usage.input_tokens"] = int(input_tokens)
    if output_tokens is not None:
        out["gen_ai.usage.output_tokens"] = int(output_tokens)
    if total_tokens is not None:
        out["gen_ai.usage.total_tokens"] = int(total_tokens)

    return out


def _truncate(s: str, max_len: int) -> str:
    """Truncate large attribute values so we don't blow up span payloads."""
    if len(s) <= max_len:
        return s
    return s[:max_len] + f"... [truncated {len(s) - max_len} chars]"


# ============================================================
# Runtime intervention: policy enforcement via RunHooks injection
# ============================================================
# OpenAI Agents SDK's TracingProcessor is informational only — it fires
# alongside execution and can't stop a tool from running. Blocking requires
# RunHooks.on_tool_start, which is awaited via asyncio.gather BEFORE the
# tool's invoke task is created (see agents/run_internal/tool_execution.py
# line 1722). Raising from on_tool_start propagates through gather and
# aborts the run before the tool body executes.
#
# Auto-instrumentation challenge: RunHooks is normally passed by the user
# to Runner.run(agent, hooks=...). For zero-code-change instrumentation we
# wrap Runner.run / run_sync / run_streamed to inject our hooks. If the
# user already provides hooks, we delegate to theirs after our own.
#
# steer mode via RunHooks: not supported. RunHooks.on_tool_start can
# observe a tool call and raise, but cannot substitute the tool's output
# with a replacement string. For full steer semantics on OpenAI Agents
# SDK, use the Tool Guardrails path at the bottom of this module:
#
#   from strathon.instrumentation.openai_agents import attach_strathon_guardrails
#   attach_strathon_guardrails(agent, client)
#
# That path uses agents.tool_guardrails.ToolGuardrailFunctionOutput.reject_content
# which the runtime substitutes for the tool's output without running the
# body. The block path below still raises StrathonPolicyBlocked from
# on_tool_start as before; the two paths coexist.

_ORIGINAL_RUN = None
_ORIGINAL_RUN_SYNC = None
_ORIGINAL_RUN_STREAMED = None
_PATCHED_CLIENT = None


def _build_strathon_run_hooks(client, user_hooks):
    """Construct a RunHooks subclass bound to this client + delegating to user_hooks.

    Built lazily so the agents import is not required at module load time.
    """
    from agents import RunHooks

    class _StrathonRunHooks(RunHooks):
        async def on_tool_start(self, context, agent, tool):
            enforcer = getattr(client, "_policy_enforcer", None)
            if enforcer is None:
                if user_hooks is not None:
                    await user_hooks.on_tool_start(context, agent, tool)
                return

            tool_name = _safe_str(getattr(tool, "name", "tool"))

            # ToolContext.tool_arguments holds the JSON-decoded arguments
            # the model passed. Fall back to context.tool_call.arguments for
            # older SDK versions that don't expose tool_arguments directly.
            args = getattr(context, "tool_arguments", None)
            if args is None:
                tc = getattr(context, "tool_call", None)
                args = getattr(tc, "arguments", None) if tc is not None else None

            attrs: Dict[str, Any] = {
                "strathon.framework": "agents",
                "gen_ai.tool.name": tool_name,
                "strathon.tool.name": tool_name,
                "strathon.tool.args": _truncate(_safe_json(args), 1500),
            }

            from strathon.policy.steer import check_halt_or_raise
            check_halt_or_raise(client, f"agents.tool.{tool_name}", attrs)
            try:
                decision = client.check_policy({
                    "name": f"agents.tool.{tool_name}",
                    "attrs": attrs,
                })
            except Exception:
                logger.exception("policy check raised in OAI Agents hook; allowing tool")
                if user_hooks is not None:
                    await user_hooks.on_tool_start(context, agent, tool)
                return

            if decision.is_block:
                _emit_intervention_span_oai(
                    client,
                    tool_name=tool_name,
                    attrs=attrs,
                    decision_kind="blocked",
                    decision=decision,
                    error_message=decision.message or "policy blocked",
                )
                from strathon.policy import StrathonPolicyBlocked
                raise StrathonPolicyBlocked(
                    decision.message or f"Tool '{tool_name}' blocked by Strathon policy",
                    policy_id=decision.policy_id,
                    policy_name=decision.policy_name,
                )

            if decision.is_throttle:
                _emit_intervention_span_oai(
                    client,
                    tool_name=tool_name,
                    attrs=attrs,
                    decision_kind="throttled",
                    decision=decision,
                    error_message=decision.message or "policy throttled",
                )
                from strathon.policy import StrathonPolicyThrottled
                raise StrathonPolicyThrottled(
                    decision.message
                    or f"Tool '{tool_name}' rate-limited by Strathon policy",
                    policy_id=decision.policy_id,
                    policy_name=decision.policy_name,
                    retry_after_seconds=decision.retry_after_seconds,
                )

            if decision.is_require_approval:
                # on_tool_start is async and is awaited before the tool body
                # runs, so we can block here for a human decision without
                # freezing the event loop (await_for_approval runs the poll
                # off-loop). Approved -> fall through and run the tool. Denied,
                # expired, or timed out -> StrathonApprovalDenied (a
                # StrathonPolicyBlocked subclass) propagates and the tool body
                # never runs. This is real approval enforcement, not observe.
                from strathon.policy import await_for_approval
                try:
                    await await_for_approval(
                        client,
                        decision,
                        {"name": f"agents.tool.{tool_name}", "attrs": attrs},
                    )
                except Exception as approval_exc:
                    _emit_intervention_span_oai(
                        client,
                        tool_name=tool_name,
                        attrs=attrs,
                        decision_kind="approval_denied",
                        decision=decision,
                        error_message=str(approval_exc) or "approval denied",
                    )
                    raise
                _emit_intervention_span_oai(
                    client,
                    tool_name=tool_name,
                    attrs=attrs,
                    decision_kind="approval_granted",
                    decision=decision,
                    error_message=None,
                )
                # Approved: continue to run the tool (fall through below).

            if decision.is_steer:
                # on_tool_start cannot substitute the tool's return value, so
                # full steer semantics aren't possible without a deeper hook.
                # Log the matched policy and allow the tool to run; this still
                # surfaces in observability and gives the user a signal that
                # they should consider switching to a `block` policy or
                # wrapping their tool's on_invoke_tool manually.
                logger.warning(
                    "Strathon: steer policy %r matched on OAI tool %s but "
                    "steer cannot be enforced through RunHooks. Attach the "
                    "Strathon guardrail to enable steer: "
                    "from strathon.instrumentation.openai_agents import "
                    "attach_strathon_guardrails; "
                    "attach_strathon_guardrails(agent, client). The tool "
                    "will run normally for this call.",
                    decision.policy_name, tool_name,
                )
                _emit_intervention_span_oai(
                    client,
                    tool_name=tool_name,
                    attrs=attrs,
                    decision_kind="steer_attempted",
                    decision=decision,
                    error_message=None,
                    replacement=decision.replacement,
                )

            if user_hooks is not None:
                await user_hooks.on_tool_start(context, agent, tool)

        async def on_tool_end(self, context, agent, tool, result):
            if user_hooks is not None:
                await user_hooks.on_tool_end(context, agent, tool, result)

        async def on_agent_start(self, context, agent):
            if user_hooks is not None:
                await user_hooks.on_agent_start(context, agent)

        async def on_agent_end(self, context, agent, output):
            if user_hooks is not None:
                await user_hooks.on_agent_end(context, agent, output)

        async def on_handoff(self, context, from_agent, to_agent):
            if user_hooks is not None:
                await user_hooks.on_handoff(context, from_agent, to_agent)

        async def on_llm_start(self, *args, **kwargs):
            if user_hooks is not None and hasattr(user_hooks, "on_llm_start"):
                await user_hooks.on_llm_start(*args, **kwargs)

        async def on_llm_end(self, *args, **kwargs):
            if user_hooks is not None and hasattr(user_hooks, "on_llm_end"):
                await user_hooks.on_llm_end(*args, **kwargs)

    return _StrathonRunHooks()


def _emit_intervention_span_oai(
    client,
    *,
    tool_name: str,
    attrs: Dict[str, Any],
    decision_kind: str,  # 'blocked' | 'steered' | 'steer_attempted'
    decision,
    error_message: Optional[str] = None,
    replacement: Optional[str] = None,
) -> None:
    """Open and immediately close a span recording an intervention decision.

    Lets the server-side audit trail (policy_matches table) populate when the
    span is ingested. Best-effort: any failure here is swallowed.
    """
    try:
        tracer = client.tracer
    except Exception:
        return

    span_attrs = dict(attrs)
    span_attrs[f"strathon.policy.{decision_kind}"] = True
    if decision.policy_id:
        span_attrs["strathon.policy.id"] = decision.policy_id
    if decision.policy_name:
        span_attrs["strathon.policy.name"] = decision.policy_name
    if decision.message:
        span_attrs["strathon.policy.message"] = decision.message
    if replacement is not None:
        span_attrs["strathon.policy.replacement"] = _truncate(replacement, 1500)

    try:
        span = tracer.start_span(
            name=f"agents.tool.{tool_name}",
            attributes=span_attrs,
        )
        try:
            if decision_kind in ("blocked", "throttled", "approval_denied"):
                span.set_status(Status(StatusCode.ERROR, error_message or "policy blocked"))
            else:
                span.set_status(Status(StatusCode.OK))
        finally:
            span.end()
    except Exception:
        logger.debug("failed to emit intervention span for %s", tool_name, exc_info=True)


def _safe_str(value: Any) -> str:
    if value is None:
        return ""
    try:
        return str(value)
    except Exception:
        return ""


def _safe_json(value: Any) -> str:
    """Render a value as JSON when possible, falling back to str()."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        import json
        return json.dumps(value, default=str)
    except Exception:
        try:
            return str(value)
        except Exception:
            return ""


def _install_policy_patch(client) -> bool:
    """Patch Runner.run / run_sync / run_streamed to inject our policy hooks.

    Idempotent: subsequent calls retarget which client check_policy queries.
    No-op if the client has policies disabled.
    """
    global _ORIGINAL_RUN, _ORIGINAL_RUN_SYNC, _ORIGINAL_RUN_STREAMED, _PATCHED_CLIENT

    if getattr(client, "_policy_enforcer", None) is None:
        return False

    try:
        from agents import Runner
    except ImportError:
        return False

    if _ORIGINAL_RUN is not None:
        _PATCHED_CLIENT = client
        return True

    # __func__ unwraps the classmethod descriptor so we can rebuild a classmethod
    # over our wrapper. Runner.run, .run_sync, .run_streamed are all classmethods.
    _ORIGINAL_RUN = Runner.__dict__["run"].__func__
    _ORIGINAL_RUN_SYNC = Runner.__dict__["run_sync"].__func__
    _ORIGINAL_RUN_STREAMED = Runner.__dict__["run_streamed"].__func__
    _PATCHED_CLIENT = client

    async def _strathon_run(cls, starting_agent, input, *, hooks=None, **kwargs):
        merged = _build_strathon_run_hooks(_PATCHED_CLIENT, hooks)
        return await _ORIGINAL_RUN(cls, starting_agent, input, hooks=merged, **kwargs)

    def _strathon_run_sync(cls, starting_agent, input, *, hooks=None, **kwargs):
        merged = _build_strathon_run_hooks(_PATCHED_CLIENT, hooks)
        return _ORIGINAL_RUN_SYNC(cls, starting_agent, input, hooks=merged, **kwargs)

    def _strathon_run_streamed(cls, starting_agent, input, *args, hooks=None, **kwargs):
        merged = _build_strathon_run_hooks(_PATCHED_CLIENT, hooks)
        return _ORIGINAL_RUN_STREAMED(cls, starting_agent, input, *args, hooks=merged, **kwargs)

    # The Runner.run* methods are bound classmethods on a framework
    # class we don't own. We're replacing them at import time with our
    # wrapper that injects strathon RunHooks before delegating to the
    # original. Mypy doesn't model dynamic method-replacement (it sees
    # the framework's typed classmethod signature and the type of a
    # freshly-built classmethod() object as incompatible). The patch is
    # intentional and tested; the type: ignores are scoped to the
    # exact lines so other type errors in this file aren't masked.
    Runner.run = classmethod(_strathon_run)  # type: ignore[method-assign,assignment]
    Runner.run_sync = classmethod(_strathon_run_sync)  # type: ignore[method-assign,assignment]
    Runner.run_streamed = classmethod(_strathon_run_streamed)  # type: ignore[method-assign,assignment]

    logger.info("OpenAI Agents SDK policy enforcement patch installed on Runner.run*")
    return True


def _uninstall_policy_patch() -> None:
    """Restore the original Runner classmethods. For tests."""
    global _ORIGINAL_RUN, _ORIGINAL_RUN_SYNC, _ORIGINAL_RUN_STREAMED, _PATCHED_CLIENT

    if _ORIGINAL_RUN is None:
        return
    try:
        from agents import Runner
        # Same dynamic-method-replacement pattern as the install path.
        Runner.run = classmethod(_ORIGINAL_RUN)  # type: ignore[method-assign,assignment,arg-type]
        Runner.run_sync = classmethod(_ORIGINAL_RUN_SYNC)  # type: ignore[method-assign,assignment,arg-type]
        Runner.run_streamed = classmethod(_ORIGINAL_RUN_STREAMED)  # type: ignore[method-assign,assignment,arg-type]
    except ImportError:
        pass
    _ORIGINAL_RUN = None
    _ORIGINAL_RUN_SYNC = None
    _ORIGINAL_RUN_STREAMED = None
    _PATCHED_CLIENT = None


def instrument(client) -> bool:
    """
    Register the Strathon TracingProcessor with the OpenAI Agents SDK.

    Also installs a policy enforcement patch on Runner.run / run_sync /
    run_streamed that injects RunHooks for block enforcement before each
    tool call. No-op if the client has policies disabled.

    Args:
        client: Strathon Client instance.

    Returns:
        True if openai-agents is installed and instrumentation was registered,
        False otherwise (e.g. package not installed).
    """
    try:
        from agents import add_trace_processor
    except ImportError:
        logger.debug("openai-agents not installed; skipping instrumentation")
        return False

    processor = StrathonAgentsSDKProcessor(client)
    # StrathonAgentsSDKProcessor implements the methods the framework's
    # TracingProcessor Protocol declares but doesn't formally inherit
    # from it (the framework's class is not stable across versions and
    # importing it conditionally creates a circular dep). At runtime
    # it duck-types correctly; mypy sees the missing nominal bond.
    add_trace_processor(processor)  # type: ignore[arg-type]

    # Install policy enforcement patch on Runner.run*.
    # No-op if the client has policies disabled.
    _install_policy_patch(client)

    logger.info("OpenAI Agents SDK instrumentation registered")
    return True


# ===========================================================================
# Steer enforcement via Tool Guardrails (added in scopes-parity work)
# ===========================================================================
#
# Earlier OAI Agents integrations couldn't enforce steer because the only
# hook available was RunHooks.on_tool_start, which can observe and raise
# but cannot substitute the tool's output. The Tool Guardrails API
# (agents.tool_guardrails, available since openai-agents 0.7.x) closes
# that gap: an input guardrail's RejectContentBehavior returns a message
# that the runtime substitutes in place of the tool's natural output,
# without the tool body running. That's exactly the steer semantic.
#
# We expose two entry points:
#
#   strathon_tool_guardrail(client) -> ToolInputGuardrail
#       Returns a guardrail instance for users who want fine-grained
#       per-tool attachment.
#
#   attach_strathon_guardrails(agent, client) -> int
#       Convenience: walks agent.tools, attaches the guardrail to every
#       FunctionTool. Returns the number of tools updated. Idempotent —
#       a tool already carrying our guardrail is skipped.
#
# Block is still enforced via the existing RunHooks path in
# _install_policy_patch (zero-code-change for users who call instrument).
# Users who want steer call one of the two functions below.


def _build_strathon_guardrail_function(client):
    """Construct the async guardrail callback closed over a Strathon client.

    Returns a callable suitable for ToolInputGuardrail(guardrail_function=...).
    """
    from agents.tool_guardrails import ToolGuardrailFunctionOutput

    async def _guardrail(data) -> ToolGuardrailFunctionOutput:
        # data: ToolInputGuardrailData. We need the tool name and the
        # raw arguments string the model produced.
        tool_context = getattr(data, "context", None)
        tool_call = getattr(tool_context, "tool_call", None) if tool_context else None
        tool_name = _safe_str(getattr(tool_call, "name", "tool")) if tool_call else "tool"
        raw_args = getattr(tool_call, "arguments", None) if tool_call else None

        enforcer = getattr(client, "_policy_enforcer", None)
        if enforcer is None:
            return ToolGuardrailFunctionOutput.allow()

        attrs = {
            "strathon.framework": "agents",
            "gen_ai.tool.name": tool_name,
            "strathon.tool.name": tool_name,
            "strathon.tool.args": _truncate(_safe_str(raw_args), 1500),
        }

        # Halt check first: an operator kill-switch overrides any policy.
        try:
            halt_decision = client.check_halt({
                "name": f"agents.tool.{tool_name}", "attrs": attrs,
            })
        except Exception:
            logger.exception("Strathon guardrail halt check raised; continuing")
            halt_decision = None
        if halt_decision is not None and halt_decision.is_halt:
            return ToolGuardrailFunctionOutput.raise_exception(
                output_info={
                    "halt_id": halt_decision.halt_id,
                    "scope": halt_decision.scope,
                    "reason": halt_decision.reason,
                    "halted": True,
                },
            )

        try:
            decision = client.check_policy({
                "name": f"agents.tool.{tool_name}",
                "attrs": attrs,
            })
        except Exception:
            # Policy lookup failures must NEVER break the user's tool.
            logger.exception("Strathon guardrail policy check raised; allowing")
            return ToolGuardrailFunctionOutput.allow()

        if decision.is_block:
            # raise_exception halts the run; user-level except handlers
            # see ToolGuardrailTripwireTriggered. Strathon's block
            # semantic is "halt this tool call" so this matches.
            return ToolGuardrailFunctionOutput.raise_exception(
                output_info={
                    "policy_id": decision.policy_id,
                    "policy_name": decision.policy_name,
                    "message": decision.message,
                },
            )

        if decision.is_throttle:
            # Same OAI primitive as block — raise_exception halts the
            # specific tool call. The output_info distinguishes the
            # cause so caller code that wants backoff-retry can branch.
            return ToolGuardrailFunctionOutput.raise_exception(
                output_info={
                    "policy_id": decision.policy_id,
                    "policy_name": decision.policy_name,
                    "message": decision.message,
                    "throttled": True,
                    "retry_after_seconds": decision.retry_after_seconds,
                },
            )

        if decision.is_steer:
            # reject_content substitutes the message for the tool's
            # output without running the body. Exactly the steer
            # semantic.
            replacement = decision.replacement or (
                f"[Strathon: tool '{tool_name}' redirected by policy"
                + (f" '{decision.policy_name}'" if decision.policy_name else "")
                + "]"
            )
            return ToolGuardrailFunctionOutput.reject_content(
                message=replacement,
                output_info={
                    "policy_id": decision.policy_id,
                    "policy_name": decision.policy_name,
                },
            )

        if decision.is_require_approval:
            # The guardrail is async, so we do REAL interactive approval:
            # wait for the operator off the event loop. Approved -> allow the
            # tool to run; denied/expired/timed out -> raise_exception so the
            # tool body never runs.
            from strathon.policy import await_for_approval
            try:
                await await_for_approval(
                    client, decision,
                    {"name": f"agents.tool.{tool_name}", "attrs": attrs},
                    on_timeout="deny",
                )
            except Exception as approval_exc:
                status = getattr(approval_exc, "status", "denied")
                return ToolGuardrailFunctionOutput.raise_exception(
                    output_info={
                        "policy_id": decision.policy_id,
                        "policy_name": decision.policy_name,
                        "message": str(approval_exc) or "approval denied",
                        "approval_status": status,
                    },
                )
            return ToolGuardrailFunctionOutput.allow()

        return ToolGuardrailFunctionOutput.allow()

    return _guardrail


def strathon_tool_guardrail(client):
    """Build a ToolInputGuardrail enforcing Strathon block + steer policies.

    Attach to a FunctionTool via:

        from agents import function_tool
        from strathon.instrumentation.openai_agents import strathon_tool_guardrail

        @function_tool
        def send_email(to: str, body: str) -> str:
            ...

        send_email.tool_input_guardrails = [strathon_tool_guardrail(client)]

    For attaching to every tool on an agent in one call, use
    ``attach_strathon_guardrails(agent, client)`` instead.
    """
    try:
        from agents.tool_guardrails import ToolInputGuardrail
    except ImportError as exc:  # pragma: no cover - openai-agents not installed
        raise RuntimeError(
            "openai-agents is not installed; pip install 'strathon[openai-agents]' "
            "to use Strathon's tool guardrails."
        ) from exc

    return ToolInputGuardrail(
        guardrail_function=_build_strathon_guardrail_function(client),
        name="strathon_policy",
    )


def attach_strathon_guardrails(agent, client) -> int:
    """Attach a Strathon guardrail to every FunctionTool on ``agent``.

    Mutates ``agent.tools[*].tool_input_guardrails`` in place. Skips
    non-FunctionTool entries (hosted tools, HostedMCPTool, etc., which
    don't run through the guardrail pipeline) and skips any FunctionTool
    that already has a Strathon guardrail attached. Idempotent.

    Returns the number of tools updated.
    """
    try:
        from agents.tool import FunctionTool
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "openai-agents is not installed; pip install 'strathon[openai-agents]' "
            "to attach Strathon guardrails."
        ) from exc

    guardrail = strathon_tool_guardrail(client)
    updated = 0
    tools = list(getattr(agent, "tools", None) or [])
    for tool in tools:
        if not isinstance(tool, FunctionTool):
            continue
        existing = list(tool.tool_input_guardrails or [])
        if any(getattr(g, "name", None) == "strathon_policy" for g in existing):
            # Already attached; idempotent skip
            continue
        existing.append(guardrail)
        tool.tool_input_guardrails = existing
        updated += 1

    logger.info(
        "Strathon guardrails attached to %d function tool(s) on agent %r",
        updated, getattr(agent, "name", "<unnamed>"),
    )
    return updated
