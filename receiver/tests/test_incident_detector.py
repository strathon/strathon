"""Tests for incident detection and Article 73 reporting hooks."""

from __future__ import annotations

import os
import sys


_RECEIVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _RECEIVER_DIR)


class TestArticle73Metadata:
    def test_critical_severity_2_day_deadline(self):
        from incident_detector import _article_73_metadata

        meta = _article_73_metadata("critical", "hash_chain_break")
        assert meta["article"] == "73"
        assert meta["deadline_days"] == 2
        assert "2 days" in meta["description"]

    def test_high_severity_15_day_deadline(self):
        from incident_detector import _article_73_metadata

        meta = _article_73_metadata("high", "policy_block_spike")
        assert meta["deadline_days"] == 15
        assert "15 days" in meta["description"]

    def test_medium_severity_15_day_deadline(self):
        from incident_detector import _article_73_metadata

        meta = _article_73_metadata("medium", "agent_error_spike")
        assert meta["deadline_days"] == 15
        assert "Investigate" in meta["description"]

    def test_deadline_date_is_future(self):
        from datetime import datetime, timezone
        from incident_detector import _article_73_metadata

        meta = _article_73_metadata("high", "budget_auto_halt")
        deadline = datetime.fromisoformat(meta["deadline_date"])
        assert deadline > datetime.now(timezone.utc)


class TestBuildIncidentPayload:
    def test_payload_shape(self):
        import uuid
        from incident_detector import build_incident_payload

        incident = {
            "trigger": "policy_block_spike",
            "severity": "high",
            "details": {"block_count": 75, "window_minutes": 5},
        }
        payload = build_incident_payload(incident, uuid.uuid4())

        assert "incident_id" in payload
        assert payload["severity"] == "high"
        assert payload["trigger"] == "policy_block_spike"
        assert "eu_ai_act_reporting" in payload
        assert payload["eu_ai_act_reporting"]["article"] == "73"
        assert "recommended_actions" in payload
        assert isinstance(payload["recommended_actions"], list)
        assert len(payload["recommended_actions"]) > 0

    def test_block_spike_recommendations(self):
        import uuid
        from incident_detector import build_incident_payload

        payload = build_incident_payload(
            {"trigger": "policy_block_spike", "severity": "high", "details": {}},
            uuid.uuid4(),
        )
        actions = payload["recommended_actions"]
        assert any("attack patterns" in a.lower() for a in actions)
        assert any("article 73" in a.lower() for a in actions)

    def test_budget_halt_recommendations(self):
        import uuid
        from incident_detector import build_incident_payload

        payload = build_incident_payload(
            {"trigger": "budget_auto_halt", "severity": "high", "details": {}},
            uuid.uuid4(),
        )
        actions = payload["recommended_actions"]
        assert any("cost" in a.lower() for a in actions)

    def test_hash_chain_break_recommendations(self):
        import uuid
        from incident_detector import build_incident_payload

        payload = build_incident_payload(
            {"trigger": "hash_chain_break", "severity": "critical", "details": {}},
            uuid.uuid4(),
        )
        actions = payload["recommended_actions"]
        assert any("tamper" in a.lower() for a in actions)
        assert payload["eu_ai_act_reporting"]["deadline_days"] == 2


class TestDefaultThresholds:
    def test_thresholds_exist(self):
        from incident_detector import DEFAULT_THRESHOLDS

        assert "policy_block_spike_count" in DEFAULT_THRESHOLDS
        assert "agent_error_spike_count" in DEFAULT_THRESHOLDS
        assert DEFAULT_THRESHOLDS["policy_block_spike_count"] == 50
        assert DEFAULT_THRESHOLDS["agent_error_spike_window_minutes"] == 5
