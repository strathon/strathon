"""Pydantic schemas — request/response models for the receiver's API.

Schemas are separate from ORM models because they serve a different purpose:
ORM models describe how data is stored; schemas describe how data crosses
the API boundary. The same `ApiKey` row in the DB might be exposed via
multiple schemas — one without the hash for listing, one for the one-time
creation response that includes the raw key, etc.

Convention:
    XxxBase     — fields common to create/read/update
    XxxCreate   — fields accepted on POST
    XxxUpdate   — fields accepted on PATCH (all optional)
    XxxRead     — fields returned on GET (excludes secrets/internal columns)
"""

from .api_keys import (
    ApiKeyCreate,
    ApiKeyCreateResponse,
    ApiKeyRead,
)
from .policies import (
    VALID_ACTIONS,
    PolicyCreate,
    PolicyRead,
    PolicyUpdate,
)

__all__ = [
    "ApiKeyCreate",
    "ApiKeyCreateResponse",
    "ApiKeyRead",
    "PolicyCreate",
    "PolicyRead",
    "PolicyUpdate",
    "VALID_ACTIONS",
]
