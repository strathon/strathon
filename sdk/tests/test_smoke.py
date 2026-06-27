"""Smoke tests for the Strathon SDK."""

import pytest

from strathon import (
    __version__,
    Client,
    Config,
    AuthenticationError,
)
from strathon.intervention import InterventionState


def make_client(**kwargs):
    """Helper that builds a Client without touching the global tracer provider."""
    defaults = dict(
        api_key="test-key",
        endpoint="http://localhost:4318",
        set_global_tracer=False,
        enable_policies=False,
    )
    defaults.update(kwargs)
    return Client(**defaults)


def test_version():
    assert __version__ == "1.2.3"


def test_client_requires_api_key():
    with pytest.raises(AuthenticationError):
        Client(api_key="", set_global_tracer=False, enable_policies=False)


def test_client_init():
    client = make_client()
    assert client.api_key == "test-key"
    assert client.endpoint == "http://localhost:4318"
    assert client.environment == "production"
    assert client.service_name == "strathon-agent"
    assert client.config.redact_pii is True


def test_client_strips_trailing_slash():
    client = make_client(endpoint="http://localhost:4318/")
    assert client.endpoint == "http://localhost:4318"


def test_client_has_tracer():
    client = make_client()
    tracer = client.tracer
    assert tracer is not None
    # Tracer should be able to start a span
    with tracer.start_as_current_span("test-span") as span:
        assert span is not None
        span.set_attribute("strathon.test", "ok")


def test_client_with_project_id():
    client = make_client(project_id="proj-123", environment="staging")
    assert client.project_id == "proj-123"
    assert client.environment == "staging"


def test_config_defaults():
    config = Config()
    assert config.sample_rate == 1.0
    assert config.batch_size == 100
    assert config.redact_pii is True
    assert len(config.redact_patterns) == 3


def test_intervention_states():
    assert InterventionState.PROCEED.value == "proceed"
    assert InterventionState.PAUSE.value == "pause"
    assert InterventionState.HALT.value == "halt"


def test_client_context_manager():
    with make_client() as client:
        assert client.api_key == "test-key"
        # Tracer is usable inside the context
        with client.tracer.start_as_current_span("ctx-span"):
            pass
    # After __exit__, shutdown was called; further span emission should still
    # work without raising (OTel handles this gracefully)


def test_client_flush_returns_bool():
    client = make_client()
    result = client.flush(timeout_millis=100)
    assert isinstance(result, bool)
    client.shutdown()


# ---------------------------------------------------------------------------
# Regression tests. These lock in config validation and endpoint-scheme
# checks so the behavior cannot silently revert.
# ---------------------------------------------------------------------------


def test_config_rejects_invalid_sample_rate():
    with pytest.raises(ValueError, match="sample_rate"):
        Config(sample_rate=1.5)
    with pytest.raises(ValueError, match="sample_rate"):
        Config(sample_rate=-0.1)


def test_config_rejects_invalid_batch_size():
    with pytest.raises(ValueError, match="batch_size"):
        Config(batch_size=0)


def test_config_rejects_non_positive_timeouts():
    with pytest.raises(ValueError, match="batch_timeout_seconds"):
        Config(batch_timeout_seconds=0)
    with pytest.raises(ValueError, match="http_timeout_seconds"):
        Config(http_timeout_seconds=-1)


def test_config_rejects_negative_retries():
    with pytest.raises(ValueError, match="max_retries"):
        Config(max_retries=-1)


def test_config_accepts_valid_values():
    # Boundary values must be allowed.
    Config(sample_rate=0.0)
    Config(sample_rate=1.0)
    Config(batch_size=1)
    Config(max_retries=0)


def test_client_rejects_endpoint_without_scheme():
    with pytest.raises(ValueError, match="http"):
        Client(
            api_key="k",
            endpoint="localhost:4318",
            set_global_tracer=False,
            enable_policies=False,
            enable_halts=False,
        )


def test_client_rejects_non_http_scheme():
    with pytest.raises(ValueError, match="http"):
        Client(
            api_key="k",
            endpoint="ftp://example.com",
            set_global_tracer=False,
            enable_policies=False,
            enable_halts=False,
        )


def test_client_registers_atexit_flush():
    # The client must register an atexit flush so a script that exits without
    # calling shutdown() does not silently lose its last span batch.
    client = make_client()
    assert client._atexit_registered is True
    # Explicit shutdown unregisters it (no double-flush into a torn-down provider).
    client.shutdown()
    assert client._atexit_registered is False
