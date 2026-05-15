"""Ingest-side policy composition.

After stage 3 of the ORM refactor, this module is intentionally thin:

  - CRUD lives in receiver/repositories/policies.py (uses AsyncSession)
  - CEL expression machinery lives in receiver/policies_eval.py (pure)
  - This module owns the ingest hot path: matching a span against the
    set of policies, and firing alert webhooks. Those two things are
    pure-Python composition, no DB.

We deliberately re-export `PolicyExpressionError` from policies_eval so
existing import sites in main.py (`from policies import PolicyExpressionError`)
keep working without churn.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List
from urllib.parse import urlparse

# Re-export so back-compat imports from main.py and elsewhere keep working
from policies_eval import PolicyExpressionError  # noqa: F401
from policies_eval import evaluate as _evaluate

logger = logging.getLogger("strathon.receiver.policies")


# Constants previously here for the CRUD layer. Kept exported for back-compat
# but the canonical source is schemas/policies.py.
VALID_ACTIONS = {"log", "alert", "block", "steer"}


# ---- Ingest-time evaluation (pure) --------------------------------------


def _build_span_context(span_name: str, attrs: Dict[str, Any]) -> Dict[str, Any]:
    """Match the shape the CEL evaluator expects."""
    return {"name": span_name, "attrs": attrs}


def _span_matches_applies_to(span_name: str, applies_to: List[str]) -> bool:
    """Empty applies_to means 'any span'; otherwise substring-match against name."""
    if not applies_to:
        return True
    return any(token in span_name for token in applies_to)


def evaluate_for_span(
    policies: List[Dict[str, Any]],
    span_name: str,
    attrs: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Return the subset of policies that match this span.

    Pure function: no DB, no webhook. Caller decides what to do with matches.
    Skips disabled policies and policies whose applies_to filter excludes
    this span. Crashes inside individual policy evaluation are swallowed
    and logged so one bad policy can't poison the rest of ingest.
    """
    if not policies:
        return []
    span_ctx = _build_span_context(span_name, attrs)
    matched: List[Dict[str, Any]] = []
    for policy in policies:
        if not policy.get("enabled", True):
            continue
        if not _span_matches_applies_to(span_name, policy.get("applies_to") or []):
            continue
        try:
            if _evaluate(policy["match_expression"], span_ctx):
                matched.append(policy)
        except Exception:
            logger.exception(
                "policy evaluation crashed for policy %s", policy.get("id")
            )
    return matched


# ---- Webhook firing -----------------------------------------------------


async def fire_webhook(
    url: str, payload: Dict[str, Any], timeout_sec: float = 5.0
) -> bool:
    """Fire-and-(mostly)-forget webhook POST.

    Returns True on 2xx, False otherwise. Uses stdlib urllib in a thread
    executor so we don't block the event loop and don't add another
    dependency for what's a tiny POST request.
    """
    if not url:
        return False
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        logger.warning("webhook url has unsupported scheme: %s", parsed.scheme)
        return False

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _post_webhook, url, payload, timeout_sec)


def _post_webhook(url: str, payload: Dict[str, Any], timeout_sec: float) -> bool:
    """Synchronous helper run inside a thread executor."""
    import urllib.error
    import urllib.request

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Strathon-Receiver/0.1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as exc:
        logger.warning("webhook %s returned %s", url, exc.code)
        return False
    except (urllib.error.URLError, TimeoutError) as exc:
        logger.warning("webhook %s failed: %s", url, exc)
        return False
    except Exception:
        logger.exception("webhook %s raised unexpectedly", url)
        return False


__all__ = [
    "PolicyExpressionError",
    "VALID_ACTIONS",
    "evaluate_for_span",
    "fire_webhook",
]
