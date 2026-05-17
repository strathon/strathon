"""Audit log subsystem.

This package implements the application-layer half of Strathon's
audit log. The database half lives in migration 010 and
:mod:`models.audit`. Operator-facing endpoints live in
:mod:`api.audit`. The repository function that records an event
inside a request transaction lives in :mod:`repositories.audit`.

What's in this package:

- :mod:`canonical` — deterministic JSON serialization for hashing
- :mod:`hash_chain` — HMAC-SHA256 chain compute over canonical rows
- :mod:`redaction` — per-field sensitivity rules (exclude/hmac/redact)
- :mod:`scim_filter` — SCIM 2.0 filter expression parser → SQL WHERE
- :mod:`actions` — controlled vocabulary of audit action names

The split keeps the deeply-tested pure-logic modules independent of
DB and HTTP, so they can be exercised without booting Postgres or
FastAPI.
"""
