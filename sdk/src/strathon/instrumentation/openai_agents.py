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
                attrs["strathon.tool.input"] = _truncate(str(tool_input), 2000)

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


def instrument(client) -> bool:
    """
    Register the Strathon TracingProcessor with the OpenAI Agents SDK.

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
    add_trace_processor(processor)
    logger.info("OpenAI Agents SDK instrumentation registered")
    return True
