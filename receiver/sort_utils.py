"""Parse a caller-supplied ``field:direction`` sort parameter.

List endpoints accept an optional ``sort`` string like ``duration:desc``. The
field is looked up in a per-endpoint allowlist that maps a stable public sort
key to a real column name, so a caller can never inject an arbitrary column.
An absent, malformed, or unknown sort resolves to ``(None, True)`` and the
caller falls back to its default order rather than erroring.
"""

from __future__ import annotations

from typing import Mapping, Optional, Tuple


def parse_sort(
    sort: Optional[str],
    allowlist: Mapping[str, str],
) -> Tuple[Optional[str], bool]:
    """Resolve ``sort`` against ``allowlist``.

    Returns ``(column, descending)``. ``column`` is the mapped real column, or
    ``None`` when the sort is absent or its key is not in the allowlist.
    ``descending`` is True unless the direction is explicitly ``asc``.
    """
    if not sort:
        return None, True
    key, _, direction = sort.partition(":")
    column = allowlist.get(key.strip().lower())
    if column is None:
        return None, True
    descending = direction.strip().lower() != "asc"
    return column, descending


__all__ = ["parse_sort"]
