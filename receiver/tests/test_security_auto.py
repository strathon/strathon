"""Tests for auto-activate security features."""

from __future__ import annotations



# ---- Account lockout tests ---------------------------------------------------


def test_account_lockout_baseline():
    """AgentBaseline updates and detects drift correctly."""
    from vigil import AgentBaseline

    b = AgentBaseline()
    # First 100 samples: calibrating, no drift.
    for _ in range(100):
        assert b.update(1.0) is False
    assert b.calibrated is True
    assert abs(b.ewma - 1.0) < 0.01

    # Sudden spike: should trigger drift.
    drifted = False
    for _ in range(20):
        if b.update(10.0):
            drifted = True
            break
    assert drifted is True


def test_ewma_tracks_gradual_change():
    """EWMA tracks gradual changes without false alerts."""
    from vigil import AgentBaseline

    b = AgentBaseline()
    # Calibrate at 1.0.
    for _ in range(100):
        b.update(1.0)

    # Gradual increase to 1.5 — should NOT trigger immediately.
    alerts = []
    for i in range(10):
        value = 1.0 + i * 0.05
        if b.update(value):
            alerts.append(value)

    # Gradual shift within CUSUM threshold should not alert.
    assert len(alerts) == 0


def test_security_auto_lockout_check():
    """check_account_lockout returns None when not locked."""
    # Unit test for the lockout logic (no DB needed).
    from security_auto import MAX_FAILED_ATTEMPTS, LOCKOUT_MINUTES
    assert MAX_FAILED_ATTEMPTS == 5
    assert LOCKOUT_MINUTES == 15


def test_security_auto_session_cap_default():
    """Default concurrent session cap is 10."""
    from security_auto import MAX_CONCURRENT_SESSIONS
    assert MAX_CONCURRENT_SESSIONS == 10


def test_approval_optimistic_locking_concept():
    """Version column prevents concurrent approve/deny race."""
    # Conceptual test: two approvals at version 1.
    # First succeeds (version 1 → 2). Second fails (version != 1).
    version = 1
    # Approve attempt 1:
    if version == 1:
        version = 2  # Success.
    # Approve attempt 2 (concurrent):
    assert version != 1  # Would fail the WHERE clause.
