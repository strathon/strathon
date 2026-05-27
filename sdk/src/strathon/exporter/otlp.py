"""OTLP HTTP exporter for Strathon.

Re-exports OpenTelemetry's OTLPSpanExporter so users have a stable strathon-namespaced
import path. The Strathon Client wires this up automatically; direct use is for
advanced cases (e.g. attaching to an existing TracerProvider).
"""

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

__all__ = ["OTLPSpanExporter"]
