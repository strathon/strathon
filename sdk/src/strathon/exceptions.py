"""Strathon SDK exceptions."""


class StrathonError(Exception):
    """Base exception for all Strathon SDK errors."""


class AuthenticationError(StrathonError):
    """Raised when API key is missing or invalid."""


class ExportError(StrathonError):
    """Raised when trace export to the Strathon receiver fails."""


class InterventionError(StrathonError):
    """Raised when intervention sync API call fails."""
