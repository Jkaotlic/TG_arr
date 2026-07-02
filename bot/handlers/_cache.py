"""Shared bounded-cache helpers for per-user/per-key module-level state.

LOGIC-21: ``music.py``, ``trending.py`` and ``calendar.py`` each grew their
own small mechanism for capping an in-memory ``dict`` so a long-running bot
process doesn't accumulate unbounded per-user state. Two of them
(``music.py``'s ``_artist_candidates``/``_trending_artists_cache`` and
``trending.py``'s ``_trending_movies_cache``/``_trending_series_cache``)
share the same core contract — evict the single oldest entry on overflow,
never ``clear()`` the whole dict (that was BUG-10/PERF-12: a clear() wipes
every other user's in-flight selection, not just the stale one).

This module factors that shared contract into plain functions that operate
on caller-owned ``dict`` instances, so each handler module keeps its own
module-level cache variables (and therefore its own identity for tests that
poke the dict directly) while sharing the eviction/TTL logic.

Deliberately *not* a class wrapping the dict: several existing tests reach
into the module-level cache dicts directly (``trending._trending_movies_cache``,
``music._artist_candidates``, ...) and call ``.clear()``/``in``/subscript on
them as plain dicts. Keeping the public dicts as plain ``dict[...]`` avoids
having to touch those tests while still deduplicating the eviction logic.
"""

from __future__ import annotations

import time
from typing import Any


def evict_lru(cache: dict[Any, Any], max_size: int) -> None:
    """Evict the oldest entries (dict insertion order) until ``cache`` has
    room for one more item, i.e. ``len(cache) < max_size``.

    BUG-10/PERF-12: pops the single oldest key at a time instead of
    ``clear()``-ing the whole cache on overflow.
    """
    while len(cache) >= max_size:
        cache.pop(next(iter(cache)))


def remember_lru(cache: dict[Any, Any], key: Any, value: Any, max_size: int) -> None:
    """Insert/refresh ``key`` in ``cache``, evicting the oldest entry when at
    capacity, keeping ``key`` as the freshest entry for eviction ordering.

    Pops ``key`` first (if present) so a refresh also moves it to the end of
    iteration order — CPython dicts preserve insertion order, so "oldest" is
    always ``next(iter(cache))``.
    """
    cache.pop(key, None)
    evict_lru(cache, max_size)
    cache[key] = value


def put_ttl(
    cache: dict[Any, Any],
    timestamps: dict[Any, float],
    key: Any,
    value: Any,
    max_size: int,
) -> None:
    """Insert/refresh ``key`` in ``cache`` with an LRU-capacity cache plus a
    parallel ``timestamps`` side-table recording the insertion time (used by
    :func:`get_ttl` for TTL expiry).

    Same eviction contract as :func:`remember_lru`; additionally stamps the
    insertion time in ``timestamps`` (same keys as ``cache``).
    """
    cache.pop(key, None)
    timestamps.pop(key, None)
    evict_lru(cache, max_size)
    cache[key] = value
    timestamps[key] = time.monotonic()


def get_ttl(
    cache: dict[Any, Any],
    timestamps: dict[Any, float],
    key: Any,
    ttl_seconds: float,
) -> Any | None:
    """Look up ``key`` in ``cache``, treating TTL-expired entries as a miss
    (and dropping them from both ``cache`` and ``timestamps``).

    Entries with no recorded timestamp (e.g. inserted via a direct
    ``cache[key] = value`` that bypassed :func:`put_ttl`) are treated as
    always-fresh, matching the pre-refactor behaviour.
    """
    if key not in cache:
        return None
    inserted_at = timestamps.get(key)
    if inserted_at is not None and time.monotonic() - inserted_at > ttl_seconds:
        cache.pop(key, None)
        timestamps.pop(key, None)
        return None
    return cache[key]
