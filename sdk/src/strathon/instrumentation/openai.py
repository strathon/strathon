"""OpenAI SDK instrumentation for Strathon.

Monkey-patches ``openai.chat.completions.create`` (sync and async)
and ``openai.responses.create`` (sync and async) to emit
OpenTelemetry spans with gen_ai.* attributes for every LLM call.
Captures model, tokens, messages, and streaming responses.

Both the Chat Completions API and the Responses API (primary since
2025) are patched. The Assistants API sunsets August 26, 2026.

This instruments the raw OpenAI Python SDK (``pip install openai``),
not the OpenAI Agents SDK which has its own integration at
``strathon.instrumentation.openai_agents``.

Patch strategy: wrap the create method with a context-manager span
that records inputs on entry and outputs + token usage on exit.
Streaming responses are wrapped to accumulate chunks and finalize
the span when the stream is exhausted.
"""

from __future__ import annotations

import functools
import json
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


def _extract_usage(response) -> Dict[str, Any]:
    """Extract token usage from a ChatCompletion response."""
    attrs: Dict[str, Any] = {}
    usage = getattr(response, "usage", None)
    if usage is None:
        return attrs
    if hasattr(usage, "prompt_tokens") and usage.prompt_tokens is not None:
        attrs["gen_ai.usage.input_tokens"] = usage.prompt_tokens
    if hasattr(usage, "completion_tokens") and usage.completion_tokens is not None:
        attrs["gen_ai.usage.output_tokens"] = usage.completion_tokens
    if hasattr(usage, "total_tokens") and usage.total_tokens is not None:
        attrs["gen_ai.usage.total_tokens"] = usage.total_tokens
    return attrs


