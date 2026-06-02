"""Tests for Vigil behavioral-drift detection (vigil.py).

Vigil is claimed as the ASI05 (memory poisoning) / ASI10 (rogue agent)
control via EWMA/CUSUM drift detection. These tests verify the core
AgentBaseline.update() math: calibration, stability (no false drift on a
steady signal), and detection of a sustained shift.

The detector is pure/deterministic, so these tests need no DB or network.
"""

from __future__ import annotations

from vigil import AgentBaseline, MIN_SPANS_FOR_BASELINE


def _calibrate(b: AgentBaseline, value: float = 10.0):
    """Feed enough samples to calibrate the baseline."""
    for _ in range(MIN_SPANS_FOR_BASELINE):
        assert b.update(value) is False  # never drifts during calibration
    assert b.calibrated is True


def test_calibration_requires_min_samples():
    b = AgentBaseline()
    for i in range(MIN_SPANS_FOR_BASELINE - 1):
        b.update(10.0)
        assert b.calibrated is False
    b.update(10.0)
    assert b.calibrated is True


def test_stable_signal_never_drifts():
    b = AgentBaseline()
    _calibrate(b, 10.0)
    # A steady signal at the baseline must not trigger drift.
    for _ in range(200):
        assert b.update(10.0) is False


def test_sustained_shift_triggers_drift():
    b = AgentBaseline()
    _calibrate(b, 10.0)
    # A large sustained jump should breach CUSUM within a bounded number of
    # observations.
    drift_detected = False
    for _ in range(50):
        if b.update(100.0):
            drift_detected = True
            break
    assert drift_detected, "sustained large shift should trigger CUSUM drift"


def test_drift_resets_after_alert():
    b = AgentBaseline()
    _calibrate(b, 10.0)
    for _ in range(50):
        if b.update(100.0):
            break
    # After an alert the CUSUM accumulators are reset so it doesn't fire every
    # subsequent tick.
    assert b.cusum_pos == 0.0 and b.cusum_neg == 0.0


def test_small_noise_does_not_trigger():
    b = AgentBaseline()
    _calibrate(b, 10.0)
    # Small symmetric noise around the baseline should stay under threshold.
    seq = [10.5, 9.5, 10.2, 9.8, 10.1, 9.9] * 20
    assert not any(b.update(v) for v in seq)
