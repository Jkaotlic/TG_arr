"""Shared helpers for message formatters — HTML escaping, timezone conversion,
progress bars and safe truncation. Used by every domain formatter module.
"""

import html
from datetime import datetime, timezone, tzinfo
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
_ZONEINFO_CACHE: dict[str, tzinfo] = {}


def _get_cached_zoneinfo(tz_name: str) -> tzinfo:
    """Return a cached ZoneInfo for `tz_name`, falling back to UTC if the
    IANA database doesn't know it (DEAD-14: single plain except clause).
    """
    cached = _ZONEINFO_CACHE.get(tz_name)
    if cached is not None:
        return cached
    tz: tzinfo
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


# ---------------------------------------------------------------------------
# Release list buttons (audit 2026-09-23 GRAB-10/GRAB-11)
# ---------------------------------------------------------------------------

#: Telegram shows about this many characters of an inline button on a phone.
LABEL_MAX = 40

#: *arr language names → flags for the list button. Unknown names are left
#: out of the button (the card lists every language by name).
_LANGUAGE_FLAGS = {
    "english": "🇬🇧", "russian": "🇷🇺", "ukrainian": "🇺🇦", "japanese": "🇯🇵",
    "french": "🇫🇷", "german": "🇩🇪", "spanish": "🇪🇸", "italian": "🇮🇹",
    "korean": "🇰🇷", "chinese": "🇨🇳", "portuguese": "🇵🇹", "polish": "🇵🇱",
}

_SOURCE_SHORT = {
    "BluRay": "BD", "WEB-DL": "WEB", "WEBRip": "WEB", "HDTV": "HDTV",
    "DVDRip": "DVD", "CAM": "CAM", "TS": "TS", "TC": "TC",
}


def release_button_label(result, idx: int) -> str:
    """``N. 2160p Remux 🇬🇧💬 ⛔ 58.3 GB S:12`` for one release button.

    The size keeps its unit — ``size_formatted[:6]`` used to render
    "123.4 MB" and "123.4 GB" identically (GRAB-11). Language flags come
    from *arr's ``languages``, 💬 from parsed subtitles or a subtitle custom
    format, ⛔ marks a release *arr rejects (GRAB-10).

    Over ``LABEL_MAX``: seeders go first, then flags one by one, then the
    head is shortened. Size and ⛔ are never cut.
    """
    quality = result.quality
    head = f"{idx + 1}. {quality.resolution or '?'}"
    source = "Remux" if quality.is_remux else _SOURCE_SHORT.get(quality.source or "", quality.source or "")
    if source:
        head += f" {source}"

    flags = [
        _LANGUAGE_FLAGS[name.casefold()]
        for name in (getattr(result, "languages", None) or [])
        if isinstance(name, str) and name.casefold() in _LANGUAGE_FLAGS
    ]
    subs = bool(quality.subtitle) or any(
        "sub" in cf.casefold() for cf in (getattr(result, "custom_formats", None) or [])
    )

    tail_parts = []
    if result.rejected:
        tail_parts.append("⛔")
    if result.size > 0:
        tail_parts.append(result.size_formatted)
    tail = " ".join(tail_parts)
    seeders = f"S:{result.seeders}" if result.seeders else ""

    def build(with_seeders: bool) -> str:
        marks = "".join(flags) + ("💬" if subs else "")
        parts = [head, marks, tail, seeders if with_seeders else ""]
        return " ".join(p for p in parts if p)

    label = build(True)
    if len(label) <= LABEL_MAX:
        return label
    label = build(False)
    while len(label) > LABEL_MAX and flags:
        flags.pop()
        label = build(False)
    if len(label) <= LABEL_MAX:
        return label
    room = LABEL_MAX - len(tail) - 1
    short_head = head[: max(room - 1, 1)].rstrip() + "…"
    return f"{short_head} {tail}" if tail else short_head


#: Human names of Sonarr's season-monitoring presets (audit UI-15: the card
#: used to show "latestSeason").
SEASON_PRESET_NAMES = {
    "all": "все сезоны",
    "future": "только будущие",
    "firstSeason": "первый сезон",
    "latestSeason": "последний сезон",
    "none": "нет",
}


def season_preset_name(preset: str | None) -> str:
    """Russian name of a season preset; ``None`` = decided by the release."""
    if preset is None:
        return "авто (по релизу)"
    return SEASON_PRESET_NAMES.get(preset, preset)
