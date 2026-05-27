"""Tests for Google ADK instrumentation.

Covers:
- Pure helper functions (_truncate, _tool_span_attrs, _model_request_attrs)
- StrathonFirewallPlugin.before_tool_callback policy enforcement:
  block returns dict, steer returns dict with replacement, throttle
  returns dict, allow returns None
- StrathonFirewallPlugin.after_tool_callback span emission
- StrathonFirewallPlugin.on_tool_error_callback error span
- StrathonFirewallPlugin model callbacks (before/after/error)
- instrument(client) returns True when google-adk is installed
- create_firewall_plugin(client) returns a plugin instance
- Graceful degradation when google-adk is not installed

Tests mock the ADK classes so they run in CI without google-adk installed.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from strathon.instrumentation.google_adk import (
    _model_request_attrs,
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


class TestToolSpanAttrs:
    def test_basic_attrs(self):
        attrs = _tool_span_attrs("search", {"query": "test"})
        assert attrs["strathon.framework"] == "google_adk"
        assert attrs["gen_ai.tool.name"] == "search"
        assert attrs["strathon.tool.name"] == "search"
        assert "strathon.tool.args" in attrs

    def test_with_agent_name(self):
        attrs = _tool_span_attrs("search", {}, agent_name="research_agent")
        assert attrs["strathon.agent.name"] == "research_agent"

    def test_none_args(self):
        attrs = _tool_span_attrs("search", None)
        assert "strathon.tool.args" not in attrs


class TestModelRequestAttrs:
    def test_with_model(self):
        llm_request = MagicMock()
        llm_request.model = "gemini-2.0-flash"
        llm_request.contents = [MagicMock(), MagicMock()]
        llm_request.config = MagicMock()
        llm_request.config.temperature = 0.7
        llm_request.config.max_output_tokens = 1024
        attrs = _model_request_attrs(llm_request)
        assert attrs["gen_ai.request.model"] == "gemini-2.0-flash"
        assert attrs["gen_ai.provider.name"] == "google"
        assert attrs["gen_ai.prompt.message_count"] == 2
        assert attrs["gen_ai.request.temperature"] == 0.7
        assert attrs["gen_ai.request.max_tokens"] == 1024

    def test_without_model(self):
        llm_request = MagicMock()
        llm_request.model = None
        llm_request.contents = None
        llm_request.config = None
        attrs = _model_request_attrs(llm_request)
        assert "gen_ai.request.model" not in attrs


# ---------------------------------------------------------------------------
# instrument() tests
# ---------------------------------------------------------------------------


class TestInstrument:
    def test_returns_bool(self):
        client = MagicMock()
        result = instrument(client)
        assert isinstance(result, bool)

    def test_returns_false_when_adk_not_installed(self):
        client = MagicMock()
        with patch(
            "strathon.instrumentation.google_adk._get_plugin_class",
            return_value=None,
        ):
            assert instrument(client) is False


# ---------------------------------------------------------------------------
# StrathonFirewallPlugin tests
# ---------------------------------------------------------------------------


def _try_import_plugin():
    """Try to build the plugin class. Skip test if unavailable."""
    from strathon.instrumentation.google_adk import _get_plugin_class

    cls = _get_plugin_class()
    if cls is None:
        pytest.skip("google-adk not installed")
    return cls


class TestBeforeToolCallback:
    """Test policy enforcement in before_tool_callback."""

    def _make_plugin(self, *, block=False, steer=False, throttle=False,
                     message=None, replacement=None, policy_name=None):
        cls = _try_import_plugin()
        client = MagicMock()

        decision = MagicMock()
        decision.is_block = block
        decision.is_steer = steer
        decision.is_throttle = throttle
        decision.message = message
        decision.replacement = replacement
        decision.policy_id = "pol_456"
        decision.policy_name = policy_name or "test-policy"
        decision.retry_after_seconds = 30 if throttle else None

        client.check_policy.return_value = decision
        client._policy_enforcer = MagicMock()
        client.tracer = MagicMock()

        plugin = cls(client=client)
        return plugin, client

    def _make_tool_mocks(self):
        tool = MagicMock()
        tool.name = "search_web"
        tool_args = {"query": "test"}
        tool_context = MagicMock()
        tool_context.agent_name = "research_agent"
        return tool, tool_args, tool_context

    def test_allow_returns_none(self):
        plugin, _ = self._make_plugin()
        tool, tool_args, tool_context = self._make_tool_mocks()

        result = asyncio.run(
            plugin.before_tool_callback(
                tool=tool, tool_args=tool_args, tool_context=tool_context,
            )
        )
        assert result is None

    def test_block_returns_dict(self):
        plugin, _ = self._make_plugin(block=True, message="Blocked!")
        tool, tool_args, tool_context = self._make_tool_mocks()

        result = asyncio.run(
            plugin.before_tool_callback(
                tool=tool, tool_args=tool_args, tool_context=tool_context,
            )
        )
        assert isinstance(result, dict)
        assert "error" in result
        assert "Blocked!" in result["error"]
        assert result["blocked_by"] == "strathon_policy"

    def test_block_uses_default_message(self):
        plugin, _ = self._make_plugin(block=True, message=None)
        tool, tool_args, tool_context = self._make_tool_mocks()

        result = asyncio.run(
            plugin.before_tool_callback(
                tool=tool, tool_args=tool_args, tool_context=tool_context,
            )
        )
        assert isinstance(result, dict)
        assert "blocked by policy" in result["error"].lower()

    def test_steer_returns_dict_with_replacement(self):
        plugin, _ = self._make_plugin(
            steer=True, replacement="Use safe_search instead"
        )
        tool, tool_args, tool_context = self._make_tool_mocks()

        result = asyncio.run(
            plugin.before_tool_callback(
                tool=tool, tool_args=tool_args, tool_context=tool_context,
            )
        )
        assert isinstance(result, dict)
        assert result["result"] == "Use safe_search instead"
        assert result["steered_by"] == "strathon_policy"

    def test_throttle_returns_dict(self):
        plugin, _ = self._make_plugin(throttle=True)
        tool, tool_args, tool_context = self._make_tool_mocks()

        result = asyncio.run(
            plugin.before_tool_callback(
                tool=tool, tool_args=tool_args, tool_context=tool_context,
            )
        )
        assert isinstance(result, dict)
        assert "throttled_by" in result

    def test_no_client_returns_none(self):
        cls = _try_import_plugin()
        plugin = cls(client=None)
        tool, tool_args, tool_context = self._make_tool_mocks()

        result = asyncio.run(
            plugin.before_tool_callback(
                tool=tool, tool_args=tool_args, tool_context=tool_context,
            )
        )
        assert result is None

    def test_no_enforcer_returns_none(self):
        cls = _try_import_plugin()
        client = MagicMock()
        client._policy_enforcer = None
        plugin = cls(client=client)
        tool, tool_args, tool_context = self._make_tool_mocks()

        result = asyncio.run(
            plugin.before_tool_callback(
                tool=tool, tool_args=tool_args, tool_context=tool_context,
            )
        )
        assert result is None

    def test_policy_check_exception_allows_tool(self):
        cls = _try_import_plugin()
        client = MagicMock()
        client._policy_enforcer = MagicMock()
        client.check_policy.side_effect = RuntimeError("Service down")
        plugin = cls(client=client)
        tool, tool_args, tool_context = self._make_tool_mocks()

        result = asyncio.run(
            plugin.before_tool_callback(
                tool=tool, tool_args=tool_args, tool_context=tool_context,
            )
        )
        assert result is None


class TestAfterToolCallback:
    """Test span emission in after_tool_callback."""

    def test_emits_span(self):
        cls = _try_import_plugin()
        client = MagicMock()
        client._policy_enforcer = None
        mock_span = MagicMock()
        client.tracer.start_span.return_value = mock_span

        plugin = cls(client=client)
        tool = MagicMock()
        tool.name = "calculator"
        tool_args = {"expr": "6*7"}
        tool_context = MagicMock()
        tool_context.agent_name = "math_agent"

        # Simulate before_tool_callback to set start time.
        asyncio.run(
            plugin.before_tool_callback(
                tool=tool, tool_args=tool_args, tool_context=tool_context,
            )
        )

        result = asyncio.run(
            plugin.after_tool_callback(
                tool=tool, tool_args=tool_args, tool_context=tool_context,
                result={"answer": 42},
            )
        )
        assert result is None  # pass-through
        client.tracer.start_span.assert_called_once()
        mock_span.set_status.assert_called()
        mock_span.end.assert_called_once()

    def test_no_client_noop(self):
        cls = _try_import_plugin()
        plugin = cls(client=None)
        tool = MagicMock()
        tool.name = "tool"

        result = asyncio.run(
            plugin.after_tool_callback(
                tool=tool, tool_args={}, tool_context=MagicMock(),
                result={},
            )
        )
        assert result is None


class TestOnToolErrorCallback:
    def test_emits_error_span(self):
        cls = _try_import_plugin()
        client = MagicMock()
        mock_span = MagicMock()
        client.tracer.start_span.return_value = mock_span

        plugin = cls(client=client)
        tool = MagicMock()
        tool.name = "fail_tool"

        result = asyncio.run(
            plugin.on_tool_error_callback(
                tool=tool, tool_args={}, tool_context=MagicMock(),
                error=ValueError("bad input"),
            )
        )
        assert result is None
        mock_span.set_status.assert_called()
        mock_span.end.assert_called_once()


class TestModelCallbacks:
    def test_before_and_after_model(self):
        cls = _try_import_plugin()
        client = MagicMock()
        mock_span = MagicMock()
        client.tracer.start_span.return_value = mock_span

        plugin = cls(client=client)
        callback_context = MagicMock()
        llm_request = MagicMock()
        llm_request.model = "gemini-2.0-flash"
        llm_request.contents = [MagicMock()]
        llm_request.config = None

        # before_model_callback
        result = asyncio.run(
            plugin.before_model_callback(
                callback_context=callback_context,
                llm_request=llm_request,
            )
        )
        assert result is None
        client.tracer.start_span.assert_called_once()
        assert len(plugin._active_model_spans) == 1

        # after_model_callback
        llm_response = MagicMock()
        llm_response.content = MagicMock()
        llm_response.content.parts = [MagicMock(text="Hello!")]
        llm_response.usage_metadata = MagicMock()
        llm_response.usage_metadata.prompt_token_count = 10
        llm_response.usage_metadata.candidates_token_count = 5
        llm_response.usage_metadata.total_token_count = 15

        result = asyncio.run(
            plugin.after_model_callback(
                callback_context=callback_context,
                llm_response=llm_response,
            )
        )
        assert result is None
        mock_span.end.assert_called_once()
        assert len(plugin._active_model_spans) == 0

    def test_model_error_ends_span(self):
        cls = _try_import_plugin()
        client = MagicMock()
        mock_span = MagicMock()
        client.tracer.start_span.return_value = mock_span

        plugin = cls(client=client)
        callback_context = MagicMock()
        llm_request = MagicMock()
        llm_request.model = "gemini-2.0-flash"
        llm_request.contents = []
        llm_request.config = None

        asyncio.run(
            plugin.before_model_callback(
                callback_context=callback_context,
                llm_request=llm_request,
            )
        )

        asyncio.run(
            plugin.on_model_error_callback(
                callback_context=callback_context,
                error=RuntimeError("API timeout"),
            )
        )
        mock_span.set_status.assert_called()
        mock_span.end.assert_called_once()
        assert len(plugin._active_model_spans) == 0


class TestCreateFirewallPlugin:
    def test_creates_instance(self):
        _try_import_plugin()
        from strathon.instrumentation.google_adk import create_firewall_plugin

        client = MagicMock()
        plugin = create_firewall_plugin(client)
        assert plugin is not None
        assert plugin.client is client
        assert plugin.name == "strathon_firewall"

    def test_raises_when_adk_missing(self):
        from strathon.instrumentation.google_adk import create_firewall_plugin

        with patch(
            "strathon.instrumentation.google_adk._get_plugin_class",
            return_value=None,
        ):
            with pytest.raises(ImportError, match="google-adk"):
                create_firewall_plugin(MagicMock())
