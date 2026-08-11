"""R71.2 — minimal in-process query cache.

Used by analytics_helpers.inject_canned_result_set to pre-populate query
results so analytics pytest fixtures can run without hitting a real DB.
The SUT's real analytics layer is patched at test time; this module
exists only to satisfy `from src.analytics import cache` imports.
"""
from typing import Any

_STORE: dict[str, Any] = {}


def set(query: str, result: Any) -> None:
    """Store `result` under `query`. Overwrites any prior value."""
    _STORE[query] = result


def get(query: str, default: Any = None) -> Any:
    """Return the cached result for `query`, or `default` if absent."""
    return _STORE.get(query, default)


def has(query: str) -> bool:
    """True iff `query` has a cached result."""
    return query in _STORE


def delete(query: str) -> None:
    """Remove `query` from the cache. No-op if absent."""
    _STORE.pop(query, None)


def clear() -> None:
    """Empty the cache. Useful for test teardown."""
    _STORE.clear()
