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
        enable_policies: If True (default), pulls runtime intervention policies
            from the receiver and enforces block/steer rules via check_policy().
        policy_refresh_interval_sec: How often to refresh policies from the
            server (default 30s).
        enable_halts: If True (default), polls the receiver for operator-imposed
            halts and raises StrathonHaltExceeded at tool boundaries when an
            active halt matches the calling agent. Polls fail-open by default:
            an unreachable receiver does NOT halt agents.
        halt_refresh_interval_sec: How often to poll for halts (default 1s).
            Faster than policy refresh because operators expect kill-switches
            to take effect quickly.
        fail_closed: If True, both the policy and halt enforcers raise
            StrathonReceiverUnreachable at the tool boundary whenever their
            cached state is older than fail_closed_max_staleness_sec. Default
            False preserves historical fail-open behavior — a brief receiver
            outage continues to be served from last-known state. Turn this on
            when your environment prefers stopping agents over running on
            stale policy/halt state.
        fail_closed_max_staleness_sec: How old the cached state may be before
            fail-closed mode treats it as unreachable. Default 60s leaves
            comfortable headroom over the 1s halt and 30s policy refresh
            intervals; brief receiver hiccups don't trip it.
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
        enable_policies: bool = True,
        policy_refresh_interval_sec: float = 30.0,
        enable_halts: bool = True,
        halt_refresh_interval_sec: float = 1.0,
        fail_closed: bool = False,
        fail_closed_max_staleness_sec: float = 60.0,
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

        # Runtime intervention: optional policy enforcer
        self._policy_enforcer = None
        if enable_policies:
            # Import lazily so tests that don't need policies don't pay the cost
            from strathon.policy.enforcer import PolicyEnforcer

            self._policy_enforcer = PolicyEnforcer(
                endpoint=self.endpoint,
                api_key=api_key,
                project_id=project_id,
                refresh_interval_sec=policy_refresh_interval_sec,
                fail_closed=fail_closed,
                fail_closed_max_staleness_sec=fail_closed_max_staleness_sec,
            )
            # start() does a synchronous fetch + spawns background refresh.
            # We swallow failures so an unreachable receiver doesn't break
            # the client; check_policy will return ALLOW until policies arrive.
            try:
                self._policy_enforcer.start()
            except Exception:
                logger.debug(
                    "Strathon: policy enforcer failed to start; intervention disabled until next refresh"
                )

        # Runtime intervention: optional halt enforcer
        # Same start-time + fail-open pattern as the policy enforcer.
        # Conceptually parallel: policies are "this specific action is
        # blocked", halts are "this whole agent is stopped".
        self._halt_enforcer = None
        if enable_halts:
            from strathon.policy.halt_enforcer import HaltEnforcer

            self._halt_enforcer = HaltEnforcer(
                endpoint=self.endpoint,
                api_key=api_key,
                project_id=project_id,
                refresh_interval_sec=halt_refresh_interval_sec,
                fail_closed=fail_closed,
                fail_closed_max_staleness_sec=fail_closed_max_staleness_sec,
            )
            try:
                self._halt_enforcer.start()
            except Exception:
                logger.debug(
                    "Strathon: halt enforcer failed to start; halt enforcement "
                    "disabled until next refresh"
                )

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

    @property
    def policy_enforcer(self):
        """The runtime intervention policy enforcer, or None if disabled."""
        return self._policy_enforcer

    @property
    def halt_enforcer(self):
        """The operator-halt enforcer, or None if disabled."""
        return self._halt_enforcer

    def check_policy(self, span_context: dict):
        """Evaluate active policies against a candidate action.

        Args:
            span_context: A dict with shape ``{"name": str, "attrs": dict}``
                describing the action that's about to happen. Framework
                integrations call this from inside their "before tool call"
                / "before LLM call" hooks.

        Returns:
            A PolicyDecision. When ``decision.is_allow`` the caller should
            proceed normally. When ``decision.is_block``, the caller should
            raise ``StrathonPolicyBlocked(decision.message)``. When
            ``decision.is_steer``, the caller should return
            ``decision.replacement`` instead of executing the real action.
        """
        if self._policy_enforcer is None:
            from strathon.policy.types import ALLOW
            return ALLOW
        return self._policy_enforcer.check_policy(span_context)

    def check_halt(self, span_context: dict):
        """Consult the halt cache for the calling agent.

        Mirror of check_policy but for operator-imposed kill-switches.
        Framework integrations call this in the same hook as
        check_policy, and check halt FIRST — if halt fires, the policy
        check is skipped because there's no point evaluating match
        expressions on an agent that's supposed to be off.

        Returns:
            A HaltDecision. On is_allow, proceed. On is_halt, raise
            StrathonHaltExceeded with the halt's reason/scope.
        """
        if self._halt_enforcer is None:
            from strathon.policy.types import ALLOW_HALT
            return ALLOW_HALT
        return self._halt_enforcer.check_halt(span_context)

    def flush(self, timeout_millis: int = 30000) -> bool:
        """
        Force-flush any pending spans synchronously.

        Returns True if all spans were exported within the timeout.
        """
        return self._span_processor.force_flush(timeout_millis=timeout_millis)

    def shutdown(self) -> None:
        """Flush pending traces and shut down the tracer provider."""
        if self._policy_enforcer is not None:
            try:
                self._policy_enforcer.stop()
            except Exception:
                logger.debug("Strathon: policy enforcer stop raised")
        self._tracer_provider.shutdown()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.shutdown()

    def __repr__(self) -> str:
        return f"Client(endpoint={self.endpoint!r}, environment={self.environment!r})"
