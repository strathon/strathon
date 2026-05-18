"""Argon2id password hashing for Strathon user authentication.

Parameters follow OWASP Password Storage Cheat Sheet (2024) primary
recommendation for Argon2id:

    m=47104 (46 MiB)   — memory cost in KiB
    t=1                 — iterations
    p=1                 — parallelism

These are intentionally conservative for self-hosted deployments that may
run in memory-constrained Docker containers. The argon2-cffi library
defaults (m=65536, t=3, p=4 — RFC 9106 LOW_MEMORY) use more resources;
we trade slightly reduced hardness for predictable behavior in small VMs.

The hash string is self-describing ($argon2id$v=19$m=47104,t=1,p=1$...),
so check_needs_rehash() will detect if parameters change in a future
version and transparently rehash on next login.

Researched: OWASP Password Storage Cheat Sheet, RFC 9106, argon2-cffi
docs (profiles, check_needs_rehash API).
"""

from __future__ import annotations

import logging
from functools import lru_cache

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

logger = logging.getLogger("strathon.receiver.password")

# OWASP recommended Argon2id parameters (primary option)
_TIME_COST = 1
_MEMORY_COST = 47104  # 46 MiB in KiB
_PARALLELISM = 1
_HASH_LEN = 32
_SALT_LEN = 16


@lru_cache(maxsize=1)
def _get_hasher() -> PasswordHasher:
    """Return a singleton PasswordHasher with OWASP params.

    Cached because PasswordHasher validates parameters on construction
    and we don't want to repeat that on every call.
    """
    return PasswordHasher(
        time_cost=_TIME_COST,
        memory_cost=_MEMORY_COST,
        parallelism=_PARALLELISM,
        hash_len=_HASH_LEN,
        salt_len=_SALT_LEN,
    )


def hash_password(password: str) -> str:
    """Hash a password with Argon2id. Returns the full PHC string.

    The returned string includes algorithm, version, parameters, salt,
    and hash — everything needed for verification and rehashing.
    """
    return _get_hasher().hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Verify a password against a stored Argon2id hash.

    Returns True on match, False on mismatch. Constant-time comparison
    is handled internally by argon2-cffi.
    """
    try:
        return _get_hasher().verify(password_hash, password)
    except VerifyMismatchError:
        return False


def check_needs_rehash(password_hash: str) -> bool:
    """Check if a hash was created with outdated parameters.

    Returns True if the hash should be re-computed on next successful
    login (transparent parameter upgrade). The caller should re-hash
    and UPDATE the user row in the same transaction as login.
    """
    return _get_hasher().check_needs_rehash(password_hash)


__all__ = [
    "check_needs_rehash",
    "hash_password",
    "verify_password",
]
