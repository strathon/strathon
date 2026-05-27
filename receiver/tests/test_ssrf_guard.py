"""Tests for webhook SSRF guard."""

from __future__ import annotations

import os
import sys

import pytest

_RECEIVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _RECEIVER_DIR)

from webhooks.ssrf_guard import SSRFError, validate_webhook_url  # noqa: E402


class TestSSRFGuard:
    def test_https_allowed(self):
        # This may raise SSRFError for DNS failure, but not for scheme.
        try:
            validate_webhook_url("https://example.com/webhook")
        except SSRFError as e:
            assert "Scheme" not in str(e)

    def test_http_blocked(self):
        with pytest.raises(SSRFError, match="Scheme"):
            validate_webhook_url("http://example.com/webhook")

    def test_ftp_blocked(self):
        with pytest.raises(SSRFError, match="Scheme"):
            validate_webhook_url("ftp://example.com/file")

    def test_file_blocked(self):
        with pytest.raises(SSRFError, match="Scheme"):
            validate_webhook_url("file:///etc/passwd")

    def test_localhost_blocked(self):
        with pytest.raises(SSRFError):
            validate_webhook_url("https://localhost/webhook")

    def test_127_0_0_1_blocked(self):
        with pytest.raises(SSRFError):
            validate_webhook_url("https://127.0.0.1/webhook")

    def test_private_10_blocked(self):
        with pytest.raises(SSRFError):
            validate_webhook_url("https://10.0.0.1/webhook")

    def test_private_172_blocked(self):
        with pytest.raises(SSRFError):
            validate_webhook_url("https://172.16.0.1/webhook")

    def test_private_192_blocked(self):
        with pytest.raises(SSRFError):
            validate_webhook_url("https://192.168.1.1/webhook")

    def test_metadata_ip_blocked(self):
        with pytest.raises(SSRFError):
            validate_webhook_url("https://169.254.169.254/latest/meta-data/")

    def test_metadata_hostname_blocked(self):
        with pytest.raises(SSRFError, match="blocked"):
            validate_webhook_url("https://metadata.google.internal/webhook")

    def test_no_hostname_blocked(self):
        with pytest.raises(SSRFError):
            validate_webhook_url("https:///no-host")

    def test_ipv6_loopback_blocked(self):
        with pytest.raises(SSRFError):
            validate_webhook_url("https://[::1]/webhook")
