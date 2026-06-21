"""Configuration for the Strathon client."""

from dataclasses import dataclass, field
from typing import List


# Default PII redaction patterns. Conservative defaults; users can override.
DEFAULT_REDACT_PATTERNS = [
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b",  # email
    r"\b\d{16}\b",                                            # credit-card-like 16 digits
    r"\b\d{3}-\d{2}-\d{4}\b",                                 # US SSN-like pattern
]


@dataclass
class Config:
    """Configuration options for the Strathon client."""

    # PII redaction: default on for regulated-industry safety
    redact_pii: bool = True
    redact_patterns: List[str] = field(default_factory=lambda: list(DEFAULT_REDACT_PATTERNS))

    # Sampling rate: 1.0 = capture all spans, 0.5 = capture half
    sample_rate: float = 1.0

    # Batching: number of spans per OTLP export call
    batch_size: int = 100
    batch_timeout_seconds: float = 5.0

    # Intervention sync API polling
    intervention_poll_interval_seconds: float = 1.0

    # HTTP request timeout to Strathon receiver
    http_timeout_seconds: float = 10.0

    # Retry behavior on export failure
    max_retries: int = 3
    retry_backoff_seconds: float = 1.0

    def __post_init__(self) -> None:
        """Validate configuration so misconfiguration fails fast with a clear
        message rather than surfacing as a confusing error deep in the export
        pipeline at runtime."""
        if not (0.0 <= self.sample_rate <= 1.0):
            raise ValueError(
                f"sample_rate must be between 0.0 and 1.0, got {self.sample_rate}"
            )
        if self.batch_size < 1:
            raise ValueError(f"batch_size must be >= 1, got {self.batch_size}")
        if self.batch_timeout_seconds <= 0:
            raise ValueError(
                f"batch_timeout_seconds must be > 0, got {self.batch_timeout_seconds}"
            )
        if self.http_timeout_seconds <= 0:
            raise ValueError(
                f"http_timeout_seconds must be > 0, got {self.http_timeout_seconds}"
            )
        if self.intervention_poll_interval_seconds <= 0:
            raise ValueError(
                "intervention_poll_interval_seconds must be > 0, got "
                f"{self.intervention_poll_interval_seconds}"
            )
        if self.max_retries < 0:
            raise ValueError(f"max_retries must be >= 0, got {self.max_retries}")
        if self.retry_backoff_seconds < 0:
            raise ValueError(
                f"retry_backoff_seconds must be >= 0, got {self.retry_backoff_seconds}"
            )
