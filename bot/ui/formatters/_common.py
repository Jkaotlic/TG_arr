"""Shared helpers for message formatters — HTML escaping, timezone conversion,
progress bars and safe truncation. Used by every domain formatter module.
"""

import html
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo


def _e(text) -> str:
    """Escape HTML entities in user-provided text."""
    if not text:
        return ""
    return html.escape(str(text))


# BUG-06/DEAD-14: module-level ZoneInfo cache — constructing ZoneInfo() parses
# the IANA tzdata file; every formatted datetime used to pay that cost again
# (calendar headers alone do it once per distinct release date). One process
# only ever runs under a single configured TIMEZONE, so a tiny cache keyed by
# tz name is effectively a single cached object in practice.
_ZONEINFO_CACHE: dict[str, ZoneInfo] = {}


def _get_cached_zoneinfo(tz_name: str) -> ZoneInfo:
    """Return a cached ZoneInfo for `tz_name`, falling back to UTC if the
    IANA database doesn't know it (DEAD-14: single plain except clause).
    """
    cached = _ZONEINFO_CACHE.get(tz_name)
    if cached is not None:
        return cached
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    _ZONEINFO_CACHE[tz_name] = tz
    return tz


def _to_local(dt: Optional[datetime]) -> Optional[datetime]:
    """Convert `dt` to the configured settings.timezone (BUG-06).

    Naive datetimes are assumed UTC (every datetime written by this
    codebase is UTC — see bot/models.py `_utcnow`). Returns None
    unchanged so call-sites can keep their existing `if dt:` guards.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    from bot.config import get_settings

    tz = _get_cached_zoneinfo(get_settings().timezone)
    return dt.astimezone(tz)


def _progress_bar(progress: float, length: int = 20) -> str:
    """Create a text-based progress bar."""
    filled = int(length * progress)
    empty = length - filled
    return "█" * filled + "░" * empty


def _safe_truncate(text: str, max_len: int = 3800) -> str:
    """Truncate text without breaking HTML tags (BUG-12).

    Strategy: if text fits, return as-is. Otherwise cut at the last
    newline before ``max_len``. If no newline exists in the budget,
    fall back to a safe character-boundary cut that avoids unclosed
    ``<…>`` tags.
    """
    SUFFIX = "\n\n... (truncated)"
    if len(text) <= max_len:
        return text

    # Reserve space for the suffix
    budget = max_len - len(SUFFIX)
    if budget <= 0:
        return text[:max_len]

    candidate = text[:budget]
    cut_at = candidate.rfind("\n")
    if cut_at == -1:
        cut_at = budget

    piece = candidate[:cut_at]
    # Guard: if the piece ends inside an HTML tag ("<..." without ">"),
    # walk back to the last safe position.
    last_open = piece.rfind("<")
    last_close = piece.rfind(">")
    if last_open > last_close:
        piece = piece[:last_open]
    return piece + SUFFIX
