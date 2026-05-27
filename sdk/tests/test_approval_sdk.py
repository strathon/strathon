"""Tests for SDK human approval workflow.

Covers:
- PolicyDecision.is_require_approval property
- StrathonApprovalDenied exception shape
- wait_for_approval returns True on approved
- wait_for_approval raises StrathonApprovalDenied on denied
- wait_for_approval raises on timeout with on_timeout=deny
- wait_for_approval returns True on timeout with on_timeout=allow
- dispatch_policy_decision handles require_approval
- Enforcer returns require_approval decision for matching policy
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from strathon.policy.types import (
    PolicyDecision,
    StrathonApprovalDenied,
    StrathonPolicyBlocked,
)


class TestPolicyDecisionApproval:
    def test_is_require_approval(self):
        d = PolicyDecision(action="require_approval")
        assert d.is_require_approval is True
        assert d.is_block is False

    def test_approval_id_field(self):
        d = PolicyDecision(
            action="require_approval",
            approval_id="abc-123",
            timeout_seconds=60,
        )
        assert d.approval_id == "abc-123"
        assert d.timeout_seconds == 60


class TestStrathonApprovalDenied:
    def test_is_subclass_of_blocked(self):
        assert issubclass(StrathonApprovalDenied, StrathonPolicyBlocked)

    def test_attributes(self):
        exc = StrathonApprovalDenied(
            "denied",
            policy_id="p1",
            policy_name="test",
            approval_id="a1",
            status="denied",
        )
        assert exc.approval_id == "a1"
        assert exc.status == "denied"
        assert exc.policy_id == "p1"


class TestWaitForApproval:
    def test_approved_returns_true(self):
        from strathon.policy.approval import wait_for_approval

        decision = PolicyDecision(
            action="require_approval",
            policy_id="p1",
            policy_name="test",
            timeout_seconds=5,
        )
        client = MagicMock()

        with patch("strathon.policy.approval.request_approval", return_value="a1"), \
             patch("strathon.policy.approval.poll_approval", return_value="approved"):
            result = wait_for_approval(
                client, decision, {"name": "test", "attrs": {}},
            )
            assert result is True

    def test_denied_raises(self):
        from strathon.policy.approval import wait_for_approval

        decision = PolicyDecision(
            action="require_approval",
            policy_id="p1",
            policy_name="test",
            timeout_seconds=5,
        )
        client = MagicMock()

        with patch("strathon.policy.approval.request_approval", return_value="a1"), \
             patch("strathon.policy.approval.poll_approval", return_value="denied"):
            with pytest.raises(StrathonApprovalDenied) as exc_info:
                wait_for_approval(
                    client, decision, {"name": "test", "attrs": {}},
                )
            assert exc_info.value.status == "denied"
            assert exc_info.value.approval_id == "a1"

    def test_expired_raises(self):
        from strathon.policy.approval import wait_for_approval

        decision = PolicyDecision(
            action="require_approval",
            policy_id="p1",
            policy_name="test",
            timeout_seconds=5,
        )
        client = MagicMock()

        with patch("strathon.policy.approval.request_approval", return_value="a1"), \
             patch("strathon.policy.approval.poll_approval", return_value="expired"):
            with pytest.raises(StrathonApprovalDenied) as exc_info:
                wait_for_approval(
                    client, decision, {"name": "test", "attrs": {}},
                )
            assert exc_info.value.status == "expired"

    def test_timeout_deny_raises(self):
        from strathon.policy.approval import wait_for_approval

        decision = PolicyDecision(
            action="require_approval",
            policy_id="p1",
            policy_name="test",
            timeout_seconds=5,
        )
        client = MagicMock()

        with patch("strathon.policy.approval.request_approval", return_value="a1"), \
             patch("strathon.policy.approval.poll_approval", return_value="timeout"):
            with pytest.raises(StrathonApprovalDenied) as exc_info:
                wait_for_approval(
                    client, decision, {"name": "test", "attrs": {}},
                    on_timeout="deny",
                )
            assert exc_info.value.status == "timeout"

    def test_timeout_allow_returns_true(self):
        from strathon.policy.approval import wait_for_approval

        decision = PolicyDecision(
            action="require_approval",
            policy_id="p1",
            policy_name="test",
            timeout_seconds=5,
        )
        client = MagicMock()

        with patch("strathon.policy.approval.request_approval", return_value="a1"), \
             patch("strathon.policy.approval.poll_approval", return_value="timeout"):
            result = wait_for_approval(
                client, decision, {"name": "test", "attrs": {}},
                on_timeout="allow",
            )
            assert result is True

    def test_receiver_unreachable_deny(self):
        from strathon.policy.approval import wait_for_approval

        decision = PolicyDecision(
            action="require_approval",
            policy_id="p1",
            policy_name="test",
            timeout_seconds=5,
        )
        client = MagicMock()

        with patch("strathon.policy.approval.request_approval", return_value=None):
            with pytest.raises(StrathonApprovalDenied):
                wait_for_approval(
                    client, decision, {"name": "test", "attrs": {}},
                )

    def test_receiver_unreachable_allow(self):
        from strathon.policy.approval import wait_for_approval

        decision = PolicyDecision(
            action="require_approval",
            policy_id="p1",
            policy_name="test",
            timeout_seconds=5,
        )
        client = MagicMock()

        with patch("strathon.policy.approval.request_approval", return_value=None):
            result = wait_for_approval(
                client, decision, {"name": "test", "attrs": {}},
                on_timeout="allow",
            )
            assert result is True


class TestDispatchApproval:
    def test_dispatch_calls_wait_for_approval(self):
        from strathon.policy.steer import dispatch_policy_decision

        client = MagicMock()
        # check_halt returns allow
        client.check_halt.return_value = MagicMock(is_halt=False)
        # check_policy returns require_approval
        decision = PolicyDecision(
            action="require_approval",
            policy_id="p1",
            policy_name="test",
            timeout_seconds=5,
        )
        client.check_policy.return_value = decision

        tool_result = "tool output"

        with patch("strathon.policy.approval.wait_for_approval") as mock_wait:
            mock_wait.return_value = True
            result = dispatch_policy_decision(
                client,
                span_name="test.tool",
                attrs={"strathon.tool.name": "search"},
                on_allow=lambda: tool_result,
            )
            mock_wait.assert_called_once()
            assert result == tool_result
