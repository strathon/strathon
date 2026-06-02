"""Tests for credential patterns, circuit breakers, SARIF, MCP gateway."""

from __future__ import annotations



# ---- Credential Pattern Tests ------------------------------------------------

def test_credential_patterns_count():
    from credential_patterns import PATTERN_COUNT
    assert PATTERN_COUNT >= 50


def test_detect_aws_access_key():
    from credential_patterns import scan_text
    text = "my key is AKIAIOSFODNN7EXAMPLE and more"
    findings = scan_text(text)
    assert len(findings) >= 1
    assert any(f["pattern_id"] == "aws-access-key" for f in findings)


def test_detect_github_pat():
    from credential_patterns import scan_text
    text = "token: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij"
    findings = scan_text(text)
    assert any(f["pattern_id"] == "github-pat" for f in findings)


def test_detect_stripe_key():
    from credential_patterns import scan_text
    text = "STRIPE_KEY=sk_live_abcdefghijklmnopqrstuvwx"
    findings = scan_text(text)
    assert any(f["pattern_id"] == "stripe-secret-key" for f in findings)


def test_detect_private_key():
    from credential_patterns import scan_text
    text = "-----BEGIN RSA PRIVATE KEY-----\nMIIE..."
    findings = scan_text(text)
    assert any(f["pattern_id"] == "rsa-private-key" for f in findings)


def test_detect_postgres_uri():
    from credential_patterns import scan_text
    text = "DATABASE_URL=postgresql://user:pass@host:5432/db"
    findings = scan_text(text)
    assert any(f["pattern_id"] == "postgres-uri" for f in findings)


def test_no_false_positive_on_normal_text():
    from credential_patterns import scan_text
    text = "Hello world, this is a normal message with no secrets."
    findings = scan_text(text)
    assert len(findings) == 0


def test_redact_credentials():
    from credential_patterns import redact_credentials
    text = "key is ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij ok"
    redacted, count = redact_credentials(text)
    assert count >= 1
    assert "ghp_" not in redacted
    assert "REDACTED" in redacted


def test_findings_never_contain_actual_secret():
    from credential_patterns import scan_text
    text = "sk_live_abcdefghijklmnopqrstuvwx"
    findings = scan_text(text)
    for f in findings:
        assert "sk_live_" not in f.get("preview", "")


# ---- Circuit Breaker Tests ---------------------------------------------------

def test_circuit_breaker_starts_closed():
    from circuit_breaker import CircuitBreaker, State
    cb = CircuitBreaker(entity_id="test-agent", entity_type="agent")
    assert cb.state == State.CLOSED
    assert cb.should_block() is False


def test_circuit_breaker_trips_after_threshold():
    from circuit_breaker import CircuitBreaker, State, ERROR_THRESHOLD
    cb = CircuitBreaker(entity_id="test-agent", entity_type="agent")
    for _ in range(ERROR_THRESHOLD):
        cb.record_error()
    assert cb.state == State.OPEN
    assert cb.should_block() is True


def test_circuit_breaker_success_doesnt_trip():
    from circuit_breaker import CircuitBreaker, State
    cb = CircuitBreaker(entity_id="test-agent", entity_type="agent")
    for _ in range(100):
        cb.record_success()
    assert cb.state == State.CLOSED


def test_circuit_breaker_check_function():
    from circuit_breaker import check_circuit, get_breaker, ERROR_THRESHOLD
    # Fresh breaker should allow.
    result = check_circuit("clean-agent", "clean-tool")
    assert result is None

    # Trip the agent breaker.
    cb = get_breaker("bad-agent", "agent")
    for _ in range(ERROR_THRESHOLD):
        cb.record_error()
    result = check_circuit("bad-agent")
    assert result is not None
    assert result["blocked_by"] == "circuit_breaker"


def test_circuit_breaker_reset():
    from circuit_breaker import get_breaker, reset_breaker, State, ERROR_THRESHOLD
    cb = get_breaker("reset-test", "agent")
    for _ in range(ERROR_THRESHOLD):
        cb.record_error()
    assert cb.state == State.OPEN
    reset_breaker("reset-test", "agent")
    assert cb.state == State.CLOSED


# ---- SARIF Tests -------------------------------------------------------------

def test_sarif_generates_valid_structure():
    from sarif_output import generate_sarif
    sarif = generate_sarif(violations=[{
        "policy_name": "block-prompt-injection",
        "action": "block",
        "agent_name": "test-agent",
        "tool_name": "search",
        "severity": "high",
    }])
    assert sarif["version"] == "2.1.0"
    assert len(sarif["runs"]) == 1
    assert len(sarif["runs"][0]["results"]) == 1
    assert sarif["runs"][0]["results"][0]["level"] == "error"


def test_sarif_empty_report():
    from sarif_output import generate_sarif
    sarif = generate_sarif()
    assert sarif["version"] == "2.1.0"
    assert len(sarif["runs"][0]["results"]) == 0


def test_sarif_credential_finding():
    from sarif_output import generate_sarif
    sarif = generate_sarif(credential_findings=[{
        "pattern_id": "aws-access-key",
        "pattern_name": "AWS Access Key ID",
        "severity": "critical",
        "category": "cloud",
    }])
    result = sarif["runs"][0]["results"][0]
    assert result["level"] == "error"
    assert "aws-access-key" in result["ruleId"]


# ---- MCP Gateway Tests -------------------------------------------------------

def test_mcp_gateway_error_response():
    from mcp_gateway import MCPSecurityGateway
    resp = MCPSecurityGateway._error("req-1", -32600, "Blocked")
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == "req-1"
    assert resp["error"]["code"] == -32600
