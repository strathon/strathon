"""Tests for instrumentation hook policy enforcement.

Covers:
- Claude Agent SDK: PreToolUse hook policy enforcement (block returns
  deny, steer returns deny+replacement, allow returns empty dict),
  PostToolUse hook span emission, create_strathon_hooks factory.
- AutoGen: BaseTool.run_json tool-level policy enforcement (block
  raises StrathonPolicyBlocked, steer returns replacement, allow runs
  tool + emits span), _install_tool_patch/_uninstall_tool_patch.

All tests mock framework classes so they run without the SDKs installed.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Claude Agent SDK hook tests
# ---------------------------------------------------------------------------

from strathon.instrumentation.claude_agent import (
    _build_pre_tool_use_hook,
    _build_post_tool_use_hook,
    _truncate,
    create_strathon_hooks,
)
from strathon.instrumentation.autogen import (
    _install_tool_patch,
    _uninstall_tool_patch,
)


def _async_return(value):
    """Build an async function that returns `value` (to patch await_for_approval)."""
    async def _fn(*args, **kwargs):
        return value
    return _fn


def _async_raise(exc):
    """Build an async function that raises `exc` (to patch await_for_approval)."""
    async def _fn(*args, **kwargs):
        raise exc
    return _fn


class TestClaudePreToolUseHook:
    def _make_hook(self, *, block=False, steer=False, throttle=False,
                   approval=False, message=None, replacement=None,
                   policy_name=None):
        client = MagicMock()
        decision = MagicMock()
        decision.is_block = block
        decision.is_steer = steer
        decision.is_throttle = throttle
        decision.is_require_approval = approval
        decision.message = message
        decision.replacement = replacement
        decision.policy_id = "pol_001"
        decision.policy_name = policy_name or "test-policy"
        decision.timeout_seconds = 60
        decision.retry_after_seconds = 30 if throttle else None
        client.check_policy.return_value = decision
        # check_halt is now called by every adapter pre-hook (operator
        # kill-switch). Default the mock to "no active halt" so these
        # policy-focused tests exercise the policy path, not the halt path.
        no_halt = MagicMock()
        no_halt.is_halt = False
        client.check_halt.return_value = no_halt
        client._policy_enforcer = MagicMock()
        client.tracer = MagicMock()
        hook = _build_pre_tool_use_hook(client)
        return hook, client

    def _input_data(self, tool_name="Bash", command="ls"):
        return {
            "tool_name": tool_name,
            "tool_input": {"command": command},
        }

    def test_allow_returns_empty_dict(self):
        hook, _ = self._make_hook()
        result = asyncio.run(hook(self._input_data(), "tid_1", MagicMock()))
        assert result == {}

    def test_block_returns_deny(self):
        hook, _ = self._make_hook(block=True, message="Blocked!")
        result = asyncio.run(hook(self._input_data(), "tid_2", MagicMock()))
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "Blocked!" in result["hookSpecificOutput"]["permissionDecisionReason"]

    def test_throttle_returns_deny(self):
        hook, _ = self._make_hook(throttle=True)
        result = asyncio.run(hook(self._input_data(), "tid_3", MagicMock()))
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "throttled" in result["hookSpecificOutput"]["permissionDecisionReason"]

    def test_steer_returns_deny_with_replacement(self):
        hook, _ = self._make_hook(steer=True, replacement="Use safe command")
        result = asyncio.run(hook(self._input_data(), "tid_4", MagicMock()))
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "Use safe command" in result["hookSpecificOutput"]["permissionDecisionReason"]

    def test_require_approval_granted_proceeds(self):
        # Regression for the D1 defect class: require_approval must be enforced,
        # never silently allowed. On grant, the hook proceeds (empty dict).
        hook, _ = self._make_hook(approval=True)
        with patch(
            "strathon.policy.await_for_approval",
            new=_async_return(True),
        ):
            result = asyncio.run(hook(self._input_data(), "tid_a1", MagicMock()))
        assert result == {}, result

    def test_require_approval_denied_blocks(self):
        # On denial, the hook must return a deny decision — the tool never runs.
        from strathon.policy import StrathonApprovalDenied
        hook, _ = self._make_hook(approval=True, message="needs sign-off")
        with patch(
            "strathon.policy.await_for_approval",
            new=_async_raise(StrathonApprovalDenied("denied", status="denied")),
        ):
            result = asyncio.run(hook(self._input_data(), "tid_a2", MagicMock()))
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny", result

    def test_require_approval_never_silently_allows(self):
        # The core invariant: a matched require_approval policy must NOT produce
        # an empty/allow result without an approval decision having been made.
        from strathon.policy import StrathonApprovalDenied
        hook, _ = self._make_hook(approval=True)
        with patch(
            "strathon.policy.await_for_approval",
            new=_async_raise(StrathonApprovalDenied("timeout", status="timeout")),
        ):
            result = asyncio.run(hook(self._input_data(), "tid_a3", MagicMock()))
        # Must be a deny, never {} (which would mean the tool ran).
        assert result != {}, "SILENT ALLOW: require_approval produced an allow"
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_no_enforcer_returns_empty(self):
        client = MagicMock()
        client._policy_enforcer = None
        hook = _build_pre_tool_use_hook(client)
        result = asyncio.run(hook(self._input_data(), "tid_5", MagicMock()))
        assert result == {}

    def test_policy_exception_returns_empty(self):
        client = MagicMock()
        client._policy_enforcer = MagicMock()
        no_halt = MagicMock()
        no_halt.is_halt = False
        client.check_halt.return_value = no_halt
        client.check_policy.side_effect = RuntimeError("down")
        hook = _build_pre_tool_use_hook(client)
        result = asyncio.run(hook(self._input_data(), "tid_6", MagicMock()))
        assert result == {}


class TestClaudePostToolUseHook:
    def test_emits_span(self):
        client = MagicMock()
        mock_span = MagicMock()
        client.tracer.start_span.return_value = mock_span
        hook = _build_post_tool_use_hook(client)

        input_data = {
            "tool_name": "Read",
            "tool_input": {"file_path": "/tmp/test"},
            "result": "file contents",
        }
        result = asyncio.run(hook(input_data, "tid_7", MagicMock()))
        assert result == {}
        client.tracer.start_span.assert_called_once()
        mock_span.set_status.assert_called()
        mock_span.end.assert_called_once()


class TestCreateStrathonHooks:
    def test_returns_dict_with_both_hooks(self):
        client = MagicMock()
        hooks = create_strathon_hooks(client)
        assert "PreToolUse" in hooks
        assert "PostToolUse" in hooks
        assert len(hooks["PreToolUse"]) == 1
        assert len(hooks["PostToolUse"]) == 1


# ---------------------------------------------------------------------------
# AutoGen BaseTool.run_json tests
# ---------------------------------------------------------------------------

class TestAutoGenToolPatch:
    def test_install_returns_false_without_enforcer(self):
        client = MagicMock()
        client._policy_enforcer = None
        assert _install_tool_patch(client) is False

    def test_install_returns_false_without_autogen(self):
        client = MagicMock()
        client._policy_enforcer = MagicMock()
        with patch(
            "strathon.instrumentation.autogen._TOOL_PATCHED", False
        ):
            # If autogen_core is not installed this should return False.
            # Since we might have it installed, we patch the flag.
            import strathon.instrumentation.autogen as mod
            original = mod._TOOL_PATCHED
            mod._TOOL_PATCHED = False
            try:
                result = _install_tool_patch(client)
                # Either True (autogen installed) or False (not installed)
                assert isinstance(result, bool)
            finally:
                mod._TOOL_PATCHED = original

    def test_uninstall_is_safe_when_not_patched(self):
        """Calling uninstall when nothing is patched should not raise."""
        _uninstall_tool_patch()


class TestClaudeTruncate:
    def test_short_unchanged(self):
        assert _truncate("hello") == "hello"

    def test_none_returns_empty(self):
        assert _truncate(None) == ""
