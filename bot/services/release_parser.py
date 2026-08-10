"""Release-title parsing: quality, year, season/episode, season-pack.

Migration 2026-07-28: this logic used to live on `ProwlarrClient` — the bot
parsed every raw Prowlarr result itself. Scryer now returns its own
`parsedRelease`, but only partially populated in practice (`videoCodec` and
`audio` come back null for most Russian-tracker releases), and the bot's
scoring — plus the Russian-audio/subtitle rules — depends on those fields.

So this stays as a *supplement*: `bot.clients.scryer` fills in whatever Scryer
reported, and these helpers fill the gaps from the release title. Scryer's own
values always win; nothing here overrides them.
"""

import re
from typing import Optional

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


def parse_quality(title: str) -> QualityInfo:
    """Parse quality information from release title."""
    title_lower = title.lower()

    # Resolution
    resolution = None
    if "2160p" in title_lower or "4k" in title_lower or "uhd" in title_lower:
        resolution = "2160p"
    elif "1080p" in title_lower:
        resolution = "1080p"
    elif "720p" in title_lower:
        resolution = "720p"
    elif "480p" in title_lower:
        resolution = "480p"
    elif "576p" in title_lower:
        resolution = "576p"

    # Source
    source = None
    # "BDRemux" / "BD-Remux" is how Russian trackers name a BluRay remux, and
    # those are a large share of what this stack actually sees — without it the
    # release card showed "Источник: —" for them (audit 2026-07-30).
    if any(
        marker in title_lower
        for marker in ("bluray", "blu-ray", "bdrip", "bdremux", "bd-remux", "bdmux")
    ):
        source = "BluRay"
    elif "web-dl" in title_lower or "webdl" in title_lower:
        source = "WEB-DL"
    elif "webrip" in title_lower or "web-rip" in title_lower:
        source = "WEBRip"
    elif "hdtv" in title_lower:
        source = "HDTV"
    elif "dvdrip" in title_lower or "dvd-rip" in title_lower:
        source = "DVDRip"
    elif any(x in title_lower for x in ["cam", "camrip", "hdcam"]):
        source = "CAM"
    elif any(x in title_lower for x in ["telesync", "hdts"]) or re.search(r"[\.\s\-\[]ts[\.\s\-\]]", title_lower):
        source = "TS"
    elif "telecine" in title_lower or re.search(r"[\.\s\-\[]tc[\.\s\-\]]", title_lower):
        source = "TC"

    # Codec
    codec = None
    if "x265" in title_lower or "hevc" in title_lower or "h.265" in title_lower or "h265" in title_lower:
        codec = "x265"
    elif "x264" in title_lower or "h.264" in title_lower or "h264" in title_lower:
        codec = "x264"
    elif "av1" in title_lower:
        codec = "AV1"
    elif "xvid" in title_lower:
        codec = "XviD"
    elif "divx" in title_lower:
        codec = "DivX"

    # HDR
    hdr = None
    if "dolby vision" in title_lower or "dolby.vision" in title_lower or re.search(r"[\.\s\-]dv[\.\s\-]|[\.\s\-]dv$", title_lower) or "dovi" in title_lower:
        hdr = "DV"
    if "hdr10+" in title_lower:
        hdr = "HDR10+" if hdr is None else f"{hdr}+HDR10+"
    elif "hdr10" in title_lower:
        hdr = "HDR10" if hdr is None else f"{hdr}+HDR10"
    elif "hdr" in title_lower:
        hdr = "HDR" if hdr is None else f"{hdr}+HDR"

    # Audio
    audio = None
    if "atmos" in title_lower:
        audio = "Atmos"
    elif "truehd" in title_lower or "true-hd" in title_lower:
        audio = "TrueHD"
    elif "dts-hd" in title_lower or "dtshd" in title_lower:
        audio = "DTS-HD"
    elif "dts" in title_lower:
        audio = "DTS"
    elif "dd5.1" in title_lower or "dd 5.1" in title_lower or "dd.5.1" in title_lower or "ac3" in title_lower:
        audio = "DD5.1"
    elif "aac" in title_lower:
        audio = "AAC"

    # Subtitles / Russian audio
    subtitle = None
    if re.search(r"rus[\.\s\-_]?sub|russian[\.\s\-_]?sub|russub", title_lower):
        subtitle = "RusSub"
    elif re.search(r"[\.\s\-_]mvo[\.\s\-_$]", title_lower):
        subtitle = "MVO"
    elif re.search(r"[\.\s\-_]dvo[\.\s\-_$]", title_lower):
        subtitle = "DVO"
    elif re.search(r"[\.\s\-_]avo[\.\s\-_$]", title_lower):
        subtitle = "AVO"
    elif re.search(r"multi[\.\s\-_]?sub", title_lower):
        subtitle = "MultiSub"

    # Remux
    is_remux = "remux" in title_lower

    # Repack/Proper
    is_repack = "repack" in title_lower or "rerip" in title_lower
    is_proper = "proper" in title_lower

    return QualityInfo(
        resolution=resolution,
        source=source,
        codec=codec,
        hdr=hdr,
        audio=audio,
        subtitle=subtitle,
        is_remux=is_remux,
        is_repack=is_repack,
        is_proper=is_proper,
    )

def extract_year(title: str) -> Optional[int]:
    """Extract year from title."""
    # Match year in various formats: (2021), [2021], .2021., 2021
    patterns = [
        r"[\(\[](\d{4})[\)\]]",  # (2021) or [2021]
        r"\.(\d{4})\.",  # .2021.
        r"\s(\d{4})\s",  # space 2021 space
        r"[\.\s](\d{4})$",  # ends with .2021 or space 2021
    ]
    for pattern in patterns:
        match = re.search(pattern, title)
        if match:
            year = int(match.group(1))
            if 1900 <= year <= 2100:
                return year
    return None

def extract_season_episode(title: str) -> tuple[Optional[int], Optional[int]]:
    """Extract season and episode numbers from title."""
    title_lower = title.lower()

    # S01E01 format
    match = re.search(r"s(\d{1,2})e(\d{1,3})", title_lower)
    if match:
        return int(match.group(1)), int(match.group(2))

    # S01 format (season only)
    match = re.search(r"s(\d{1,2})(?![e\d])", title_lower)
    if match:
        return int(match.group(1)), None

    # Season 1 Episode 2 format (supports dot/space separators)
    match = re.search(r"season[\s.]*(\d{1,2})(?:[\s.]*episode[\s.]*(\d{1,3}))?", title_lower)
    if match:
        season = int(match.group(1))
        episode = int(match.group(2)) if match.group(2) else None
        return season, episode

    # 1x01 format
    match = re.search(r"(\d{1,2})x(\d{1,3})", title_lower)
    if match:
        return int(match.group(1)), int(match.group(2))

    return None, None

def is_season_pack(title: str) -> bool:
    """Check if release is a season pack."""
    title_lower = title.lower()

    # Explicit season pack markers (check first — most reliable)
    if any(x in title_lower for x in ["complete season", "season pack", "full season"]):
        return True

    # Has any episode reference at all? If so, not a season pack.
    if re.search(r"s\d{1,2}e\d|(\d{1,2})x(\d{1,3})|episode[\s.]*\d", title_lower):
        return False

    # S01 format without any episode reference
    if re.search(r"s\d{1,2}(?:\b|[\.\s\-])", title_lower):
        return True

    # "Season X" without episode
    if re.search(r"season[\s.]*\d{1,2}(?![\s.]*episode)", title_lower):
        return True

    return False
