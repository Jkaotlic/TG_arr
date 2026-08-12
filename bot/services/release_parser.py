"""Release-title parsing: quality, year, season/episode, season-pack.

Migration 2026-07-28: this logic used to live on `ProwlarrClient` — the bot
parsed every raw Prowlarr result itself.

Rollback 2026-08-10: two callers now, with different needs. Radarr/Sonarr's
own interactive search already returns a short, pre-parsed quality name
(`quality.quality.name`, e.g. "Bluray-2160p") — `parse_quality_name` below is
just a thin adapter onto `QualityInfo`, not a second heuristic; *arr's own
value always wins. Prowlarr's free-text search has no such structured value
at all, so `parse_quality` (title-heuristic parsing, unchanged since the
2026-07-28 migration) remains the only source of quality info on that path.
"""

from bot.models import QualityInfo


def parse_quality_name(name: str) -> QualityInfo:
    """Map *arr's own parsed quality name (e.g. "Bluray-2160p") onto QualityInfo.

    Rollback 2026-08-10: Radarr/Sonarr's interactive search (`GET .../release`)
    already ran its own parser and returns `quality.quality.name` — this is
    just a thin adapter onto our model, not a second heuristic. `parse_quality`
    below (title-heuristic parsing) still exists for the Prowlarr free-text
    path, which gets no such pre-parsed value.
    """
    if not name:
        return QualityInfo()

    name_lower = name.lower()

    resolution = None
    for token in ("2160p", "1080p", "720p", "480p", "576p"):
        if token in name_lower:
            resolution = token
            break

    source = None
    if "remux" in name_lower:
        source = "BluRay"
    elif "bluray" in name_lower or "bdrip" in name_lower:
        source = "BluRay"
    elif "webdl" in name_lower or "web-dl" in name_lower:
        source = "WEB-DL"
    elif "webrip" in name_lower:
        source = "WEBRip"
    elif "hdtv" in name_lower:
        source = "HDTV"
    elif "dvd" in name_lower:
        source = "DVDRip"

    return QualityInfo(
        resolution=resolution,
        source=source,
        is_remux="remux" in name_lower,
    )


