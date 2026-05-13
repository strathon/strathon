"""Main Strathon client for sending traces and managing interventions."""

from typing import Optional

from strathon.config import Config
from strathon.exceptions import AuthenticationError
from strathon.exporter.otlp import OTLPExporter


class Client:
    """
    Strathon client for an agent application.

    Manages OpenTelemetry tracer, OTLP exporter, and intervention API client.
    Pass this client to ``instrument()`` to auto-capture traces from supported frameworks.

    Args:
        api_key: API key obtained from your Strathon dashboard.
        endpoint: Strathon receiver endpoint. Use http://localhost:4318 for self-hosted.
        project_id: Optional project identifier.
        environment: Environment name (e.g. "production", "staging", "dev").
        config: Optional Config instance for advanced settings.
    """

    def __init__(
        self,
        api_key: str,
        endpoint: str = "http://localhost:4318",
        project_id: Optional[str] = None,
        environment: str = "production",
        config: Optional[Config] = None,
    ):
        if not api_key:
            raise AuthenticationError("api_key is required")

        self.api_key = api_key
        self.endpoint = endpoint.rstrip("/")
        self.project_id = project_id
        self.environment = environment
        self.config = config or Config()

        self._exporter = OTLPExporter(
            endpoint=f"{self.endpoint}/v1/traces",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=self.config.http_timeout_seconds,
        )

        # TODO: initialize OpenTelemetry tracer provider
        # TODO: initialize intervention sync client
        # TODO: start background batch flush

    def shutdown(self) -> None:
        """Flush pending traces and close connections."""
        self._exporter.shutdown()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.shutdown()

    def __repr__(self) -> str:
        return f"Client(endpoint={self.endpoint!r}, environment={self.environment!r})"
