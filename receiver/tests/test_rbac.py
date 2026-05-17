"""Unit tests for RBAC: password hashing, role hierarchy, scope mapping.

Pure tests that don't touch the DB — fast enough to run on every commit.
Integration tests (register → login → create member) are in the e2e suite.
"""

import os
import sys

_RECEIVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _RECEIVER_DIR)


# ---- Password hashing (Argon2id) -----------------------------------------

from password import check_needs_rehash, hash_password, verify_password  # noqa: E402


def test_hash_produces_argon2id_format():
    h = hash_password("test-password-123")
    assert h.startswith("$argon2id$v=19$")


def test_hash_contains_owasp_params():
    h = hash_password("test-password-123")
    # OWASP: m=47104, t=1, p=1
    assert "m=47104" in h
    assert "t=1" in h
    assert "p=1" in h


def test_verify_correct_password():
    h = hash_password("correct-horse-battery-staple")
    assert verify_password(h, "correct-horse-battery-staple") is True


def test_verify_wrong_password():
    h = hash_password("correct-horse-battery-staple")
    assert verify_password(h, "wrong-password") is False


def test_verify_empty_password():
    h = hash_password("some-password")
    assert verify_password(h, "") is False


def test_hash_is_unique_per_call():
    """Same password should produce different hashes (different salts)."""
    h1 = hash_password("same-password")
    h2 = hash_password("same-password")
    assert h1 != h2
    # But both should verify
    assert verify_password(h1, "same-password") is True
    assert verify_password(h2, "same-password") is True


def test_check_needs_rehash_current_params():
    """A freshly generated hash should not need rehashing."""
    h = hash_password("test")
    assert check_needs_rehash(h) is False


# ---- RBAC role definitions ------------------------------------------------

from rbac import (  # noqa: E402
    ROLE_RANK,
    ROLE_SCOPES,
    VALID_ROLES,
    can_manage_role,
    role_has_scope,
)


def test_valid_roles_has_four():
    assert len(VALID_ROLES) == 4
    assert VALID_ROLES == {"owner", "admin", "operator", "viewer"}


def test_role_rank_is_ordered():
    assert ROLE_RANK["owner"] > ROLE_RANK["admin"]
    assert ROLE_RANK["admin"] > ROLE_RANK["operator"]
    assert ROLE_RANK["operator"] > ROLE_RANK["viewer"]


def test_can_manage_role_hierarchy():
    # Owner can manage everyone
    assert can_manage_role("owner", "admin") is True
    assert can_manage_role("owner", "operator") is True
    assert can_manage_role("owner", "viewer") is True

    # Admin can manage operator and viewer
    assert can_manage_role("admin", "operator") is True
    assert can_manage_role("admin", "viewer") is True

    # Admin cannot manage owner or another admin
    assert can_manage_role("admin", "owner") is False
    assert can_manage_role("admin", "admin") is False

    # Operator can manage viewer
    assert can_manage_role("operator", "viewer") is True
    assert can_manage_role("operator", "operator") is False
    assert can_manage_role("operator", "admin") is False

    # Viewer can't manage anyone
    assert can_manage_role("viewer", "viewer") is False
    assert can_manage_role("viewer", "operator") is False


def test_owner_has_wildcard_scope():
    assert "*" in ROLE_SCOPES["owner"]


def test_admin_has_no_wildcard():
    assert "*" not in ROLE_SCOPES["admin"]


def test_admin_has_no_projects_manage():
    """Admin cannot delete projects — that requires owner."""
    from auth import SCOPE_PROJECTS_MANAGE
    assert SCOPE_PROJECTS_MANAGE not in ROLE_SCOPES["admin"]


def test_viewer_has_only_read_scopes():
    for scope in ROLE_SCOPES["viewer"]:
        assert ":read" in scope, f"viewer has non-read scope: {scope}"


def test_operator_has_write_scopes():
    from auth import SCOPE_POLICIES_WRITE, SCOPE_HALTS_WRITE, SCOPE_BUDGETS_WRITE
    assert SCOPE_POLICIES_WRITE in ROLE_SCOPES["operator"]
    assert SCOPE_HALTS_WRITE in ROLE_SCOPES["operator"]
    assert SCOPE_BUDGETS_WRITE in ROLE_SCOPES["operator"]


def test_operator_no_api_keys_write():
    """Operators cannot manage API keys — that's admin territory."""
    from auth import SCOPE_API_KEYS_WRITE
    assert SCOPE_API_KEYS_WRITE not in ROLE_SCOPES["operator"]


def test_role_has_scope_works():
    assert role_has_scope("owner", "anything") is True  # wildcard
    assert role_has_scope("viewer", "traces:read") is True
    assert role_has_scope("viewer", "policies:write") is False


# ---- Auth context extension -----------------------------------------------

from auth import ApiKeyContext  # noqa: E402


def test_api_key_context_backward_compat():
    """Existing code that only reads key_id/project_id/key_prefix/scopes works."""
    import uuid
    ctx = ApiKeyContext(
        key_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        key_prefix="stra_test123",
        scopes=("traces:write",),
    )
    # New fields default to None/"apikey"
    assert ctx.user_id is None
    assert ctx.role is None
    assert ctx.auth_method == "apikey"


def test_api_key_context_session_auth():
    """Session auth populates the new fields."""
    import uuid
    ctx = ApiKeyContext(
        key_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        key_prefix="session",
        scopes=("traces:read", "policies:read"),
        user_id=uuid.uuid4(),
        role="viewer",
        auth_method="session",
    )
    assert ctx.user_id is not None
    assert ctx.role == "viewer"
    assert ctx.auth_method == "session"


# ---- Scope coverage sanity checks ----------------------------------------

def test_every_role_has_scopes():
    for role in VALID_ROLES:
        assert len(ROLE_SCOPES[role]) > 0, f"role {role} has no scopes"


def test_role_scopes_are_subsets_of_parent():
    """Each role's scopes should be a subset of the next-higher role's scopes.

    Exception: owner uses wildcard '*' which means 'all scopes'.
    """
    # viewer ⊂ operator ⊂ admin
    assert ROLE_SCOPES["viewer"].issubset(ROLE_SCOPES["operator"]), (
        f"viewer scopes not subset of operator: "
        f"{ROLE_SCOPES['viewer'] - ROLE_SCOPES['operator']}"
    )
    assert ROLE_SCOPES["operator"].issubset(ROLE_SCOPES["admin"]), (
        f"operator scopes not subset of admin: "
        f"{ROLE_SCOPES['operator'] - ROLE_SCOPES['admin']}"
    )
