"""Built-in credential and secret detection patterns.

70+ patterns covering cloud provider keys, API tokens, database
connection strings, private keys, and common secret formats.

Each pattern has: id, name, regex (compiled with re2 for linear
time), severity (critical/high/medium), and category.

Used by the PII redaction pipeline and by the block-secret-leakage
policy template. Patterns are additive to any custom regex the
operator defines.

Research: GitHub secret scanning patterns,
AWS credential format documentation, TruffleHog detector list,
GitLeaks pattern library.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("strathon.credentials")

try:
    import re2 as _re
except ImportError:
    import re as _re


@dataclass(frozen=True)
class CredentialPattern:
    id: str
    name: str
    pattern: Any  # compiled regex
    severity: str  # critical, high, medium
    category: str  # cloud, api, database, key, token, generic


def _p(id: str, name: str, regex: str, severity: str, category: str) -> CredentialPattern:
    return CredentialPattern(
        id=id, name=name, pattern=_re.compile(regex),
        severity=severity, category=category,
    )


# ---- Cloud Provider Keys ----------------------------------------------------

PATTERNS: list[CredentialPattern] = [
    # AWS
    _p("aws-access-key", "AWS Access Key ID",
       r"(?:A3T[A-Z0-9]|AKIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|ASIA)[A-Z0-9]{16}",
       "critical", "cloud"),
    _p("aws-secret-key", "AWS Secret Access Key",
       r"(?i)aws_?secret_?access_?key\s*[:=]\s*[A-Za-z0-9/+=]{40}",
       "critical", "cloud"),
    _p("aws-session-token", "AWS Session Token",
       r"(?i)aws_?session_?token\s*[:=]\s*[A-Za-z0-9/+=]{100,}",
       "high", "cloud"),

    # GCP
    _p("gcp-api-key", "Google API Key",
       r"AIza[0-9A-Za-z_-]{35}",
       "critical", "cloud"),
    _p("gcp-service-account", "GCP Service Account Key",
       r"\"type\"\s*:\s*\"service_account\"",
       "critical", "cloud"),
    _p("gcp-oauth-token", "Google OAuth Token",
       r"ya29\.[0-9A-Za-z_-]{50,}",
       "high", "cloud"),

    # Azure
    _p("azure-storage-key", "Azure Storage Account Key",
       r"(?i)DefaultEndpointsProtocol=https;AccountName=[^;]+;AccountKey=[A-Za-z0-9+/=]{88}",
       "critical", "cloud"),
    _p("azure-ad-client-secret", "Azure AD Client Secret",
       r"(?i)(?:client_?secret|azure_?secret)\s*[:=]\s*[A-Za-z0-9~._-]{34,}",
       "high", "cloud"),
    _p("azure-connection-string", "Azure Connection String",
       r"(?i)(?:AccountKey|SharedAccessKey)=[A-Za-z0-9+/=]{30,}",
       "high", "cloud"),

    # ---- API Tokens ----------------------------------------------------------

    # GitHub
    _p("github-pat", "GitHub Personal Access Token",
       r"ghp_[A-Za-z0-9]{36}",
       "critical", "token"),
    _p("github-oauth", "GitHub OAuth Token",
       r"gho_[A-Za-z0-9]{36}",
       "high", "token"),
    _p("github-app-token", "GitHub App Token",
       r"(?:ghs|ghr)_[A-Za-z0-9]{36,}",
       "high", "token"),
    _p("github-fine-grained", "GitHub Fine-Grained PAT",
       r"github_pat_[A-Za-z0-9]{22}_[A-Za-z0-9]{59}",
       "critical", "token"),

    # GitLab
    _p("gitlab-pat", "GitLab Personal Access Token",
       r"glpat-[A-Za-z0-9_-]{20,}",
       "critical", "token"),
    _p("gitlab-runner", "GitLab Runner Token",
       r"GR1348941[A-Za-z0-9_-]{20,}",
       "high", "token"),

    # Slack
    _p("slack-bot-token", "Slack Bot Token",
       r"xoxb-[0-9]{10,}-[0-9]{10,}-[A-Za-z0-9]{24,}",
       "critical", "token"),
    _p("slack-user-token", "Slack User Token",
       r"xoxp-[0-9]{10,}-[0-9]{10,}-[0-9]{10,}-[a-f0-9]{32}",
       "critical", "token"),
    _p("slack-signing-secret", "Slack Signing Secret",
       r"xoxs-[0-9]{10,}-[0-9]{10,}-[A-Za-z0-9]{24,}",
       "high", "token"),
    _p("slack-webhook", "Slack Incoming Webhook",
       r"https://hooks\.slack\.com/services/T[A-Z0-9]{8,}/B[A-Z0-9]{8,}/[A-Za-z0-9]{24}",
       "high", "token"),

    # Discord
    _p("discord-bot-token", "Discord Bot Token",
       r"[MN][A-Za-z0-9]{23,}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}",
       "critical", "token"),
    _p("discord-webhook", "Discord Webhook URL",
       r"https://discord(?:app)?\.com/api/webhooks/[0-9]+/[A-Za-z0-9_-]+",
       "high", "token"),

    # OpenAI / Anthropic / AI providers
    _p("openai-api-key", "OpenAI API Key",
       r"sk-[A-Za-z0-9]{20}T3BlbkFJ[A-Za-z0-9]{20}",
       "critical", "token"),
    _p("openai-project-key", "OpenAI Project Key",
       r"sk-proj-[A-Za-z0-9_-]{40,}",
       "critical", "token"),
    _p("anthropic-api-key", "Anthropic API Key",
       r"sk-ant-[A-Za-z0-9_-]{40,}",
       "critical", "token"),
    _p("huggingface-token", "Hugging Face Token",
       r"hf_[A-Za-z0-9]{34,}",
       "high", "token"),
    _p("groq-api-key", "Groq API Key",
       r"gsk_[A-Za-z0-9]{52}",
       "high", "token"),
    _p("xai-api-key", "xAI (Grok) API Key",
       r"xai-[A-Za-z0-9]{80}",
       "high", "token"),
    _p("cohere-api-key", "Cohere API Key",
       r"(?i)cohere[_-]?api[_-]?key\s*[:=]\s*[A-Za-z0-9]{40}",
       "high", "token"),
    _p("perplexity-api-key", "Perplexity API Key",
       r"pplx-[A-Za-z0-9]{48,}",
       "high", "token"),
    _p("replicate-api-token", "Replicate API Token",
       r"r8_[A-Za-z0-9]{37}",
       "high", "token"),

    # Stripe
    _p("stripe-secret-key", "Stripe Secret Key",
       r"sk_live_[A-Za-z0-9]{24,}",
       "critical", "token"),
    _p("stripe-publishable", "Stripe Publishable Key",
       r"pk_live_[A-Za-z0-9]{24,}",
       "high", "token"),
    _p("stripe-restricted", "Stripe Restricted Key",
       r"rk_live_[A-Za-z0-9]{24,}",
       "high", "token"),

    # Twilio
    _p("twilio-api-key", "Twilio API Key",
       r"SK[0-9a-fA-F]{32}",
       "high", "token"),

    # SendGrid
    _p("sendgrid-api-key", "SendGrid API Key",
       r"SG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}",
       "critical", "token"),

    # Mailgun
    _p("mailgun-api-key", "Mailgun API Key",
       r"key-[0-9a-zA-Z]{32}",
       "high", "token"),

    # ---- Database Connection Strings -----------------------------------------

    _p("postgres-uri", "PostgreSQL Connection String",
       r"postgres(?:ql)?://[^\s\"']{10,}",
       "critical", "database"),
    _p("mysql-uri", "MySQL Connection String",
       r"mysql://[^\s\"']{10,}",
       "critical", "database"),
    _p("mongodb-uri", "MongoDB Connection String",
       r"mongodb(?:\+srv)?://[^\s\"']{10,}",
       "critical", "database"),
    _p("redis-uri", "Redis Connection String",
       r"redis(?:s)?://[^\s\"']{10,}",
       "high", "database"),

    # ---- Private Keys --------------------------------------------------------

    _p("rsa-private-key", "RSA Private Key",
       r"-----BEGIN RSA PRIVATE KEY-----",
       "critical", "key"),
    _p("ec-private-key", "EC Private Key",
       r"-----BEGIN EC PRIVATE KEY-----",
       "critical", "key"),
    _p("openssh-private-key", "OpenSSH Private Key",
       r"-----BEGIN OPENSSH PRIVATE KEY-----",
       "critical", "key"),
    _p("pgp-private-key", "PGP Private Key",
       r"-----BEGIN PGP PRIVATE KEY BLOCK-----",
       "critical", "key"),
    _p("pkcs8-private-key", "PKCS8 Private Key",
       r"-----BEGIN PRIVATE KEY-----",
       "critical", "key"),
    _p("encrypted-private-key", "Encrypted Private Key",
       r"-----BEGIN ENCRYPTED PRIVATE KEY-----",
       "high", "key"),

    # ---- JWT / Bearer --------------------------------------------------------

    _p("jwt-token", "JSON Web Token",
       r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
       "high", "token"),
    _p("bearer-token", "Bearer Token in Header",
       r"(?i)(?:authorization|bearer)\s*[:=]\s*bearer\s+[A-Za-z0-9_.-]{20,}",
       "high", "token"),
    _p("basic-auth", "Basic Auth Credentials",
       r"(?i)(?:authorization)\s*[:=]\s*basic\s+[A-Za-z0-9+/=]{10,}",
       "high", "token"),

    # ---- Npm / PyPI / Package Registries -------------------------------------

    _p("npm-token", "npm Access Token",
       r"npm_[A-Za-z0-9]{36}",
       "critical", "token"),
    _p("pypi-token", "PyPI API Token",
       r"pypi-[A-Za-z0-9_-]{50,}",
       "critical", "token"),

    # ---- Infrastructure ------------------------------------------------------

    _p("docker-hub-token", "Docker Hub Token",
       r"dckr_pat_[A-Za-z0-9_-]{20,}",
       "high", "token"),
    _p("terraform-cloud-token", "Terraform Cloud Token",
       r"(?i)(?:TFE_TOKEN|tf_cloud_token)\s*[:=]\s*[A-Za-z0-9.]{14,}",
       "high", "token"),
    _p("vault-token", "HashiCorp Vault Token",
       r"(?:hvs|hvb|hvr)\.[A-Za-z0-9_-]{24,}",
       "critical", "token"),

    # ---- SaaS / Platform Tokens ----------------------------------------------

    _p("vercel-token", "Vercel Token",
       r"(?i)vercel[_-]?(?:api[_-]?)?token\s*[:=]\s*[A-Za-z0-9]{24}",
       "high", "token"),
    _p("supabase-service-key", "Supabase Service Key",
       r"sbp_[A-Za-z0-9]{40}",
       "critical", "token"),
    _p("cloudflare-api-token", "Cloudflare API Token",
       r"(?i)cloudflare[_-]?(?:api[_-]?)?token\s*[:=]\s*[A-Za-z0-9_-]{40}",
       "high", "token"),
    _p("cloudflare-origin-ca", "Cloudflare Origin CA Key",
       r"v1\.0-[A-Za-z0-9]{24}-[A-Za-z0-9]{146}",
       "high", "token"),
    _p("digitalocean-pat", "DigitalOcean Personal Access Token",
       r"dop_v1_[a-f0-9]{64}",
       "critical", "token"),
    _p("digitalocean-oauth", "DigitalOcean OAuth Token",
       r"do[or]_v1_[a-f0-9]{64}",
       "high", "token"),
    _p("shopify-access-token", "Shopify Access Token",
       r"shp(?:at|ca|pa|ss)_[a-fA-F0-9]{32}",
       "critical", "token"),
    _p("datadog-api-key", "Datadog API Key",
       r"(?i)datadog[_-]?api[_-]?key\s*[:=]\s*[a-f0-9]{32}",
       "high", "token"),
    _p("newrelic-license-key", "New Relic License Key",
       r"(?i)NRAK-[A-Z0-9]{27}",
       "high", "token"),
    _p("notion-token", "Notion Integration Token",
       r"(?:secret_|ntn_)[A-Za-z0-9]{43,}",
       "high", "token"),
    _p("linear-api-key", "Linear API Key",
       r"lin_api_[A-Za-z0-9]{40}",
       "high", "token"),
    _p("postmark-token", "Postmark Server Token",
       r"(?i)postmark[_-]?(?:server[_-]?)?token\s*[:=]\s*[a-f0-9-]{36}",
       "high", "token"),
    _p("sentry-dsn", "Sentry DSN",
       r"https://[a-f0-9]{32}@[a-z0-9.-]+\.ingest\.sentry\.io/[0-9]+",
       "medium", "token"),
    _p("atlassian-api-token", "Atlassian API Token",
       r"ATATT3[A-Za-z0-9_=-]{180,}",
       "high", "token"),
    _p("square-access-token", "Square Access Token",
       r"(?:sq0atp-|EAAA)[A-Za-z0-9_-]{22,}",
       "critical", "token"),

    # ---- Generic Secrets -----------------------------------------------------

    _p("generic-api-key", "Generic API Key Assignment",
       r"(?i)(?:api[_-]?key|apikey|api[_-]?secret)\s*[:=]\s*['\"]?[A-Za-z0-9_-]{20,}['\"]?",
       "medium", "generic"),
    _p("generic-password", "Password Assignment",
       r"(?i)(?:password|passwd|pwd)\s*[:=]\s*['\"]?[^\s'\"]{8,}['\"]?",
       "medium", "generic"),
    _p("generic-secret", "Secret Assignment",
       r"(?i)(?:secret|token|credential)\s*[:=]\s*['\"]?[A-Za-z0-9_/+=-]{20,}['\"]?",
       "medium", "generic"),
    _p("private-key-inline", "Inline Private Key Material",
       r"(?i)(?:private[_-]?key)\s*[:=]\s*['\"]?[A-Za-z0-9+/=]{40,}['\"]?",
       "critical", "key"),
]


def scan_text(text: str) -> list[dict[str, Any]]:
    """Scan text for credential patterns. Returns list of findings."""
    if not text:
        return []

    findings = []
    for p in PATTERNS:
        matches = list(p.pattern.finditer(text))
        for m in matches:
            findings.append({
                "pattern_id": p.id,
                "pattern_name": p.name,
                "severity": p.severity,
                "category": p.category,
                "match_start": m.start(),
                "match_end": m.end(),
                "match_length": m.end() - m.start(),
                # Never include the actual secret in the finding.
                "preview": text[max(0, m.start() - 10):m.start()] + "[REDACTED]",
            })

    # Sort by severity (critical first).
    severity_order = {"critical": 0, "high": 1, "medium": 2}
    findings.sort(key=lambda f: severity_order.get(f["severity"], 3))
    return findings


def redact_credentials(text: str) -> tuple[str, int]:
    """Redact all detected credentials from text.

    Returns (redacted_text, count_of_redactions).
    """
    if not text:
        return text, 0

    # Collect all match spans.
    spans = []
    for p in PATTERNS:
        for m in p.pattern.finditer(text):
            spans.append((m.start(), m.end(), p.name))

    if not spans:
        return text, 0

    # Merge overlapping spans.
    spans.sort()
    merged = [spans[0]]
    for s, e, name in spans[1:]:
        if s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e), merged[-1][2])
        else:
            merged.append((s, e, name))

    # Replace from end to preserve offsets.
    result = text
    for s, e, name in reversed(merged):
        result = result[:s] + f"[{name.upper()}_REDACTED]" + result[e:]

    return result, len(merged)


# Summary for API / CLI.
PATTERN_COUNT = len(PATTERNS)
CATEGORIES = sorted(set(p.category for p in PATTERNS))
SEVERITY_COUNTS: dict[str, int] = {}
for p in PATTERNS:
    SEVERITY_COUNTS[p.severity] = SEVERITY_COUNTS.get(p.severity, 0) + 1
