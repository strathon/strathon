"""Anthropic SDK instrumentation for Strathon.

Monkey-patches ``anthropic.messages.create`` (sync and async)
to emit OpenTelemetry spans with gen_ai.* attributes for every
Messages API call. Captures model, tokens, messages, tool use,
and streaming responses.

This instruments the raw Anthropic Python SDK
(``pip install anthropic``), not the Claude Agent SDK which has
its own integration at ``strathon.instrumentation.claude_agent``.

Patch strategy: identical to the OpenAI instrumentation —
wrap the create method with a context-manager span that records
inputs on entry and outputs + token usage on exit. Streaming
responses are wrapped to accumulate events and finalize the span
when the stream completes.
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


def _request_attrs(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Build span attributes from the create() kwargs."""
    attrs: Dict[str, Any] = {
        "strathon.framework": "anthropic",
        "gen_ai.provider.name": "anthropic",
        "gen_ai.operation.name": "chat",
    }
    model = kwargs.get("model")
    if model:
        attrs["gen_ai.request.model"] = str(model)
    max_tokens = kwargs.get("max_tokens")
    if max_tokens is not None:
        attrs["gen_ai.request.max_tokens"] = int(max_tokens)
    temperature = kwargs.get("temperature")
    if temperature is not None:
        attrs["gen_ai.request.temperature"] = float(temperature)
    messages = kwargs.get("messages")
    if messages:
        attrs["gen_ai.prompt"] = _truncate(json.dumps(messages, default=str))
    system = kwargs.get("system")
    if system:
        attrs["gen_ai.request.system"] = _truncate(str(system))
    tools = kwargs.get("tools")
    if tools:
        tool_names = [
            t.get("name", "unknown")
            for t in tools
            if isinstance(t, dict)
        ]
        if tool_names:
            attrs["gen_ai.request.tool_names"] = ",".join(tool_names)
    return attrs


def _response_attrs(response) -> Dict[str, Any]:
    """Build span attributes from the Message response."""
    attrs: Dict[str, Any] = {}
    model = getattr(response, "model", None)
    if model:
        attrs["gen_ai.response.model"] = str(model)
    resp_id = getattr(response, "id", None)
    if resp_id:
        attrs["gen_ai.response.id"] = str(resp_id)
    stop_reason = getattr(response, "stop_reason", None)
    if stop_reason:
        attrs["gen_ai.response.finish_reason"] = str(stop_reason)
    # Usage.
    usage = getattr(response, "usage", None)
    if usage:
        input_tokens = getattr(usage, "input_tokens", None)
        if input_tokens is not None:
            attrs["gen_ai.usage.input_tokens"] = input_tokens
        output_tokens = getattr(usage, "output_tokens", None)
        if output_tokens is not None:
            attrs["gen_ai.usage.output_tokens"] = output_tokens
        cache_read = getattr(usage, "cache_read_input_tokens", None)
        if cache_read is not None:
            attrs["gen_ai.usage.cache_read.input_tokens"] = cache_read
        cache_creation = getattr(usage, "cache_creation_input_tokens", None)
        if cache_creation is not None:
            attrs["gen_ai.usage.cache_creation.input_tokens"] = cache_creation
    # Content.
    content = getattr(response, "content", None)
    if content:
        text_parts = []
        tool_names = []
        for block in content:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                text_parts.append(getattr(block, "text", ""))
            elif block_type == "tool_use":
                tool_names.append(getattr(block, "name", "unknown"))
        if text_parts:
            attrs["gen_ai.completion"] = _truncate("\n".join(text_parts))
        if tool_names:
            attrs["gen_ai.response.tool_calls"] = ",".join(tool_names)
    return attrs


class _StreamWrapper:
    """Wraps a sync streaming response to finalize the span on close."""

    def __init__(self, stream, span):
        self._stream = stream
        self._span = span
        self._text = ""
        self._usage_attrs: Dict[str, Any] = {}

    def __iter__(self):
        return self

    def __next__(self):
        try:
            event = next(self._stream)
            self._process_event(event)
            return event
        except StopIteration:
            self._finalize()
            raise

    def __enter__(self):
        if hasattr(self._stream, "__enter__"):
            self._stream.__enter__()
        return self

    def __exit__(self, *args):
        self._finalize()
        if hasattr(self._stream, "__exit__"):
            return self._stream.__exit__(*args)
        return False

    def _process_event(self, event):
        event_type = getattr(event, "type", None)
        if event_type == "content_block_delta":
            delta = getattr(event, "delta", None)
            if delta and getattr(delta, "type", None) == "text_delta":
                self._text += getattr(delta, "text", "")
        elif event_type == "message_delta":
            usage = getattr(event, "usage", None)
            if usage:
                output_tokens = getattr(usage, "output_tokens", None)
                if output_tokens is not None:
                    self._usage_attrs["gen_ai.usage.output_tokens"] = output_tokens
        elif event_type == "message_start":
            message = getattr(event, "message", None)
            if message:
                model = getattr(message, "model", None)
                if model:
                    self._usage_attrs["gen_ai.response.model"] = str(model)
                usage = getattr(message, "usage", None)
                if usage:
                    input_tokens = getattr(usage, "input_tokens", None)
                    if input_tokens is not None:
                        self._usage_attrs["gen_ai.usage.input_tokens"] = input_tokens

    def _finalize(self):
        if self._span.is_recording():
            if self._text:
                self._span.set_attribute(
                    "gen_ai.completion", _truncate(self._text)
                )
            for k, v in self._usage_attrs.items():
                self._span.set_attribute(k, v)
            self._span.set_status(Status(StatusCode.OK))
            self._span.end()


