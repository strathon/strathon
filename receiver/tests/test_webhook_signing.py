"""Tests for webhooks.signing.

Three properties must hold for these tests to be meaningful:

1. Signatures we produce must verify under the reference
   ``standardwebhooks`` library. If the spec library can't verify our
   output, no third-party consumer can.

2. Tampered payloads, expired timestamps, and substituted message ids
   must all fail verification. These are the threat models the spec
   covers, and our signing has no security value if any of them slips
   through.

3. Multi-key rotation must emit one v1 signature per active key in a
   single space-delimited webhook-signature header. Consumers verifying
   against any one of the keys must succeed.

We use the reference library on the verify side for property 1 — this
is the integration test for "we follow the Standard Webhooks spec
correctly," not a tautological assertion against our own helper.
"""

from __future__ import annotations

import math
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

_RECEIVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _RECEIVER_DIR)

from standardwebhooks.webhooks import (  # noqa: E402
    Webhook as _StdWebhook,
    WebhookVerificationError,
)

from webhooks.signing import (  # noqa: E402
    SIGNING_KEY_PREFIX_LEN,
    compute_signature_headers,
    create_signing_key,
    hash_secret,
)


# ---- create_signing_key -------------------------------------------------


def test_create_signing_key_returns_whsec_prefixed_plaintext():
    plaintext, _prefix, _hash = create_signing_key()
    assert plaintext.startswith("whsec_")


def test_create_signing_key_returns_four_char_prefix():
    _, prefix, _ = create_signing_key()
    assert len(prefix) == SIGNING_KEY_PREFIX_LEN == 4


def test_create_signing_key_returns_32_byte_hash():
    _, _, secret_hash = create_signing_key()
    assert len(secret_hash) == 32  # SHA-256 = 256 bits = 32 bytes


def test_create_signing_key_returns_distinct_secrets_each_call():
    """Catches a regression where we accidentally returned a static value."""
    p1, _, _ = create_signing_key()
    p2, _, _ = create_signing_key()
    assert p1 != p2


def test_create_signing_key_hash_matches_hash_secret_helper():
    """create_signing_key's hash must be reproducible from the plaintext
    via hash_secret() — otherwise verifying signing-key identity would
    require the create-time hash, which we don't keep around."""
    plaintext, _, secret_hash = create_signing_key()
    assert hash_secret(plaintext) == secret_hash


# ---- hash_secret --------------------------------------------------------


def test_hash_secret_deterministic():
    assert hash_secret("whsec_test") == hash_secret("whsec_test")


def test_hash_secret_sensitive_to_one_byte_change():
    assert hash_secret("whsec_test") != hash_secret("whsec_tesT")


# ---- compute_signature_headers: structure -------------------------------


def test_single_key_emits_three_headers():
    plaintext, _, _ = create_signing_key()
    headers = compute_signature_headers(
        secrets_plaintext=[plaintext],
        webhook_id="msg_x",
        body='{"a":1}',
    )
    assert set(headers) == {"webhook-id", "webhook-timestamp", "webhook-signature"}


def test_zero_keys_emits_id_and_timestamp_but_no_signature():
    """An operator with no signing keys still gets identifying headers;
    consumers will reject the unsigned delivery, which is correct."""
    headers = compute_signature_headers(
        secrets_plaintext=[],
        webhook_id="msg_x",
        body='{}',
    )
    assert "webhook-id" in headers
    assert "webhook-timestamp" in headers
    assert "webhook-signature" not in headers


def test_empty_string_secret_is_ignored():
    """An empty string in the iterable must not produce a phantom token."""
    p, _, _ = create_signing_key()
    headers = compute_signature_headers(
        secrets_plaintext=["", p],
        webhook_id="msg_x",
        body="{}",
    )
    assert headers["webhook-signature"].count("v1,") == 1


# ---- compute_signature_headers: round-trip with the spec library --------


def test_signature_verifies_with_spec_library():
    """The core integration test: anyone using a Standard Webhooks
    verifier (any of seven supported languages) must accept our signature."""
    plaintext, _, _ = create_signing_key()
    body = '{"event":"test","value":42}'
    headers = compute_signature_headers([plaintext], "msg_roundtrip", body)
    _StdWebhook(plaintext).verify(body, headers)  # raises on failure


