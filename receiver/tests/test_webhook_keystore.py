"""Unit tests for webhooks.keystore.

The keystore is in-memory state used by the actor to assemble outbound
signatures. Bugs here would surface as missing or wrong signatures on
delivery, which the consumer would reject — alert delivery would still
work mechanically but verification would fail at the consumer end. So
we want this layer tested at a level of detail that catches subtle
bugs (wrong project_id keying, mixed-up plaintexts during rotation).
"""

from __future__ import annotations

import os
import sys
import uuid

import pytest

_RECEIVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _RECEIVER_DIR)

from webhooks.keystore import (  # noqa: E402
    forget_secret_by_id,
    get_active_secrets,
    remember_secret,
    reset_for_testing,
)


@pytest.fixture(autouse=True)
def _clean_keystore():
    reset_for_testing()
    yield
    reset_for_testing()


def test_empty_keystore_returns_empty_list():
    p = uuid.uuid4()
    assert get_active_secrets(p) == []


def test_remember_then_get_round_trip():
    p = uuid.uuid4()
    k = uuid.uuid4()
    remember_secret(p, "whsec_alpha", key_id=k)
    assert get_active_secrets(p) == ["whsec_alpha"]


def test_multiple_keys_same_project_returns_both():
    """Rotation: both keys are active. Both plaintexts must be in
    the returned list so the actor can emit both signatures."""
    p = uuid.uuid4()
    k1, k2 = uuid.uuid4(), uuid.uuid4()
    remember_secret(p, "whsec_one", key_id=k1)
    remember_secret(p, "whsec_two", key_id=k2)
    got = get_active_secrets(p)
    assert "whsec_one" in got
    assert "whsec_two" in got
    assert len(got) == 2


def test_returned_list_order_is_insertion_order():
    """Predictable ordering matters for debugging multi-signature
    headers: operators reading the header should see the same order
    every time."""
    p = uuid.uuid4()
    k1, k2, k3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    remember_secret(p, "whsec_first", key_id=k1)
    remember_secret(p, "whsec_second", key_id=k2)
    remember_secret(p, "whsec_third", key_id=k3)
    assert get_active_secrets(p) == ["whsec_first", "whsec_second", "whsec_third"]


def test_remembering_same_key_id_with_new_plaintext_replaces():
    """Edge case: operator collides on key_id (shouldn't happen in
    practice with UUID4, but defending against it keeps the model
    consistent). Latest write wins; we don't accumulate stale values."""
    p = uuid.uuid4()
    k = uuid.uuid4()
    remember_secret(p, "whsec_old", key_id=k)
    remember_secret(p, "whsec_new", key_id=k)
    assert get_active_secrets(p) == ["whsec_new"]


def test_projects_are_isolated():
    """A plaintext stored under project A must not appear in project B."""
    pa, pb = uuid.uuid4(), uuid.uuid4()
    remember_secret(pa, "whsec_for_a", key_id=uuid.uuid4())
    remember_secret(pb, "whsec_for_b", key_id=uuid.uuid4())
    assert get_active_secrets(pa) == ["whsec_for_a"]
    assert get_active_secrets(pb) == ["whsec_for_b"]


def test_forget_secret_by_id_drops_only_target():
    p = uuid.uuid4()
    k1, k2 = uuid.uuid4(), uuid.uuid4()
    remember_secret(p, "whsec_keep", key_id=k1)
    remember_secret(p, "whsec_drop", key_id=k2)
    forget_secret_by_id(p, k2)
    assert get_active_secrets(p) == ["whsec_keep"]


def test_forget_unknown_key_id_is_noop():
    """Idempotency: revoking the same row twice or revoking after
    a restart (when the in-memory state is empty) must not raise."""
    p = uuid.uuid4()
    forget_secret_by_id(p, uuid.uuid4())  # no entries; must not raise


def test_forget_last_key_cleans_up_project_bucket():
    """Internal invariant: an empty project bucket is removed entirely
    so the keystore doesn't accumulate stale per-project state."""
    p = uuid.uuid4()
    k = uuid.uuid4()
    remember_secret(p, "whsec_only", key_id=k)
    forget_secret_by_id(p, k)
    assert get_active_secrets(p) == []
    # Internal state: bucket should be gone. We check via the
    # module-private dict; this is a smoke for the cleanup branch.
    from webhooks import keystore
    assert p not in keystore._secrets


def test_remember_empty_plaintext_is_noop():
    """Defensive: an empty string isn't a meaningful secret and we
    don't want to fill the keystore with garbage on misuse."""
    p = uuid.uuid4()
    remember_secret(p, "", key_id=uuid.uuid4())
    assert get_active_secrets(p) == []


def test_key_id_generated_when_omitted():
    """If a caller doesn't supply key_id (synthetic fixture, etc.),
    we generate one so the entry has a stable handle. Two adds without
    explicit ids produce two entries, not one overwritten."""
    p = uuid.uuid4()
    remember_secret(p, "whsec_one")
    remember_secret(p, "whsec_two")
    got = get_active_secrets(p)
    assert "whsec_one" in got
    assert "whsec_two" in got