def _request_attrs(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Build span attributes from the create() kwargs."""
    attrs: Dict[str, Any] = {
        "strathon.framework": "openai",
        "gen_ai.provider.name": "openai",
        "gen_ai.operation.name": "chat",
    }
    model = kwargs.get("model")
    if model:
        attrs["gen_ai.request.model"] = str(model)
    messages = kwargs.get("messages")
    if messages:
        attrs["gen_ai.prompt"] = _truncate(json.dumps(messages, default=str))
    temperature = kwargs.get("temperature")
    if temperature is not None:
        attrs["gen_ai.request.temperature"] = float(temperature)
    max_tokens = kwargs.get("max_tokens") or kwargs.get("max_completion_tokens")
    if max_tokens is not None:
        attrs["gen_ai.request.max_tokens"] = int(max_tokens)
    tools = kwargs.get("tools")
    if tools:
        tool_names = [
            t.get("function", {}).get("name", "unknown")
            for t in tools
            if isinstance(t, dict)
        ]
        if tool_names:
            attrs["gen_ai.request.tool_names"] = ",".join(tool_names)
    return attrs


def _response_attrs(response) -> Dict[str, Any]:
    """Build span attributes from the response."""
    attrs: Dict[str, Any] = {}
    model = getattr(response, "model", None)
    if model:
        attrs["gen_ai.response.model"] = str(model)
    resp_id = getattr(response, "id", None)
    if resp_id:
        attrs["gen_ai.response.id"] = str(resp_id)
    # Extract first choice content for logging.
    choices = getattr(response, "choices", None)
    if choices and len(choices) > 0:
        choice = choices[0]
        message = getattr(choice, "message", None)
        if message:
            content = getattr(message, "content", None)
            if content:
                attrs["gen_ai.completion"] = _truncate(content)
            # Tool calls in the response.
            tool_calls = getattr(message, "tool_calls", None)
            if tool_calls:
                names = []
                for tc in tool_calls:
                    fn = getattr(tc, "function", None)
                    if fn:
                        names.append(getattr(fn, "name", "unknown"))
                if names:
                    attrs["gen_ai.response.tool_calls"] = ",".join(names)
        finish_reason = getattr(choice, "finish_reason", None)
        if finish_reason:
            attrs["gen_ai.response.finish_reason"] = str(finish_reason)
    attrs.update(_extract_usage(response))
    return attrs


class _StreamWrapper:
    """Wraps a streaming response to finalize the OTel span on exhaustion."""

    def __init__(self, stream, span, tracer):
        self._stream = stream
        self._span = span
        self._chunks: list[Any] = []
        self._total_content = ""

    def __iter__(self):
        return self

    def __next__(self):
        try:
            chunk = next(self._stream)
            self._process_chunk(chunk)
            return chunk
        except StopIteration:
            self._finalize()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._finalize()
        # Propagate to underlying stream's __exit__ if it has one.
        if hasattr(self._stream, "__exit__"):
            return self._stream.__exit__(*args)
        return False

    def _process_chunk(self, chunk):
        self._chunks.append(chunk)
        choices = getattr(chunk, "choices", None)
        if choices and len(choices) > 0:
            delta = getattr(choices[0], "delta", None)
            if delta:
                content = getattr(delta, "content", None)
                if content:
                    self._total_content += content

    def _finalize(self):
        if self._span.is_recording():
            if self._total_content:
                self._span.set_attribute(
                    "gen_ai.completion",
                    _truncate(self._total_content),
                )
            # Last chunk may have usage for stream_options=include_usage.
            if self._chunks:
                last = self._chunks[-1]
                for k, v in _extract_usage(last).items():
                    self._span.set_attribute(k, v)
                model = getattr(last, "model", None)
                if model:
                    self._span.set_attribute("gen_ai.response.model", str(model))
            self._span.set_status(Status(StatusCode.OK))
            self._span.end()


class _AsyncStreamWrapper:
    """Async version of _StreamWrapper."""

    def __init__(self, stream, span, tracer):
        self._stream = stream
        self._span = span
        self._total_content = ""
        self._chunks: list[Any] = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            chunk = await self._stream.__anext__()
            self._process_chunk(chunk)
            return chunk
        except StopAsyncIteration:
            self._finalize()
            raise

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self._finalize()
        if hasattr(self._stream, "__aexit__"):
            return await self._stream.__aexit__(*args)
        return False

    def _process_chunk(self, chunk):
        self._chunks.append(chunk)
        choices = getattr(chunk, "choices", None)
        if choices and len(choices) > 0:
            delta = getattr(choices[0], "delta", None)
            if delta:
                content = getattr(delta, "content", None)
                if content:
                    self._total_content += content

    def _finalize(self):
        if self._span.is_recording():
            if self._total_content:
                self._span.set_attribute(
                    "gen_ai.completion",
                    _truncate(self._total_content),
                )
            if self._chunks:
                last = self._chunks[-1]
                for k, v in _extract_usage(last).items():
                    self._span.set_attribute(k, v)
                model = getattr(last, "model", None)
                if model:
                    self._span.set_attribute("gen_ai.response.model", str(model))
            self._span.set_status(Status(StatusCode.OK))
            self._span.end()


def _make_sync_wrapper(original, tracer):
    """Build the sync monkey-patch wrapper for create()."""

    @functools.wraps(original)
    def wrapper(*args, **kwargs):
        span_attrs = _request_attrs(kwargs)
        span = tracer.start_span(
            name=f"openai.chat.{kwargs.get('model', 'unknown')}",
            attributes=span_attrs,
        )
        try:
            response = original(*args, **kwargs)
        except Exception as exc:
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.end()
            raise

        # Streaming: wrap the response iterator.
        if kwargs.get("stream"):
            return _StreamWrapper(response, span, tracer)

        # Non-streaming: finalize the span immediately.
        for k, v in _response_attrs(response).items():
            span.set_attribute(k, v)
        span.set_status(Status(StatusCode.OK))
        span.end()
        return response

    return wrapper


def _make_async_wrapper(original, tracer):
    """Build the async monkey-patch wrapper for acreate() / async create()."""

    @functools.wraps(original)
    async def wrapper(*args, **kwargs):
        span_attrs = _request_attrs(kwargs)
        span = tracer.start_span(
            name=f"openai.chat.{kwargs.get('model', 'unknown')}",
            attributes=span_attrs,
        )
        try:
            response = await original(*args, **kwargs)
        except Exception as exc:
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.end()
            raise

        if kwargs.get("stream"):
            return _AsyncStreamWrapper(response, span, tracer)

        for k, v in _response_attrs(response).items():
            span.set_attribute(k, v)
        span.set_status(Status(StatusCode.OK))
        span.end()
        return response

    return wrapper


def instrument(client) -> bool:
    """Instrument the OpenAI Python SDK for trace capture.

    Monkey-patches ``openai.resources.chat.completions.Completions.create``
    and the async variant, plus ``openai.resources.responses.Responses.create``
    and its async variant, to emit OpenTelemetry spans for every
    chat completion and response call.

    The Responses API (responses.create) is now OpenAI's primary surface
    as of 2025; the Assistants API sunsets August 26, 2026. Both paths
    are patched to ensure full coverage.

    Args:
        client: Strathon Client instance.

    Returns:
        True if instrumentation was successful, False if OpenAI
        is not installed.
    """
    global _PATCHED
    try:
        import openai  # noqa: F401
        from openai.resources.chat.completions import (
            AsyncCompletions,
            Completions,
        )
    except ImportError:
        logger.debug("OpenAI not installed; skipping instrumentation")
        return False

    if _PATCHED:
        logger.debug("OpenAI already instrumented; skipping")
        return True

    tracer = client.tracer

    # Sync chat completions patch.
    original_create = Completions.create
    Completions.create = _make_sync_wrapper(original_create, tracer)

    # Async chat completions patch.
    original_async_create = AsyncCompletions.create
    AsyncCompletions.create = _make_async_wrapper(original_async_create, tracer)

    # Responses API patch (primary surface since 2025).
    try:
        from openai.resources.responses import (
            AsyncResponses,
            Responses,
        )
        original_responses_create = Responses.create
        Responses.create = _make_sync_wrapper(original_responses_create, tracer)
        original_async_responses_create = AsyncResponses.create
        AsyncResponses.create = _make_async_wrapper(
            original_async_responses_create, tracer
        )
        logger.debug("OpenAI Responses API instrumentation registered")
    except (ImportError, AttributeError):
        # Older OpenAI SDK versions may not have responses module.
        logger.debug(
            "OpenAI Responses API not available; "
            "only chat.completions instrumented"
        )

    _PATCHED = True
    logger.info("OpenAI instrumentation registered")
    return True
