"""Main Strathon client for sending traces and managing interventions."""

import logging
from typing import Optional

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from strathon.config import Config
from strathon.exceptions import AuthenticationError

logger = logging.getLogger(__name__)


class Client:
    """
    Strathon client for an agent application.

    On initialization, sets up an OpenTelemetry TracerProvider with an OTLP HTTP
    exporter pointing to your Strathon receiver. Pass this client to ``instrument()``
    to auto-capture traces from supported frameworks, or use ``client.tracer``
    directly to emit spans manually.

    Args:
        api_key: API key obtained from your Strathon dashboard.
        endpoint: Strathon receiver endpoint. Use http://localhost:4318 for self-hosted.
        project_id: Optional project identifier.
        environment: Environment name (e.g. "production", "staging", "dev").
        service_name: OpenTelemetry service.name resource attribute.
        config: Optional Config instance for advanced settings.
        set_global_tracer: If True (default), registers this client's tracer
            provider as the global one when no real provider exists yet.
    """

    def __init__(
        self,
        api_key: str,
        endpoint: str = "http://localhost:4318",
        project_id: Optional[str] = None,
        environment: str = "production",
        service_name: str = "strathon-agent",
        config: Optional[Config] = None,
        set_global_tracer: bool = True,
    ):
        if not api_key:
            raise AuthenticationError("api_key is required")

        self.api_key = api_key
        self.endpoint = endpoint.rstrip("/")
        self.project_id = project_id
        self.environment = environment
        self.service_name = service_name
        self.config = config or Config()

        # OTel resource attributes for every span emitted by this client
        resource_attrs = {
            "service.name": service_name,
            "deployment.environment": environment,
        }
        if project_id:
            resource_attrs["strathon.project_id"] = project_id

        resource = Resource.create(resource_attrs)

        # OTLP HTTP exporter targeting the Strathon receiver
        self._otlp_exporter = OTLPSpanExporter(
            endpoint=f"{self.endpoint}/v1/traces",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=int(self.config.http_timeout_seconds),
        )

        # Batch processor wraps the exporter for async batched sending
        self._span_processor = BatchSpanProcessor(
            self._otlp_exporter,
            max_export_batch_size=self.config.batch_size,
            schedule_delay_millis=int(self.config.batch_timeout_seconds * 1000),
            export_timeout_millis=int(self.config.http_timeout_seconds * 1000),
        )

        # Tracer provider owns the span processor and resource
        self._tracer_provider = TracerProvider(resource=resource)
        self._tracer_provider.add_span_processor(self._span_processor)

        # Register as the global provider only if no real one is set yet
        if set_global_tracer:
            current = trace.get_tracer_provider()
            if isinstance(current, trace.ProxyTracerProvider):
                trace.set_tracer_provider(self._tracer_provider)

        # Named tracer for instrumentations and manual span emission
        self._tracer = self._tracer_provider.get_tracer("strathon", "0.1.0")

        logger.debug(
            "Strathon Client initialized: endpoint=%s environment=%s service=%s",
            self.endpoint,
            self.environment,
            self.service_name,
        )

    @property
    def tracer(self):
        """OpenTelemetry tracer for emitting spans from instrumentations."""
        return self._tracer

    def flush(self, timeout_millis: int = 30000) -> bool:
        """
        Force-flush any pending spans synchronously.

        Returns True if all spans were exported within the timeout.
        """
        return self._span_processor.force_flush(timeout_millis=timeout_millis)

    def shutdown(self) -> None:
        """Flush pending traces and shut down the tracer provider."""
        self._tracer_provider.shutdown()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.shutdown()

    def __repr__(self) -> str:
        return f"Client(endpoint={self.endpoint!r}, environment={self.environment!r})"
