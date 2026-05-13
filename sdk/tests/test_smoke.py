"""Smoke tests for the Strathon SDK."""

import pytest

from strathon import (
    __version__,
    Client,
    Config,
    AuthenticationError,
)
from strathon.intervention import InterventionState


def test_version():
    assert __version__ == "0.1.0"


def test_client_requires_api_key():
    with pytest.raises(AuthenticationError):
        Client(api_key="")


def test_client_init():
    client = Client(api_key="test-key", endpoint="http://localhost:4318")
    assert client.api_key == "test-key"
    assert client.endpoint == "http://localhost:4318"
    assert client.environment == "production"
    assert client.config.redact_pii is True


def test_client_strips_trailing_slash():
    client = Client(api_key="test-key", endpoint="http://localhost:4318/")
    assert client.endpoint == "http://localhost:4318"


def test_config_defaults():
    config = Config()
    assert config.sample_rate == 1.0
    assert config.batch_size == 100
    assert len(config.redact_patterns) == 3


def test_intervention_states():
    assert InterventionState.PROCEED.value == "proceed"
    assert InterventionState.PAUSE.value == "pause"
    assert InterventionState.HALT.value == "halt"


def test_client_context_manager():
    with Client(api_key="test-key") as client:
        assert client.api_key == "test-key"
