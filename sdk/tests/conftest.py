"""Shared fixtures for the SDK test suite.

The Client builds a real OTLP HTTP exporter aimed at its endpoint. In unit tests
no receiver listens there, so every client that emits a span logs connection
errors and burns seconds on export retries at teardown. This autouse fixture
swaps the OTLP exporter for an in-memory one for the whole suite, so tests never
touch the network and run without that noise. Tests that pass their own
span_exporter are unaffected; tests that assert on exported spans can read them
from the shared exporter.
"""

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)


@pytest.fixture(autouse=True)
def in_memory_span_export(monkeypatch):
    """Route the Client's default OTLP exporter to memory for the test.

    Patches the OTLPSpanExporter symbol the client resolves at construction, so
    a Client built without an explicit span_exporter records spans in memory
    instead of shipping them over HTTP. Yields the exporter so a test can
    inspect finished spans if it needs to.
    """
    exporter = InMemorySpanExporter()
    monkeypatch.setattr(
        "strathon.client.OTLPSpanExporter", lambda *a, **k: exporter
    )
    return exporter