def test_tampered_body_fails_spec_verify():
    plaintext, _, _ = create_signing_key()
    body = '{"event":"test"}'
    headers = compute_signature_headers([plaintext], "msg_tamper", body)
    with pytest.raises(WebhookVerificationError):
        _StdWebhook(plaintext).verify(body + " ", headers)


def test_wrong_secret_fails_spec_verify():
    p1, _, _ = create_signing_key()
    p2, _, _ = create_signing_key()
    body = '{"event":"test"}'
    headers = compute_signature_headers([p1], "msg_wrong_secret", body)
    with pytest.raises(WebhookVerificationError):
        _StdWebhook(p2).verify(body, headers)


def test_substituted_message_id_fails_spec_verify():
    """A consumer that trusts a webhook-id from one delivery in another
    would be a serious bug. The signature binds the id, so swapping the
    id while keeping the signature must fail."""
    plaintext, _, _ = create_signing_key()
    body = '{"event":"test"}'
    headers = compute_signature_headers([plaintext], "msg_original", body)
    forged = dict(headers, **{"webhook-id": "msg_forged"})
    with pytest.raises(WebhookVerificationError):
        _StdWebhook(plaintext).verify(body, forged)


def test_old_timestamp_fails_spec_verify():
    """Standard Webhooks tolerance window is ~5 minutes. A 10-minute-old
    signature must be rejected even if otherwise valid — replay defense."""
    plaintext, _, _ = create_signing_key()
    body = '{"event":"test"}'
    old = datetime.now(timezone.utc) - timedelta(minutes=10)
    headers = compute_signature_headers([plaintext], "msg_replay", body, now=old)
    with pytest.raises(WebhookVerificationError):
        _StdWebhook(plaintext).verify(body, headers)


# ---- compute_signature_headers: multi-key rotation ----------------------


def test_two_active_keys_produce_two_signatures():
    p1, _, _ = create_signing_key()
    p2, _, _ = create_signing_key()
    headers = compute_signature_headers([p1, p2], "msg_rotation", '{"x":1}')
    # Space-delimited v1 tokens
    parts = headers["webhook-signature"].split(" ")
    assert len(parts) == 2
    assert all(part.startswith("v1,") for part in parts)


def test_either_rotation_key_verifies():
    """During rotation a consumer that has either the old or new key
    must accept the delivery. This is the property that lets operators
    rotate without a coordination flag day."""
    p1, _, _ = create_signing_key()
    p2, _, _ = create_signing_key()
    body = '{"event":"rotation"}'
    headers = compute_signature_headers([p1, p2], "msg_rot", body)
    _StdWebhook(p1).verify(body, headers)  # consumer with old key
    _StdWebhook(p2).verify(body, headers)  # consumer with new key


def test_consumer_without_either_rotation_key_rejects():
    p1, _, _ = create_signing_key()
    p2, _, _ = create_signing_key()
    p_unrelated, _, _ = create_signing_key()
    body = '{"event":"rotation"}'
    headers = compute_signature_headers([p1, p2], "msg_rot", body)
    with pytest.raises(WebhookVerificationError):
        _StdWebhook(p_unrelated).verify(body, headers)


# ---- timestamp control --------------------------------------------------


def test_timestamp_uses_provided_now_when_given():
    """Tests need control over timestamps to exercise replay protection."""
    plaintext, _, _ = create_signing_key()
    pinned = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    headers = compute_signature_headers(
        [plaintext], "msg_pinned", "{}", now=pinned,
    )
    assert headers["webhook-timestamp"] == str(int(pinned.timestamp()))


def test_timestamp_uses_wallclock_when_omitted():
    plaintext, _, _ = create_signing_key()
    before = math.floor(datetime.now(timezone.utc).timestamp())
    headers = compute_signature_headers([plaintext], "msg_now", "{}")
    after = math.floor(datetime.now(timezone.utc).timestamp())
    got = int(headers["webhook-timestamp"])
    assert before <= got <= after
