"""Tests for Pydantic AI instrumentation.

Covers:
- Pure helper functions (_truncate, _provider_from_model, _tool_span_attrs,
  _model_request_attrs)
- StrathonFirewall.before_tool_execute policy enforcement:
  block raises SkipToolExecution, steer raises SkipToolExecution with
  replacement, throttle raises SkipToolExecution, allow passes through
- StrathonFirewall.wrap_tool_execute span emission
- StrathonFirewall model request hooks (before/after/error)
- instrument(client) returns True when pydantic-ai is installed
- create_firewall(client) returns a capability instance
- Graceful degradation when pydantic-ai is not installed

These tests mock the Pydantic AI classes rather than importing them
directly, so they run in CI without pydantic-ai installed.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from strathon.instrumentation.pydantic_ai import (
    _json_or_str,
    _model_request_attrs,
    _provider_from_model,
    _tool_span_attrs,
    _truncate,
    instrument,
)


# ---------------------------------------------------------------------------
# Pure helper tests
# ---------------------------------------------------------------------------


class TestTruncate:
    def test_short_string_unchanged(self):
        assert _truncate("hello") == "hello"

    def test_long_string_truncated(self):
        result = _truncate("x" * 3000)
        assert len(result) < 3000
        assert "truncated" in result

    def test_none_returns_empty(self):
        assert _truncate(None) == ""

    def test_custom_max_len(self):
        result = _truncate("abcdefghij", max_len=5)
        assert len(result) <= 30  # 5 + truncation notice
        assert "truncated" in result


class TestProviderFromModel:
    def test_pydantic_ai_style_colon_prefix(self):
        assert _provider_from_model("openai:gpt-4o") == "openai"
        assert _provider_from_model("anthropic:claude-sonnet-4-6") == "anthropic"
        assert _provider_from_model("google:gemini-2.0-pro") == "google"

    def test_heuristic_openai(self):
        assert _provider_from_model("gpt-4o") == "openai"
        assert _provider_from_model("o1-preview") == "openai"

    def test_heuristic_anthropic(self):
        assert _provider_from_model("claude-3-5-sonnet") == "anthropic"

    def test_heuristic_google(self):
        assert _provider_from_model("gemini-2.0-pro") == "google"

    def test_none_and_empty(self):
        assert _provider_from_model(None) is None
        assert _provider_from_model("") is None

    def test_unknown(self):
        assert _provider_from_model("custom-model") is None


class TestToolSpanAttrs:
    def test_basic_attrs(self):
        attrs = _tool_span_attrs("search", {"query": "test"})
        assert attrs["strathon.framework"] == "pydantic_ai"
        assert attrs["gen_ai.tool.name"] == "search"
        assert attrs["strathon.tool.name"] == "search"
        assert "strathon.tool.args" in attrs

    def test_none_args(self):
        attrs = _tool_span_attrs("search", None)
        assert "strathon.tool.args" not in attrs


class TestModelRequestAttrs:
    def test_with_model(self):
        attrs = _model_request_attrs("openai:gpt-4o", 5)
        assert attrs["gen_ai.request.model"] == "openai:gpt-4o"
        assert attrs["gen_ai.provider.name"] == "openai"
        assert attrs["gen_ai.prompt.message_count"] == 5

    def test_without_model(self):
        attrs = _model_request_attrs(None, 0)
        assert "gen_ai.request.model" not in attrs
        assert "gen_ai.provider.name" not in attrs


class TestJsonOrStr:
    def test_dict(self):
        assert _json_or_str({"a": 1}) == '{"a": 1}'

    def test_list(self):
        assert _json_or_str([1, 2]) == "[1, 2]"

    def test_none(self):
        assert _json_or_str(None) == ""

    def test_string(self):
        assert _json_or_str("hello") == "hello"


# ---------------------------------------------------------------------------
# instrument() tests
# ---------------------------------------------------------------------------


class TestInstrument:
    def test_returns_bool(self):
        client = MagicMock()
        result = instrument(client)
        assert isinstance(result, bool)

    def test_returns_false_when_pydantic_ai_not_installed(self):
        """When _get_firewall_class returns None, instrument returns False."""
        client = MagicMock()
        with patch(
            "strathon.instrumentation.pydantic_ai._get_firewall_class",
            return_value=None,
        ):
            assert instrument(client) is False


# ---------------------------------------------------------------------------
# StrathonFirewall capability tests
# ---------------------------------------------------------------------------
# These tests construct the capability class directly if pydantic-ai is
# available, or skip otherwise.


def _try_import_firewall():
    """Try to build the StrathonFirewall class. Skip test if unavailable."""
    from strathon.instrumentation.pydantic_ai import _get_firewall_class

    cls = _get_firewall_class()
    if cls is None:
        pytest.skip("pydantic-ai not installed or too old for capabilities")
    return cls


class TestStrathonFirewallBeforeToolExecute:
    """Test policy enforcement in before_tool_execute."""

    def _make_firewall(self, *, block=False, steer=False, throttle=False,
                       message=None, replacement=None, policy_name=None):
        cls = _try_import_firewall()
        client = MagicMock()

        decision = MagicMock()
        decision.is_block = block
        decision.is_steer = steer
        decision.is_throttle = throttle
        decision.message = message
        decision.replacement = replacement
        decision.policy_id = "pol_123"
        decision.policy_name = policy_name or "test-policy"
        decision.retry_after_seconds = 30 if throttle else None

        client.check_policy.return_value = decision
        client._policy_enforcer = MagicMock()
        client.tracer = MagicMock()

        fw = cls(client=client)
        return fw, client

    def _make_mocks(self):
        ctx = MagicMock()
        call = MagicMock()
        call.tool_name = "search_web"
        tool_def = MagicMock()
        tool_def.name = "search_web"
        args = {"query": "test"}
        return ctx, call, tool_def, args

    def test_allow_passes_args_through(self):
        fw, _ = self._make_firewall()
        ctx, call, tool_def, args = self._make_mocks()
        result = fw.before_tool_execute(ctx, call=call, tool_def=tool_def, args=args)
        assert result == args

    def test_block_raises_skip_tool_execution(self):
        from pydantic_ai.exceptions import SkipToolExecution

        fw, _ = self._make_firewall(block=True, message="Blocked by policy")
        ctx, call, tool_def, args = self._make_mocks()

        with pytest.raises(SkipToolExecution) as exc_info:
            fw.before_tool_execute(ctx, call=call, tool_def=tool_def, args=args)
        assert "Blocked by policy" in str(exc_info.value.result)

    def test_block_uses_default_message_when_none(self):
        from pydantic_ai.exceptions import SkipToolExecution

        fw, _ = self._make_firewall(block=True, message=None)
        ctx, call, tool_def, args = self._make_mocks()

        with pytest.raises(SkipToolExecution):
            fw.before_tool_execute(ctx, call=call, tool_def=tool_def, args=args)

    def test_steer_raises_skip_with_replacement(self):
        from pydantic_ai.exceptions import SkipToolExecution

        fw, _ = self._make_firewall(
            steer=True, replacement="Use safe_search instead"
        )
        ctx, call, tool_def, args = self._make_mocks()

        with pytest.raises(SkipToolExecution) as exc_info:
            fw.before_tool_execute(ctx, call=call, tool_def=tool_def, args=args)
        assert "Use safe_search instead" in str(exc_info.value.result)

    def test_throttle_raises_skip(self):
        from pydantic_ai.exceptions import SkipToolExecution

        fw, _ = self._make_firewall(throttle=True)
        ctx, call, tool_def, args = self._make_mocks()

        with pytest.raises(SkipToolExecution):
            fw.before_tool_execute(ctx, call=call, tool_def=tool_def, args=args)

    def test_no_client_passes_through(self):
        cls = _try_import_firewall()
        fw = cls(client=None)
        ctx, call, tool_def, args = self._make_mocks()
        result = fw.before_tool_execute(ctx, call=call, tool_def=tool_def, args=args)
        assert result == args

    def test_no_policy_enforcer_passes_through(self):
        cls = _try_import_firewall()
        client = MagicMock()
        client._policy_enforcer = None
        fw = cls(client=client)
        ctx, call, tool_def, args = self._make_mocks()
        result = fw.before_tool_execute(ctx, call=call, tool_def=tool_def, args=args)
        assert result == args

    def test_policy_check_exception_allows_tool(self):
        cls = _try_import_firewall()
        client = MagicMock()
        client._policy_enforcer = MagicMock()
        client.check_policy.side_effect = RuntimeError("Policy service down")
        fw = cls(client=client)
        ctx, call, tool_def, args = self._make_mocks()
        # Should not raise — fail-open on policy check errors.
        result = fw.before_tool_execute(ctx, call=call, tool_def=tool_def, args=args)
        assert result == args


class TestStrathonFirewallWrapToolExecute:
    """Test span emission in wrap_tool_execute."""

    def test_emits_span_on_success(self):
        cls = _try_import_firewall()
        client = MagicMock()
        client._policy_enforcer = None
        mock_span = MagicMock()
        client.tracer.start_span.return_value = mock_span

        fw = cls(client=client)
        ctx = MagicMock()
        call = MagicMock()
        call.tool_name = "calculator"
        tool_def = MagicMock()
        tool_def.name = "calculator"

        async def handler(args):
            return "42"

        result = asyncio.run(
            fw.wrap_tool_execute(
                ctx, call=call, tool_def=tool_def, args={"expr": "6*7"},
                handler=handler,
            )
        )
        assert result == "42"
        client.tracer.start_span.assert_called_once()
        mock_span.set_status.assert_called()
        mock_span.end.assert_called_once()

    def test_emits_error_span_on_exception(self):
        cls = _try_import_firewall()
        client = MagicMock()
        client._policy_enforcer = None
        mock_span = MagicMock()
        client.tracer.start_span.return_value = mock_span

        fw = cls(client=client)
        ctx = MagicMock()
        call = MagicMock()
        call.tool_name = "fail_tool"
        tool_def = MagicMock()
        tool_def.name = "fail_tool"

        async def handler(args):
            raise ValueError("bad input")

        with pytest.raises(ValueError, match="bad input"):
            asyncio.run(
                fw.wrap_tool_execute(
                    ctx, call=call, tool_def=tool_def, args={},
                    handler=handler,
                )
            )
        mock_span.set_status.assert_called()
        mock_span.end.assert_called_once()

    def test_no_client_passes_through(self):
        cls = _try_import_firewall()
        fw = cls(client=None)
        ctx = MagicMock()
        call = MagicMock()
        tool_def = MagicMock()
        tool_def.name = "tool"

        async def handler(args):
            return "result"

        result = asyncio.run(
            fw.wrap_tool_execute(
                ctx, call=call, tool_def=tool_def, args={},
                handler=handler,
            )
        )
        assert result == "result"


class TestStrathonFirewallModelHooks:
    """Test model request/response span hooks."""

    def test_before_and_after_model_request(self):
        cls = _try_import_firewall()
        client = MagicMock()
        mock_span = MagicMock()
        mock_span.is_recording.return_value = True
        client.tracer.start_span.return_value = mock_span

        fw = cls(client=client)
        ctx = MagicMock()

        # Simulate request_context.
        request_context = MagicMock()
        request_context.model = MagicMock()
        request_context.model.model_name = "openai:gpt-4o"
        request_context.messages = [MagicMock(), MagicMock()]

        # before_model_request should stash a span.
        result_ctx = fw.before_model_request(ctx, request_context)
        assert result_ctx is request_context
        client.tracer.start_span.assert_called_once()
        assert len(fw._active_model_spans) == 1

        # Simulate response.
        response = MagicMock()
        response.model_name = "gpt-4o-2025-05-13"
        response.usage = MagicMock()
        response.usage.request_tokens = 100
        response.usage.response_tokens = 50
        response.usage.input_tokens = None
        response.usage.output_tokens = None
        response.usage.total_tokens = 150

        result_resp = fw.after_model_request(ctx, response)
        assert result_resp is response
        mock_span.end.assert_called_once()
        assert len(fw._active_model_spans) == 0

    def test_model_request_error_ends_span(self):
        cls = _try_import_firewall()
        client = MagicMock()
        mock_span = MagicMock()
        client.tracer.start_span.return_value = mock_span

        fw = cls(client=client)
        ctx = MagicMock()

        request_context = MagicMock()
        request_context.model = MagicMock()
        request_context.model.model_name = "openai:gpt-4o"
        request_context.messages = []

        fw.before_model_request(ctx, request_context)
        assert len(fw._active_model_spans) == 1

        error = RuntimeError("API timeout")
        with pytest.raises(RuntimeError, match="API timeout"):
            fw.on_model_request_error(ctx, error)
        mock_span.end.assert_called_once()
        assert len(fw._active_model_spans) == 0

    def test_no_client_noop(self):
        cls = _try_import_firewall()
        fw = cls(client=None)
        ctx = MagicMock()
        request_context = MagicMock()

        result = fw.before_model_request(ctx, request_context)
        assert result is request_context


class TestCreateFirewall:
    def test_creates_instance(self):
        _try_import_firewall()  # skip if pydantic-ai unavailable
        from strathon.instrumentation.pydantic_ai import create_firewall

        client = MagicMock()
        fw = create_firewall(client)
        assert fw is not None
        assert fw.client is client

    def test_raises_when_pydantic_ai_missing(self):
        from strathon.instrumentation.pydantic_ai import create_firewall

        with patch(
            "strathon.instrumentation.pydantic_ai._get_firewall_class",
            return_value=None,
        ):
            with pytest.raises(ImportError, match="pydantic-ai"):
                create_firewall(MagicMock())