class _AsyncStreamWrapper:
    """Async version of _StreamWrapper."""

    def __init__(self, stream, span):
        self._stream = stream
        self._span = span
        self._text = ""
        self._usage_attrs: Dict[str, Any] = {}

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            event = await self._stream.__anext__()
            self._process_event(event)
            return event
        except StopAsyncIteration:
            self._finalize()
            raise

    async def __aenter__(self):
        if hasattr(self._stream, "__aenter__"):
            await self._stream.__aenter__()
        return self

    async def __aexit__(self, *args):
        self._finalize()
        if hasattr(self._stream, "__aexit__"):
            return await self._stream.__aexit__(*args)
        return False

    def _process_event(self, event):
        event_type = getattr(event, "type", None)
        if event_type == "content_block_delta":
            delta = getattr(event, "delta", None)
            if delta and getattr(delta, "type", None) == "text_delta":
                self._text += getattr(delta, "text", "")
        elif event_type == "message_delta":
            usage = getattr(event, "usage", None)
            if usage:
                output_tokens = getattr(usage, "output_tokens", None)
                if output_tokens is not None:
                    self._usage_attrs["gen_ai.usage.output_tokens"] = output_tokens
        elif event_type == "message_start":
            message = getattr(event, "message", None)
            if message:
                model = getattr(message, "model", None)
                if model:
                    self._usage_attrs["gen_ai.response.model"] = str(model)
                usage = getattr(message, "usage", None)
                if usage:
                    input_tokens = getattr(usage, "input_tokens", None)
                    if input_tokens is not None:
                        self._usage_attrs["gen_ai.usage.input_tokens"] = input_tokens

    def _finalize(self):
        if self._span.is_recording():
            if self._text:
                self._span.set_attribute(
                    "gen_ai.completion", _truncate(self._text)
                )
            for k, v in self._usage_attrs.items():
                self._span.set_attribute(k, v)
            self._span.set_status(Status(StatusCode.OK))
            self._span.end()


def _make_sync_wrapper(original, tracer):
    @functools.wraps(original)
    def wrapper(*args, **kwargs):
        span_attrs = _request_attrs(kwargs)
        span = tracer.start_span(
            name=f"anthropic.messages.{kwargs.get('model', 'unknown')}",
            attributes=span_attrs,
        )
        try:
            response = original(*args, **kwargs)
        except Exception as exc:
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.end()
            raise

        if kwargs.get("stream"):
            return _StreamWrapper(response, span)

        for k, v in _response_attrs(response).items():
            span.set_attribute(k, v)
        span.set_status(Status(StatusCode.OK))
        span.end()
        return response

    return wrapper


def _make_async_wrapper(original, tracer):
    @functools.wraps(original)
    async def wrapper(*args, **kwargs):
        span_attrs = _request_attrs(kwargs)
        span = tracer.start_span(
            name=f"anthropic.messages.{kwargs.get('model', 'unknown')}",
            attributes=span_attrs,
        )
        try:
            response = await original(*args, **kwargs)
        except Exception as exc:
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.end()
            raise

        if kwargs.get("stream"):
            return _AsyncStreamWrapper(response, span)

        for k, v in _response_attrs(response).items():
            span.set_attribute(k, v)
        span.set_status(Status(StatusCode.OK))
        span.end()
        return response

    return wrapper


def instrument(client) -> bool:
    """Instrument the Anthropic Python SDK for trace capture.

    Monkey-patches ``anthropic.resources.messages.Messages.create``
    and the async variant to emit OpenTelemetry spans for every
    Messages API call.

    Args:
        client: Strathon Client instance.

    Returns:
        True if instrumentation was successful, False if the
        Anthropic SDK is not installed.
    """
    global _PATCHED
    try:
        from anthropic.resources.messages import (  # type: ignore[import-not-found]
            AsyncMessages,
            Messages,
        )
    except ImportError:
        logger.debug("Anthropic not installed; skipping instrumentation")
        return False

    if _PATCHED:
        logger.debug("Anthropic already instrumented; skipping")
        return True

    tracer = client.tracer

    original_create = Messages.create
    Messages.create = _make_sync_wrapper(original_create, tracer)

    original_async_create = AsyncMessages.create
    AsyncMessages.create = _make_async_wrapper(original_async_create, tracer)

    _PATCHED = True
    logger.info("Anthropic instrumentation registered")
    return True
