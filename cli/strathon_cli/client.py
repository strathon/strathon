"""HTTP client for Strathon receiver API.

Reads STRATHON_API_KEY and STRATHON_ENDPOINT from environment.
All CLI commands use this client for API calls.
"""

from __future__ import annotations

import os
import sys
from typing import Any

import click
import httpx


def get_client() -> tuple[httpx.Client, str]:
    """Return (httpx.Client, base_url) configured from env vars.

    Exits with error message if STRATHON_API_KEY is not set.
    """
    api_key = os.environ.get("STRATHON_API_KEY")
    if not api_key:
        click.echo(
            "Error: STRATHON_API_KEY environment variable not set.\n"
            "  export STRATHON_API_KEY=stra_...",
            err=True,
        )
        sys.exit(1)

    endpoint = os.environ.get(
        "STRATHON_ENDPOINT", "http://localhost:4318"
    )

    client = httpx.Client(
        base_url=endpoint,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30.0,
    )
    return client, endpoint


def api_get(path: str, params: dict | None = None) -> dict[str, Any]:
    """GET request to the Strathon API."""
    client, _ = get_client()
    try:
        r = client.get(path, params=params or {})
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        _handle_error(e)
    except httpx.ConnectError:
        click.echo("Error: cannot connect to Strathon receiver", err=True)
        sys.exit(1)
    return {}


def api_post(path: str, json: dict | None = None) -> dict[str, Any]:
    """POST request to the Strathon API."""
    client, _ = get_client()
    try:
        r = client.post(path, json=json or {})
        r.raise_for_status()
        if r.status_code == 204:
            return {}
        return r.json()
    except httpx.HTTPStatusError as e:
        _handle_error(e)
    except httpx.ConnectError:
        click.echo("Error: cannot connect to Strathon receiver", err=True)
        sys.exit(1)
    return {}


def api_patch(path: str, json: dict | None = None) -> dict[str, Any]:
    """PATCH request to the Strathon API."""
    client, _ = get_client()
    try:
        r = client.patch(path, json=json or {})
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        _handle_error(e)
    except httpx.ConnectError:
        click.echo("Error: cannot connect to Strathon receiver", err=True)
        sys.exit(1)
    return {}


def api_delete(path: str) -> None:
    """DELETE request to the Strathon API."""
    client, _ = get_client()
    try:
        r = client.delete(path)
        r.raise_for_status()
    except httpx.HTTPStatusError as e:
        _handle_error(e)
    except httpx.ConnectError:
        click.echo("Error: cannot connect to Strathon receiver", err=True)
        sys.exit(1)


def _handle_error(e: httpx.HTTPStatusError) -> None:
    """Print error and exit."""
    try:
        body = e.response.json()
        detail = body.get("detail") or body.get("error", {}).get("message", "")
    except Exception:
        detail = e.response.text

    click.echo(f"Error {e.response.status_code}: {detail}", err=True)
    sys.exit(1)
