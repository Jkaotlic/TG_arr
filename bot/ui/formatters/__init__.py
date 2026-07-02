"""Message formatters for Telegram bot.

All output uses HTML parse_mode. User-provided content is escaped via html.escape().

This is a package split by domain (search/torrent/emby/calendar) that used to be
a single ~1000-line module. The public API is unchanged: `Formatters` still
exposes every `format_*` (and `_to_local`/`_safe_truncate`/etc.) static method
it always did — callers do `from bot.ui.formatters import Formatters` exactly
as before.
"""

from datetime import datetime
from typing import Optional

from bot.ui.formatters._common import (
    _e,
    _get_cached_zoneinfo,
    _progress_bar,
    _safe_truncate,
    _to_local,
)
from bot.ui.formatters.calendar import _extract_date_key, _format_date_header
from bot.ui.formatters.emby import _EmbyFormatters
from bot.ui.formatters.search import _SearchFormatters
from bot.ui.formatters.torrent import _TorrentFormatters

__all__ = ["Formatters"]


class Formatters(_SearchFormatters, _TorrentFormatters, _EmbyFormatters):
    """Message formatting utilities — HTML mode.

    Domain-specific formatters live in mixins (see search.py/torrent.py/emby.py);
    calendar formatting stays here directly (see the module docstring in
    bot/ui/formatters/calendar.py for why `format_calendar` can't move out).
    """

    # Re-exposed so every name that used to live directly on the pre-split
    # `Formatters` class (bot/ui/formatters.py) is still reachable as
    # `Formatters.<name>` — full 1:1 attribute-surface compatibility.
    _get_cached_zoneinfo = staticmethod(_get_cached_zoneinfo)
    _to_local = staticmethod(_to_local)
    _safe_truncate = staticmethod(_safe_truncate)
    _progress_bar = staticmethod(_progress_bar)
    _extract_date_key = staticmethod(_extract_date_key)
    _format_date_header = staticmethod(_format_date_header)

    # =========================================================================
    # Calendar / Schedule Formatting
    # =========================================================================

    @staticmethod
    def format_calendar(
        episodes: list[dict],
        movies: list[dict],
        days: int = 7,
        albums: Optional[list[dict]] = None,
    ) -> str:
        """Format combined calendar for Sonarr/Radarr/Lidarr."""
        albums = albums or []
        lines = [f"📅 <b>Календарь релизов</b> ({days} дн.)\n"]

        if not episodes and not movies and not albums:
            lines.append("Нет предстоящих релизов.")
            return "\n".join(lines)

        # BUG-06: "today" must be the local calendar day (settings.timezone),
        # not the UTC day — between 00:00-03:00 MSK, UTC is still "yesterday"
        # and a same-day release would be mislabelled "tomorrow".
        from bot.config import get_settings

        tz = Formatters._get_cached_zoneinfo(get_settings().timezone)
        today = datetime.now(tz).date()

        if episodes:
            lines.append(f"📺 <b>Сериалы ({len(episodes)})</b>")
            by_date: dict[str, list[dict]] = {}
            for ep in episodes:
                date_key = _extract_date_key(ep.get("air_date", ""))
                by_date.setdefault(date_key, []).append(ep)

            for date_key in sorted(by_date.keys()):
                date_header = _format_date_header(date_key, today)
                lines.append(f"\n  📆 <b>{date_header}</b>")
                for ep in by_date[date_key]:
                    s = ep.get("season", 0)
                    e = ep.get("episode", 0)
                    series = _e(ep.get("series_title", "?"))
                    ep_title = _e(ep.get("title", ""))
                    status = "✅" if ep.get("has_file") else "⏳"
                    ep_label = f"S{s:02d}E{e:02d}"
                    line = f"  {status} <b>{series}</b> {ep_label}"
                    if ep_title:
                        line += f" — {ep_title}"
                    lines.append(line)

        if movies:
            if episodes:
                lines.append("")
            lines.append(f"🎬 <b>Фильмы ({len(movies)})</b>")
            by_date: dict[str, list[dict]] = {}
            for m in movies:
                date_key = _extract_date_key(m.get("release_date", ""))
                by_date.setdefault(date_key, []).append(m)

            for date_key in sorted(by_date.keys()):
                date_header = _format_date_header(date_key, today)
                lines.append(f"\n  📆 <b>{date_header}</b>")
                for m in by_date[date_key]:
                    title = _e(m.get("title", "?"))
                    year = m.get("year", "")
                    year_str = f" ({year})" if year else ""
                    status = "✅" if m.get("has_file") else ("📀" if m.get("is_available") else "⏳")
                    runtime = m.get("runtime", 0)
                    runtime_str = f" • {runtime} мин" if runtime else ""

                    release_types = []
                    if m.get("digital_release"):
                        release_types.append("💾 цифровой")
                    if m.get("physical_release"):
                        release_types.append("📀 физический")
                    if m.get("in_cinemas"):
                        release_types.append("🎥 кино")
                    type_str = f" [{', '.join(release_types)}]" if release_types else ""

                    lines.append(f"  {status} <b>{title}</b>{year_str}{runtime_str}{type_str}")

        if albums:
            if episodes or movies:
                lines.append("")
            lines.append(f"🎵 <b>Музыка ({len(albums)})</b>")
            by_date: dict[str, list[dict]] = {}
            for a in albums:
                date_key = _extract_date_key(a.get("release_date", ""))
                by_date.setdefault(date_key, []).append(a)

            for date_key in sorted(by_date.keys()):
                date_header = _format_date_header(date_key, today)
                lines.append(f"\n  📆 <b>{date_header}</b>")
                for a in by_date[date_key]:
                    artist = _e(a.get("artist_name", "?"))
                    title = _e(a.get("title", "?"))
                    album_type = a.get("album_type", "")
                    type_str = f" [{_e(album_type)}]" if album_type else ""
                    status = "✅" if a.get("has_file") else "⏳"
                    lines.append(f"  {status} <b>{artist}</b> — {title}{type_str}")

        result = "\n".join(lines)
        return Formatters._safe_truncate(result, max_len=3800)
