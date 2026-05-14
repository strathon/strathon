"""LangGraph / LangChain auto-instrumentation for Strathon.

LangGraph builds on LangChain, so we hook into LangChain's standard
BaseCallbackHandler interface. Every chain (graph node), LLM call, tool
invocation, and retriever step fires a start/end callback with a UUID
run_id and an optional parent_run_id. We translate those into OpenTelemetry
spans on the Strathon Client's tracer.

This single handler instruments both pure LangChain and LangGraph apps;
LangGraph node executions surface as chain callbacks with langgraph_node
in their metadata.

Same architectural pattern as the OpenAI Agents SDK (TracingProcessor) and
CrewAI (BaseEventListener) integrations: subscribe to the framework's
emission stream, key active spans by the framework's correlation id,
translate to OTel.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Sequence
from uuid import UUID

from opentelemetry import trace as otel_trace
from opentelemetry.trace import Span as OTelSpan, Status, StatusCode

logger = logging.getLogger(__name__)

_MAX_ATTR_LEN = 2000


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
            return json.dumps(value, default=str)
        except Exception:
            return _safe_str(value)
    return _safe_str(value)


def _provider_from_model(model: Optional[str]) -> Optional[str]:
    """Best-effort provider parsing from a model name."""
    if not model:
        return None
    lower = model.lower()
    if "/" in lower:
        prefix = lower.split("/", 1)[0]
        if prefix in {"anthropic", "openai", "google", "mistral", "cohere"}:
            return prefix
    if lower.startswith("gpt") or "openai" in lower:
        return "openai"
    if lower.startswith("claude") or "anthropic" in lower:
        return "anthropic"
    if lower.startswith("gemini") or "google" in lower:
        return "google"
    if lower.startswith("mistral") or lower.startswith("mixtral"):
        return "mistral"
    return None


def _model_from_serialized(serialized: Optional[dict]) -> Optional[str]:
    """Pull the model name out of a LangChain serialized dict.

    The serialized dict typically has shape:
      {"id": ["langchain", "chat_models", "openai", "ChatOpenAI"],
       "kwargs": {"model": "gpt-4o", "temperature": 0, ...},
       "name": "ChatOpenAI"}
    """
    if not serialized:
        return None
    kwargs = serialized.get("kwargs") or {}
    # Common keys across providers
    for key in ("model", "model_name", "deployment_name"):
        val = kwargs.get(key)
        if val:
            return str(val)
    return None


def _chain_name_from_serialized(serialized: Optional[dict]) -> str:
    """Derive a human-readable chain name from the serialized dict."""
    if not serialized:
        return "chain"
    name = serialized.get("name")
    if name:
        return str(name)
    id_path = serialized.get("id")
    if isinstance(id_path, list) and id_path:
        return str(id_path[-1])
    return "chain"


def _tool_name_from_serialized(serialized: Optional[dict]) -> str:
    if not serialized:
        return "tool"
    return str(serialized.get("name") or "tool")


def _extract_token_usage_from_llm_result(response) -> Dict[str, Any]:
    """Pull token usage from a LangChain LLMResult.

    Different providers stash usage in different places:
      - OpenAI: response.llm_output["token_usage"] -> {prompt_tokens, completion_tokens, total_tokens}
      - Anthropic via langchain-anthropic: response.llm_output["usage"]
      - Some chat models put it in generations[0][0].generation_info["token_usage"]
    """
    out: Dict[str, Any] = {}

    def _consume(usage):
        if not usage:
            return
        if isinstance(usage, dict):
            pt = usage.get("prompt_tokens") or usage.get("input_tokens")
            ct = usage.get("completion_tokens") or usage.get("output_tokens")
            tt = usage.get("total_tokens")
        else:
            pt = getattr(usage, "prompt_tokens", None) or getattr(usage, "input_tokens", None)
            ct = getattr(usage, "completion_tokens", None) or getattr(usage, "output_tokens", None)
            tt = getattr(usage, "total_tokens", None)
        if pt is not None:
            try:
                out["gen_ai.usage.input_tokens"] = int(pt)
            except (TypeError, ValueError):
                pass
        if ct is not None:
            try:
                out["gen_ai.usage.output_tokens"] = int(ct)
            except (TypeError, ValueError):
                pass
        if tt is not None:
            try:
                out["gen_ai.usage.total_tokens"] = int(tt)
            except (TypeError, ValueError):
                pass

    llm_output = getattr(response, "llm_output", None)
    if isinstance(llm_output, dict):
        _consume(llm_output.get("token_usage"))
        if not out:
            _consume(llm_output.get("usage"))

    # Fallback: scan generations for per-gen usage info
    if not out:
        generations = getattr(response, "generations", None)
        if generations:
            try:
                first_batch = generations[0] if generations else []
                if first_batch:
                    gen = first_batch[0]
                    gen_info = getattr(gen, "generation_info", None) or {}
                    if isinstance(gen_info, dict):
                        _consume(gen_info.get("token_usage") or gen_info.get("usage"))
                    # Some chat models attach usage on .message.usage_metadata
                    msg = getattr(gen, "message", None)
                    if msg is not None and not out:
                        usage_meta = getattr(msg, "usage_metadata", None)
                        if usage_meta:
                            _consume(usage_meta)
            except Exception:
                pass

    return out


class StrathonLangGraphHandler:
    """
    LangChain BaseCallbackHandler that mirrors callbacks into Strathon OTel spans.

    Keys active spans by str(run_id). Looks up parent via str(parent_run_id).
    Same idea as our other integrations but using LangChain's run UUID system.

    Subclassing BaseCallbackHandler happens dynamically in instrument() so this
    module can be imported even when langchain_core is not installed.
    """

    # do NOT inherit BaseCallbackHandler here; we bind it lazily in instrument()
    ignore_chain = False
    ignore_llm = False
    ignore_agent = False
    ignore_retriever = False
    ignore_chat_model = False
    # raise_error=True tells LangChain to propagate exceptions thrown from
    # our callbacks. Critical for StrathonPolicyBlocked to actually block.
    raise_error = True
    run_inline = False

    def __init__(self, client) -> None:
        self.client = client
        self._tracer = client.tracer
        self._spans: Dict[str, OTelSpan] = {}

    # ---- Internal helpers ----

    def _start_span(
        self,
        name: str,
        run_id: UUID,
        parent_run_id: Optional[UUID],
        attrs: Dict[str, Any],
    ) -> None:
        try:
            key = str(run_id)
            parent_key = str(parent_run_id) if parent_run_id else None
            parent_span = self._spans.get(parent_key) if parent_key else None
            ctx = (
                otel_trace.set_span_in_context(parent_span)
                if parent_span is not None
                else None
            )
            base_attrs: Dict[str, Any] = {
                "strathon.framework": "langgraph",
                "langgraph.run_id": key,
            }
            base_attrs.update({k: v for k, v in attrs.items() if v is not None})
            span = self._tracer.start_span(name=name, context=ctx, attributes=base_attrs)
            self._spans[key] = span
        except Exception:
            logger.exception("StrathonLangGraphHandler: start_span failed for %s", run_id)

    def _end_span(
        self,
        run_id: UUID,
        attrs: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        key = str(run_id)
        span = self._spans.pop(key, None)
        if span is None:
            return
        try:
            if attrs:
                for k, v in attrs.items():
                    if v is not None:
                        span.set_attribute(k, v)
            if error:
                span.set_status(Status(StatusCode.ERROR, error))
            else:
                span.set_status(Status(StatusCode.OK))
        except Exception:
            logger.exception("StrathonLangGraphHandler: end_span attrs failed for %s", run_id)
        finally:
            try:
                span.end()
            except Exception:
                logger.exception("StrathonLangGraphHandler: end_span failed for %s", run_id)

    # ---- Chain callbacks (graph nodes in LangGraph) ----

    def on_chain_start(
        self,
        serialized: Optional[dict],
        inputs: Any,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        chain_name = _chain_name_from_serialized(serialized)
        attrs: Dict[str, Any] = {
            "langgraph.chain.name": chain_name,
        }
        # LangGraph metadata includes the node name explicitly
        if metadata:
            node_name = metadata.get("langgraph_node") or metadata.get("ls_node_name")
            if node_name:
                attrs["langgraph.node.name"] = _safe_str(node_name)
                attrs["strathon.agent.name"] = _safe_str(node_name)
                attrs["gen_ai.agent.name"] = _safe_str(node_name)
            step = metadata.get("langgraph_step")
            if step is not None:
                attrs["langgraph.step"] = _safe_str(step)
        if tags:
            attrs["langgraph.tags"] = ",".join(_safe_str(t) for t in tags)
        if inputs is not None:
            attrs["langgraph.chain.inputs"] = _truncate(_json_or_str(inputs), 1500)
        self._start_span(f"langgraph.chain.{chain_name}", run_id, parent_run_id, attrs)

    def on_chain_end(
        self,
        outputs: Any,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        attrs: Dict[str, Any] = {}
        if outputs is not None:
            attrs["langgraph.chain.outputs"] = _truncate(_json_or_str(outputs), 1500)
        self._end_span(run_id, attrs)

    def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        self._end_span(run_id, error=_safe_str(error))

    # ---- LLM callbacks (both legacy LLMs and chat models) ----

    def on_llm_start(
        self,
        serialized: Optional[dict],
        prompts: Sequence[str],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        model = _model_from_serialized(serialized)
        attrs: Dict[str, Any] = {
            "gen_ai.operation.name": "completion",
        }
        if model:
            attrs["gen_ai.request.model"] = model
            provider = _provider_from_model(model)
            if provider:
                attrs["gen_ai.provider.name"] = provider
        if prompts:
            joined = "\n---\n".join(_safe_str(p) for p in prompts)
            attrs["langgraph.llm.prompts"] = _truncate(joined, 2000)
        self._start_span("langgraph.llm", run_id, parent_run_id, attrs)

    def on_chat_model_start(
        self,
        serialized: Optional[dict],
        messages: Sequence[Sequence[Any]],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        model = _model_from_serialized(serialized)
        attrs: Dict[str, Any] = {
            "gen_ai.operation.name": "chat",
        }
        if model:
            attrs["gen_ai.request.model"] = model
            provider = _provider_from_model(model)
            if provider:
                attrs["gen_ai.provider.name"] = provider
        # Flatten messages for visibility (truncated)
        if messages:
            try:
                flat = []
                for batch in messages:
                    for m in batch:
                        content = getattr(m, "content", None) or _safe_str(m)
                        msg_type = getattr(m, "type", None) or type(m).__name__
                        flat.append(f"[{msg_type}] {content}")
                attrs["langgraph.llm.messages"] = _truncate("\n".join(flat), 2000)
            except Exception:
                pass
        self._start_span("langgraph.llm", run_id, parent_run_id, attrs)

    def on_llm_end(
        self,
        response: Any,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        attrs: Dict[str, Any] = {}
        attrs.update(_extract_token_usage_from_llm_result(response))
        llm_output = getattr(response, "llm_output", None)
        if isinstance(llm_output, dict):
            response_model = llm_output.get("model_name") or llm_output.get("model")
            if response_model:
                attrs["gen_ai.response.model"] = _safe_str(response_model)
        # Best-effort capture of first generation text
        try:
            generations = getattr(response, "generations", None)
            if generations and generations[0]:
                gen = generations[0][0]
                text = getattr(gen, "text", None)
                if text is None:
                    msg = getattr(gen, "message", None)
                    if msg is not None:
                        text = getattr(msg, "content", None)
                if text:
                    attrs["langgraph.llm.response"] = _truncate(_safe_str(text), 1500)
        except Exception:
            pass
        self._end_span(run_id, attrs)

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        self._end_span(run_id, error=_safe_str(error))

    def on_llm_new_token(self, *args: Any, **kwargs: Any) -> None:
        # Streaming tokens are too noisy to span individually; ignore.
        return

    # ---- Tool callbacks ----

    def on_tool_start(
        self,
        serialized: Optional[dict],
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        inputs: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        tool_name = _tool_name_from_serialized(serialized)
        # Canonical attribute across all frameworks: strathon.tool.args holds
        # the tool's input as a JSON string (or raw string fallback). Policies
        # match on this name regardless of framework.
        if inputs:
            tool_args = _truncate(_json_or_str(inputs), 1500)
        elif input_str:
            tool_args = _truncate(_safe_str(input_str), 1500)
        else:
            tool_args = ""

        attrs: Dict[str, Any] = {
            "strathon.framework": "langgraph",
            "gen_ai.tool.name": tool_name,
            "strathon.tool.name": tool_name,
            "strathon.tool.args": tool_args,
        }

        # Runtime intervention: ask the client's policy enforcer if this tool
        # call is allowed. block raises (LangChain propagates it as a tool
        # error); steer raises a special exception the user-level wrapper can
        # catch (covered in docs). For now we only support hard block here.
        try:
            decision = self.client.check_policy({
                "name": f"langgraph.tool.{tool_name}",
                "attrs": attrs,
            })
            if decision.is_block:
                # Annotate the (about-to-be-created) span with policy match info
                attrs["strathon.policy.blocked"] = True
                attrs["strathon.policy.id"] = decision.policy_id or ""
                attrs["strathon.policy.name"] = decision.policy_name or ""
                attrs["strathon.policy.message"] = decision.message or ""
                # Open and immediately close a span recording the block
                self._start_span(
                    f"langgraph.tool.{tool_name}", run_id, parent_run_id, attrs
                )
                self._end_span(run_id, error=decision.message or "policy blocked")
                # Import here to avoid circular dependency
                from strathon.policy import StrathonPolicyBlocked
                raise StrathonPolicyBlocked(
                    decision.message or "blocked by Strathon policy",
                    policy_id=decision.policy_id,
                    policy_name=decision.policy_name,
                )
            if decision.is_steer:
                attrs["strathon.policy.steered"] = True
                attrs["strathon.policy.id"] = decision.policy_id or ""
                attrs["strathon.policy.name"] = decision.policy_name or ""
                attrs["strathon.policy.replacement"] = decision.replacement or ""
                # Open and immediately close a span recording the steer.
                # We cannot return a replacement value from on_tool_start, so
                # for v0 steer mode in LangGraph users should call
                # client.check_policy() in their tool wrapper. We log the
                # decision so it's still observable.
                self._start_span(
                    f"langgraph.tool.{tool_name}", run_id, parent_run_id, attrs
                )
                self._end_span(run_id)
                return
        except Exception as exc:
            # Reraise StrathonPolicyBlocked; swallow other errors so a broken
            # policy never breaks the underlying app.
            from strathon.policy import StrathonPolicyBlocked
            if isinstance(exc, StrathonPolicyBlocked):
                raise
            logger.exception("policy check raised; allowing tool to proceed")

        self._start_span(f"langgraph.tool.{tool_name}", run_id, parent_run_id, attrs)

    def on_tool_end(
        self,
        output: Any,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        attrs: Dict[str, Any] = {}
        if output is not None:
            attrs["strathon.tool.output"] = _truncate(_safe_str(output), 1500)
        self._end_span(run_id, attrs)

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        self._end_span(run_id, error=_safe_str(error))

    # ---- Retriever callbacks ----

    def on_retriever_start(
        self,
        serialized: Optional[dict],
        query: str,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        retriever_name = _chain_name_from_serialized(serialized)
        attrs: Dict[str, Any] = {
            "strathon.retriever.name": retriever_name,
            "strathon.retriever.query": _truncate(_safe_str(query), 1000),
        }
        self._start_span(
            f"langgraph.retriever.{retriever_name}", run_id, parent_run_id, attrs
        )

    def on_retriever_end(
        self,
        documents: Any,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        attrs: Dict[str, Any] = {}
        try:
            attrs["strathon.retriever.doc_count"] = len(documents) if documents else 0
        except Exception:
            pass
        self._end_span(run_id, attrs)

    def on_retriever_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        self._end_span(run_id, error=_safe_str(error))

    # ---- Agent action / finish (informational events) ----

    def on_agent_action(
        self,
        action: Any,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        # Don't open a new span; attach an event to the parent if available
        parent_span = self._spans.get(str(parent_run_id) if parent_run_id else "")
        if parent_span is None:
            parent_span = self._spans.get(str(run_id))
        if parent_span is None:
            return
        tool = getattr(action, "tool", None)
        tool_input = getattr(action, "tool_input", None)
        attrs = {"strathon.agent.action.tool": _safe_str(tool)} if tool else {}
        if tool_input is not None:
            attrs["strathon.agent.action.input"] = _truncate(_json_or_str(tool_input), 1000)
        try:
            parent_span.add_event("agent.action", attributes=attrs)
        except Exception:
            pass

    def on_agent_finish(
        self,
        finish: Any,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        parent_span = self._spans.get(str(parent_run_id) if parent_run_id else "")
        if parent_span is None:
            parent_span = self._spans.get(str(run_id))
        if parent_span is None:
            return
        return_values = getattr(finish, "return_values", None) or {}
        attrs = {}
        if return_values:
            attrs["strathon.agent.finish.output"] = _truncate(
                _json_or_str(return_values), 1000
            )
        try:
            parent_span.add_event("agent.finish", attributes=attrs)
        except Exception:
            pass


# Module-level reference so the handler isn't garbage collected
_REGISTERED_HANDLER: Optional[StrathonLangGraphHandler] = None


def instrument(client) -> Optional["StrathonLangGraphHandler"]:
    """
    Build a Strathon LangChain/LangGraph callback handler and return it.

    Unlike CrewAI and the OpenAI Agents SDK, LangChain does not have a global
    "register this handler" call. Instead, the user passes the handler into
    each chain/graph invocation via callbacks=[handler] or RunnableConfig.

    Args:
        client: Strathon Client instance.

    Returns:
        StrathonLangGraphHandler instance bound to BaseCallbackHandler if
        langchain_core is installed; None otherwise.

    Usage:
        handler = strathon.instrument(client, frameworks=["langgraph"])
        result = graph.invoke(input, config={"callbacks": [handler]})
    """
    global _REGISTERED_HANDLER

    try:
        from langchain_core.callbacks.base import BaseCallbackHandler
    except ImportError:
        logger.debug("langchain_core not installed; skipping LangGraph instrumentation")
        return None

    if _REGISTERED_HANDLER is not None:
        # Re-bind to new client without rebuilding the subclass
        _REGISTERED_HANDLER.client = client
        _REGISTERED_HANDLER._tracer = client.tracer
        return _REGISTERED_HANDLER

    # Dynamically bind our handler class to BaseCallbackHandler
    class _BoundHandler(StrathonLangGraphHandler, BaseCallbackHandler):
        pass

    _REGISTERED_HANDLER = _BoundHandler(client)
    logger.info("LangGraph/LangChain instrumentation handler ready")
    return _REGISTERED_HANDLER
