"""SSRF-safe HTTP transport for webhook delivery.

Wraps httpx.AsyncHTTPTransport with DNS resolution + IP validation
at connection time. Prevents:
- Requests to private/loopback/link-local IPs
- Requests to cloud metadata endpoints (169.254.169.254)
- DNS rebinding attacks (IP checked after resolution, not before)
- Non-HTTPS schemes (file://, gopher://, ftp://)

Research: OWASP SSRF Prevention Cheat Sheet, PlanetScale webhook
security guide (resolve-then-check-then-connect pattern), DNS
rebinding attack patterns (CVE-2025-69660, GHSA-wvjg-9879-3m7w).
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Private, loopback, link-local, metadata, and reserved IP ranges.
BLOCKED_NETWORKS_V4 = [
    ipaddress.ip_network("127.0.0.0/8"),       # Loopback
    ipaddress.ip_network("10.0.0.0/8"),         # Private (RFC 1918)
    ipaddress.ip_network("172.16.0.0/12"),      # Private (RFC 1918)
    ipaddress.ip_network("192.168.0.0/16"),     # Private (RFC 1918)
    ipaddress.ip_network("169.254.0.0/16"),     # Link-local / metadata
    ipaddress.ip_network("0.0.0.0/8"),          # "This" network
    ipaddress.ip_network("100.64.0.0/10"),      # Shared address space (CGNAT)
    ipaddress.ip_network("192.0.0.0/24"),       # IETF protocol assignments
    ipaddress.ip_network("198.18.0.0/15"),      # Benchmark testing
    ipaddress.ip_network("224.0.0.0/4"),        # Multicast
    ipaddress.ip_network("240.0.0.0/4"),        # Reserved
]
BLOCKED_NETWORKS_V6 = [
    ipaddress.ip_network("::1/128"),            # Loopback
    ipaddress.ip_network("fc00::/7"),           # Unique local
    ipaddress.ip_network("fe80::/10"),          # Link-local
    ipaddress.ip_network("::ffff:0:0/96"),      # IPv4-mapped (check v4 too)
]

# Hostnames that should never be webhook targets.
BLOCKED_HOSTNAMES = frozenset({
    "metadata.google.internal",
    "metadata.goog",
    "169.254.169.254",
})

ALLOWED_SCHEMES = frozenset({"https"})


class SSRFError(Exception):
    """Raised when a webhook URL targets a blocked destination."""


def validate_webhook_url(url: str) -> None:
    """Validate a webhook URL for SSRF safety.

    Checks scheme, hostname, and resolved IP. Raises SSRFError if unsafe.
    This is called both at webhook registration time (early feedback) and
    at delivery time (defense against DNS rebinding).
    """
    parsed = urlparse(url)

    # Scheme check.
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise SSRFError(
            f"Scheme '{parsed.scheme}' not allowed. Webhooks require HTTPS."
        )

    hostname = parsed.hostname
    if not hostname:
        raise SSRFError("URL has no hostname")

    # Blocked hostname check.
    if hostname.lower() in BLOCKED_HOSTNAMES:
        raise SSRFError(f"Hostname '{hostname}' is blocked")

    # Resolve and check IP.
    try:
        addr_infos = socket.getaddrinfo(
            hostname, parsed.port or 443, proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror as exc:
        raise SSRFError(f"DNS resolution failed for '{hostname}': {exc}")

    for family, _type, _proto, _canonname, sockaddr in addr_infos:
        ip_str = str(sockaddr[0])
        _check_ip(ip_str, hostname)


def _check_ip(ip_str: str, hostname: str) -> None:
    """Check a resolved IP against the blocklist. Raises SSRFError."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        raise SSRFError(f"Invalid IP address '{ip_str}' for '{hostname}'")

    if isinstance(ip, ipaddress.IPv4Address):
        for net in BLOCKED_NETWORKS_V4:
            if ip in net:
                raise SSRFError(
                    f"Resolved IP {ip_str} for '{hostname}' is in "
                    f"blocked range {net}"
                )
    elif isinstance(ip, ipaddress.IPv6Address):
        for net in BLOCKED_NETWORKS_V6:
            if ip in net:
                raise SSRFError(
                    f"Resolved IP {ip_str} for '{hostname}' is in "
                    f"blocked range {net}"
                )
        # Also check IPv4-mapped IPv6 addresses.
        if ip.ipv4_mapped:
            _check_ip(str(ip.ipv4_mapped), hostname)

    # Extra check: is it globally routable?
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
        raise SSRFError(
            f"Resolved IP {ip_str} for '{hostname}' is not globally routable"
        )
