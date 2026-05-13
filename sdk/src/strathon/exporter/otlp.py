"""OTLP HTTP exporter for sending traces to the Strathon receiver."""

import logging
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)


class OTLPExporter:
    """
    Exports OpenTelemetry traces to a Strathon receiver via OTLP HTTP.

    Args:
        endpoint: Full URL to the /v1/traces endpoint.
        headers: HTTP headers (typically includes Authorization).
        timeout: HTTP request timeout in seconds.
    """

    def __init__(
        self,
        endpoint: str,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = 10.0,
    ):
        self.endpoint = endpoint
        self.headers = headers or {}
        self.timeout = timeout
        self._client = httpx.Client(timeout=timeout)

    def export(self, spans: Any) -> None:
        """
        Export a batch of spans to the Strathon receiver.

        Args:
            spans: OTLP-formatted span batch (protobuf or JSON).
        """
        # TODO: serialize spans to OTLP protobuf
        # TODO: POST to self.endpoint with retries
        # TODO: handle 4xx/5xx response codes
        raise NotImplementedError("OTLP export not yet implemented")

    def shutdown(self) -> None:
        """Close the HTTP client."""
        self._client.close()
