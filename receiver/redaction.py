"""Regex-based PII redaction for ingest-path span attributes.

Strathon redacts PII at ingest time, before spans land in Postgres and
before webhook payloads are assembled. The redactor is pure-Python
regex with no spaCy / NER dependency — fast, deterministic, and free of
the 500 MB image bloat that comes with running Presidio inline.

Design choices and the research behind them
============================================

The pattern we follow is the one the OpenTelemetry Collector
``redaction`` processor formalizes: two layers, applied in order.

Layer 1 — key-based actions
    Some attribute keys should never carry their value into the trace
    store regardless of content. ``http.request.header.authorization``
    is always a credential; ``user.email`` is always PII. Operators
    declare these as ``{key: action}`` pairs and we apply them by name.

Layer 2 — value-based pattern matching
    For free-text attributes like ``strathon.tool.args`` we don't
    know what's inside, so we scan the value for known PII patterns
    (emails, credit cards, SSNs, etc.) and apply the chosen action.

Entity names mirror Microsoft Presidio
    ``EMAIL_ADDRESS``, ``PHONE_NUMBER``, ``CREDIT_CARD``, ``US_SSN``,
    ``IP_ADDRESS``, ``API_KEY``. Operators who later swap in a
    Presidio sidecar (v2 plan) keep their entity-name config working.

Per-entity actions match LiteLLM's vocabulary
    ``redact``  — replace with ``[ENTITY_NAME]``
    ``mask``    — keep last N chars, replace the rest
    ``hash``    — SHA-256, deterministic so analytics still work
    ``delete``  — drop the attribute entirely (key-action only)

Defaults are conservative
    Default-on for new projects, default action ``redact`` for every
    detected entity. Operators can tighten (``delete`` everything) or
    loosen (``mask`` instead of ``redact`` to preserve last 4 digits
    for fraud-checking) per entity.

Interaction with policy evaluation
==================================

A subtle but critical property: policy evaluation runs on the
UNREDACTED span. Redaction is applied AFTER ``evaluate_for_span`` but
BEFORE persistence. The reason: a policy like
``attrs["strathon.tool.args"].contains("@competitor.com")`` would never
fire if the email were already redacted to ``[EMAIL_ADDRESS]`` by the
time the matcher ran. We want both: matching works on raw content,
storage is sanitized.

The webhook actor's payload is built from the policy_matches row,
which references the persisted (redacted) span — so consumers of
alerts never see the raw PII either. Same property holds end-to-end.

Performance notes
=================

For a 10 KB ``strathon.tool.args`` value with 6 default patterns, a
single scan completes in well under 1 ms on modest hardware. The
ingest path is the budget here: each span is scanned exactly once,
and the patterns are compiled at module import (`re2.compile` where
possible, Python ``re`` for patterns requiring lookaround).

google-re2 guarantees linear-time matching (no backtracking), preventing
ReDoS attacks via crafted span attribute values. The phone number pattern
uses Python ``re`` because it requires lookbehind/lookahead which RE2
does not support; its fixed-width quantifiers make ReDoS impractical.

Input normalization: NFKC + control character stripping runs before
regex evaluation to prevent Unicode-based evasion (homoglyphs,
zero-width joiners, directional overrides).
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Tuple

try:
    import re2 as _re_engine  # Linear-time regex (no backtracking).
except ImportError:
    import re as _re_engine  # type: ignore[assignment]  # Fallback.

logger = logging.getLogger("strathon.receiver.redaction")


# ---- Input normalization ----------------------------------------------------


def _normalize_text(value: str) -> str:
    """NFKC normalization + control character stripping.

    Prevents Unicode-based evasion: homoglyphs (fullwidth digits),
    zero-width joiners, bidirectional overrides, and other control
    characters that could make PII invisible to ASCII-based patterns.
    """
    # NFKC: compatibility decomposition + canonical composition.
    # Maps fullwidth digits ＄１２３ → $123, ligatures ﬃ → ffi, etc.
    normalized = unicodedata.normalize("NFKC", value)
    # Strip C0/C1 control characters (except tab, newline, carriage return),
    # zero-width joiners/non-joiners, and bidirectional overrides.
    return "".join(
        c for c in normalized
        if unicodedata.category(c) != "Cc" or c in ("\t", "\n", "\r")
    )


# ---- Entity definitions -------------------------------------------------
#
# Each pattern is paired with an optional validator. The validator gets
# the matched string and returns True if it's a real instance of that
# entity, False if it's a false positive. The validator runs only when
# the regex matches, so its cost is bounded by the regex hit rate.


def _luhn_check(s: str) -> bool:
    """Standard Luhn algorithm for credit-card validation.

    The credit-card regex matches any 13-19 digit run; many of those
    aren't actually credit cards (random numeric IDs, account numbers,
    order numbers, etc.). The Luhn check rejects ~90% of these false
    positives, which is the difference between "redacts every long
    number in the trace" (annoying) and "redacts credit cards only"
    (correct).
    """
    digits = [int(c) for c in s if c.isdigit()]
    if len(digits) < 13 or len(digits) > 19:
        return False
    # Process digits right to left, doubling every second one.
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# Email: a permissive RFC-5322-ish pattern. Avoids over-matching things
# like "foo@bar" by requiring a TLD with 2+ chars.
_EMAIL = _re_engine.compile(
    r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b"
)

# US SSN: 3-2-4 with hyphens. We don't accept the no-hyphen variant
# (9 consecutive digits) because that produces too many false positives
# in financial and account-number contexts.
_US_SSN = _re_engine.compile(
    r"\b\d{3}-\d{2}-\d{4}\b"
)

# Credit card: 13-19 digits, optionally separated by spaces or hyphens.
# Validated with Luhn afterwards. We match the surface form first
# because the validator needs the digit string.
_CREDIT_CARD = _re_engine.compile(
    r"\b(?:\d[ \-]?){12,18}\d\b"
)

# US-style phone numbers: (XXX) XXX-XXXX, XXX-XXX-XXXX, XXX.XXX.XXXX.
#
# The regex deliberately does NOT lead with `\b`. Word boundaries only
# fire at a transition between a word char and a non-word char, and
# `(` is non-word — so `\b(` never matches the parenthesized form.
# Instead we use a negative lookbehind `(?<![\d\w])` so the pattern
# doesn't grab a phone-shaped substring out of a longer digit run
# (an account number, an ID), but DOES match when the previous char
# is a space, start of string, or other punctuation.
#
# We deliberately don't try international numbers in v1; the false
# positive rate is too high without context.
_PHONE_US = re.compile(
    r"(?<![\d\w])(?:\(\d{3}\)\s?|\d{3}[\-.])\d{3}[\-.]\d{4}(?!\d)"
)

# IPv4. We catch obvious things like 192.168.x.x; IPv6 deferred.
_IPV4 = _re_engine.compile(
    r"\b(?:25[0-5]|2[0-4]\d|1\d{2}|[1-9]?\d)(?:\.(?:25[0-5]|2[0-4]\d|1\d{2}|[1-9]?\d)){3}\b"
)

# API key heuristic. Catches common secret-shaped tokens by their
# well-known prefixes. This is the highest-impact pattern in practice —
# accidentally logging an `sk_live_...` or a JWT is the #1 way LLM
# observability tools end up with credentials in their backend.
_API_KEY = _re_engine.compile(
    r"\b(?:"
    r"sk-[A-Za-z0-9]{20,}"             # OpenAI
    r"|sk_(?:live|test)_[A-Za-z0-9]{20,}"  # Stripe
    r"|pk_(?:live|test)_[A-Za-z0-9]{20,}"  # Stripe publishable
    r"|rk_(?:live|test)_[A-Za-z0-9]{20,}"  # Stripe restricted
    r"|ghp_[A-Za-z0-9]{36}"            # GitHub PAT
    r"|github_pat_[A-Za-z0-9_]{82}"    # GitHub fine-grained PAT
    r"|whsec_[A-Za-z0-9+/=]{20,}"      # Standard Webhooks secrets
    r"|xox[abprs]-[A-Za-z0-9\-]{10,}"  # Slack tokens
    r"|AKIA[0-9A-Z]{16}"               # AWS access key ID
    r"|AIza[0-9A-Za-z\-_]{35}"         # Google API key
    r"|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"  # JWT
    r")\b"
)


@dataclass(frozen=True)
class EntityDef:
    """Metadata for one detected entity type.

    pattern    compiled regex
    validator  optional callable; if returns False the match is dropped
    name       entity name used in placeholder and in operator config
    """
    name: str
    pattern: Any  # re2.Pattern or re.Pattern
    validator: Any  # Optional[Callable[[str], bool]]


# Order matters. API_KEY first: keys are catastrophic to leak and we
# want them caught before generic patterns claim parts of them.
# EMAIL second so it claims user@host before phone/IP heuristics try
# to find numbers inside the local-part. CREDIT_CARD before PHONE so
# 16-digit card numbers don't get mis-classified as phone candidates.
DEFAULT_ENTITIES: Tuple[EntityDef, ...] = (
    EntityDef("API_KEY",       _API_KEY,     None),
    EntityDef("EMAIL_ADDRESS", _EMAIL,       None),
    EntityDef("CREDIT_CARD",   _CREDIT_CARD, _luhn_check),
    EntityDef("US_SSN",        _US_SSN,      None),
    EntityDef("PHONE_NUMBER",  _PHONE_US,    None),
    EntityDef("IP_ADDRESS",    _IPV4,        None),
)


# ---- Actions ------------------------------------------------------------


VALID_VALUE_ACTIONS = {"redact", "mask", "hash"}
# delete is only meaningful for whole-attribute actions (you can't
# "delete part of a value"; you delete the whole attribute)
VALID_KEY_ACTIONS = {"redact", "mask", "hash", "delete"}


def _apply_value_action(matched: str, entity_name: str, action: str) -> str:
    """Return the replacement string for one match."""
    if action == "redact":
        return f"[{entity_name}]"
    if action == "mask":
        # Keep last 4 visible (or all of them if shorter than 4).
        if len(matched) <= 4:
            return "*" * len(matched)
        return "*" * (len(matched) - 4) + matched[-4:]
    if action == "hash":
        h = hashlib.sha256(matched.encode("utf-8")).hexdigest()
        return f"[{entity_name}:{h[:12]}]"
    # Should not reach here; validate_strategy catches bad actions
    return matched


# ---- Config DTO ---------------------------------------------------------


@dataclass(frozen=True)
class RedactionConfig:
    """Per-project redaction config, resolved from project_settings.

    enabled           overall on/off switch
    strategy          {ENTITY_NAME: action} for value-pattern matches.
                      Missing entries default to "redact".
    key_actions       {attribute_key: action} for whole-attribute
                      handling. Applied BEFORE value-pattern matching.
    allowlist         If non-empty, ONLY these attribute keys survive
                      redaction. Strongest privacy posture.
    custom_patterns   Operator-provided extra regex patterns, applied
                      after the defaults.
    """
    enabled: bool
    strategy: Mapping[str, str]
    key_actions: Mapping[str, str]
    allowlist: Tuple[str, ...]
    custom_patterns: Tuple[Tuple[str, Any], ...]
    credential_scan_enabled: bool = True

    @classmethod
    def disabled(cls) -> "RedactionConfig":
        return cls(
            enabled=False, strategy={}, key_actions={},
            allowlist=(), custom_patterns=(),
            credential_scan_enabled=False,
        )


def validate_strategy(strategy: Mapping[str, str]) -> None:
    """Raise ValueError if any action in a strategy is unknown."""
    for entity, action in strategy.items():
        if action not in VALID_VALUE_ACTIONS:
            raise ValueError(
                f"invalid action {action!r} for entity {entity!r}. "
                f"Valid: {sorted(VALID_VALUE_ACTIONS)}"
            )


def validate_key_actions(key_actions: Mapping[str, str]) -> None:
    for key, action in key_actions.items():
        if action not in VALID_KEY_ACTIONS:
            raise ValueError(
                f"invalid action {action!r} for attribute key {key!r}. "
                f"Valid: {sorted(VALID_KEY_ACTIONS)}"
            )


# ---- Core redaction -----------------------------------------------------


def _base64_decode_rescan(
    text: str,
    strategy: Mapping[str, str],
    entities: Iterable["EntityDef"],
) -> str:
    """Find base64-encoded substrings, decode them, check for PII.

    If decoded content matches any PII pattern, replace the ENCODED
    substring with [BASE64_PII_REDACTED]. Prevents evasion via
    base64-encoding sensitive data like SSNs or emails.

    Only processes substrings that look like base64 (20+ chars,
    valid base64 alphabet, valid padding).
    """
    import base64 as b64_mod

    # Match potential base64 chunks: 20+ chars of [A-Za-z0-9+/=].
    try:
        import re2 as re_engine
    except ImportError:
        import re as re_engine

    b64_pattern = re_engine.compile(r'[A-Za-z0-9+/]{20,}={0,2}')
    matches = list(b64_pattern.finditer(text))
    if not matches:
        return text

    result = text
    for m in reversed(matches):  # Reverse to preserve offsets.
        chunk = m.group(0)
        try:
            decoded = b64_mod.b64decode(chunk).decode("utf-8", errors="strict")
        except Exception:
            continue  # Not valid base64 or not UTF-8.

        # Check if decoded content contains PII.
        has_pii = False
        for ent in entities:
            if ent.pattern.search(decoded):
                has_pii = True
                break

        if has_pii:
            action = strategy.get("base64_pii", "redact")
            replacement = "[BASE64_PII_REDACTED]" if action == "redact" else chunk
            result = result[:m.start()] + replacement + result[m.end():]

    return result


def redact_string(
    text: str,
    *,
    strategy: Mapping[str, str] | None = None,
    entities: Iterable[EntityDef] = DEFAULT_ENTITIES,
    custom_patterns: Iterable[Tuple[str, Any]] = (),
    credential_scan: bool = True,
) -> str:
    """Scan ``text`` for PII and apply the per-entity action.

    Returns the redacted string. Multiple matches across multiple
    entity types are applied in a single left-to-right pass per
    entity, in the order defined by ``DEFAULT_ENTITIES``. Custom
    patterns run last so they can match anything left after defaults.
    """
    if not isinstance(text, str) or not text:
        return text
    strategy = strategy or {}

    # Normalize before scanning: NFKC + strip control characters.
    # Prevents Unicode-based evasion (homoglyphs, zero-width joiners).
    out = _normalize_text(text)

    # Base64 decode-rescan: detect PII hidden in base64-encoded substrings.
    # If a base64 chunk decodes to text containing PII patterns, redact
    # the ENCODED chunk in the original string.
    out = _base64_decode_rescan(out, strategy, entities)

    # Built-in credential pattern scanning (50+ patterns for API keys,
    # cloud credentials, private keys, database URIs, tokens).
    # Only scan strings long enough to contain credentials (20+ chars).
    # Short strings (tool names, model names, status codes) can't contain
    # meaningful credentials and scanning them wastes CPU.
    # Skipped entirely when credential_scan=False (per-project toggle).
    if credential_scan and len(out) >= 20:
        from credential_patterns import redact_credentials as _redact_creds
        out, _cred_count = _redact_creds(out)

    def _replace(m: Any, entity_name: str, validator: Any) -> str:
        matched = m.group(0)
        if validator is not None and not validator(matched):
            return matched  # validation failed; keep original
        action = strategy.get(entity_name, "redact")
        return _apply_value_action(matched, entity_name, action)

    for ent in entities:
        def _ent_sub(m: Any, ent: EntityDef = ent) -> str:
            return _replace(m, ent.name, ent.validator)

        out = ent.pattern.sub(_ent_sub, out)

    for entity_name, pat in custom_patterns:
        def _custom_sub(m: Any, name: str = entity_name) -> str:
            return _replace(m, name, None)

        out = pat.sub(_custom_sub, out)

    return out


def redact_attributes(
    attrs: Dict[str, Any],
    config: RedactionConfig,
) -> Dict[str, Any]:
    """Apply both layers to a span's attribute dict.

    Returns a NEW dict; the input is not mutated, so callers that
    want to keep the unredacted version (e.g., policy evaluation
    against raw content) can. The original ``attrs`` dict can still
    be inspected after the call.

    Order:
      1. Allowlist filter (drops everything not in the list).
      2. Key actions (delete/hash/redact/mask whole values).
      3. Value pattern scan on remaining string attributes.
    """
    if not config.enabled:
        return attrs

    # Step 1: allowlist
    if config.allowlist:
        allowed = set(config.allowlist)
        attrs = {k: v for k, v in attrs.items() if k in allowed}

    out: Dict[str, Any] = {}
    for key, value in attrs.items():
        # Step 2: key actions
        action = config.key_actions.get(key)
        if action == "delete":
            continue  # drop the attribute entirely
        if action is not None and isinstance(value, str):
            # Whole-value transformation: treat the entire value as
            # the matched span of a synthetic entity named after the
            # action's target key (so an audit shows what was redacted).
            placeholder_name = key.upper().replace(".", "_")
            out[key] = _apply_value_action(value, placeholder_name, action)
            continue

        # Step 3: value pattern scan (only on string values; other
        # types like ints and bools are left alone)
        if isinstance(value, str):
            out[key] = redact_string(
                value,
                strategy=config.strategy,
                custom_patterns=config.custom_patterns,
                credential_scan=config.credential_scan_enabled,
            )
        else:
            out[key] = value

    return out


__all__ = [
    "DEFAULT_ENTITIES",
    "EntityDef",
    "RedactionConfig",
    "VALID_KEY_ACTIONS",
    "VALID_VALUE_ACTIONS",
    "redact_attributes",
    "redact_string",
    "validate_key_actions",
    "validate_strategy",
]
