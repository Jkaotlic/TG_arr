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

import re

from bot.models import QualityInfo

#: Первый токен имени качества у Lidarr, если это аудио. Не «все возможные
#: форматы», а те, что реально приходят от Lidarr'овских профилей — плюс ALAC,
#: APE, OGG и Opus, которые он тоже умеет отдавать.
_AUDIO_FORMATS = frozenset({
    "flac", "mp3", "aac", "wav", "wma", "wavpack", "alac", "ape", "ogg",
    "opus", "vorbis",
})


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

    # Аудио. Живой замер 2026-08-12 (127 раздач одного альбома в Lidarr 3.1.2):
    # имена приходят как «FLAC», «FLAC 24bit», «MP3-320», «AAC-VBR», «WavPack»,
    # «WMA», «Unknown». Ни одно из них не описывается видео-полями ниже, поэтому
    # без этой ветки у музыкальной раздачи качество терялось целиком — а формат
    # у музыки и есть главный различитель. Имя сохраняется как есть: «MP3-320»
    # и «MP3-128» — разные вещи, и обрезать битрейт нельзя.
    if name_lower.split("-")[0].split()[0] in _AUDIO_FORMATS:
        return QualityInfo(codec=name)

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


# Audit 2026-09-23 GRAB-17: tokens, not substrings. "hdr" inside "HDRip",
# "cam" inside "Camera", "4k" inside a group tag all used to match. A token
# is bounded by anything that is not a letter or digit (dots, dashes,
# brackets, spaces, "|").
_TOKEN_4K_RE = re.compile(r"(?<![a-z0-9])(?:4k|uhd)(?![a-z0-9])")
_CAM_RE = re.compile(r"(?<![a-z0-9])(?:hd)?cam(?:rip)?(?![a-z0-9])")
_HDR10P_RE = re.compile(r"(?<![a-z0-9])hdr10(?:\+|plus)")
_HDR10_RE = re.compile(r"(?<![a-z0-9])hdr10(?![a-z0-9])")
_HDR_RE = re.compile(r"(?<![a-z0-9])hdr(?![a-z0-9])")
_DV_RE = re.compile(r"(?<![a-z0-9])(?:dv|dovi)(?![a-z0-9])|dolby[\s.]vision")
_TS_RE = re.compile(r"(?<![a-z0-9])(?:ts|hdts|telesync)(?![a-z0-9])")
_TC_RE = re.compile(r"(?<![a-z0-9])(?:tc|telecine)(?![a-z0-9])")
_SUB_RE = re.compile(r"rus[.\s\-_]?subs?|russian[.\s\-_]?subs?|(?<![a-z0-9])multi[.\s\-_]?subs?")
#: Voice-over (закадровый перевод / дубляж) — audio, not subtitles. The bot
#: showed "💬 Субтитры: MVO", which reads as the opposite of what it is.
_VOICE_RES = (
    ("MVO", re.compile(r"(?<![a-z0-9])mvo(?![a-z0-9])")),
    ("DVO", re.compile(r"(?<![a-z0-9])dvo(?![a-z0-9])")),
    ("AVO", re.compile(r"(?<![a-z0-9])avo(?![a-z0-9])")),
    ("Dub", re.compile(r"(?<![a-zа-я0-9])(?:dub|dubbed|дубляж|дублированный)(?![a-zа-я0-9])")),
)


def parse_title_quality(title: str) -> QualityInfo:
    """Quality from a raw release title — the Prowlarr free-text path, and
    the fields *arr's short quality name does not carry (codec, HDR/DV,
    audio, voice-over, subtitles) on the *arr path.

    Moved here from ``ProwlarrClient._parse_quality`` (which now delegates)
    so both paths share one parser (audit 2026-09-23 GRAB-10/GRAB-17).
    """
    t = (title or "").lower()

    resolution = None
    if "2160p" in t or _TOKEN_4K_RE.search(t):
        resolution = "2160p"
    elif "1080p" in t:
        resolution = "1080p"
    elif "720p" in t:
        resolution = "720p"
    elif "480p" in t:
        resolution = "480p"
    elif "576p" in t:
        resolution = "576p"

    # "BDRemux"/"BD-Remux" is how Russian trackers name a BluRay remux
    # (audit 2026-07-30).
    source = None
    if any(m in t for m in ("bluray", "blu-ray", "bdrip", "bdremux", "bd-remux", "bdmux")):
        source = "BluRay"
    elif "web-dl" in t or "webdl" in t:
        source = "WEB-DL"
    elif "webrip" in t or "web-rip" in t:
        source = "WEBRip"
    elif "hdtv" in t:
        source = "HDTV"
    elif "dvdrip" in t or "dvd-rip" in t:
        source = "DVDRip"
    elif _CAM_RE.search(t):
        source = "CAM"
    elif _TS_RE.search(t):
        source = "TS"
    elif _TC_RE.search(t):
        source = "TC"

    codec = None
    if "x265" in t or "hevc" in t or "h.265" in t or "h265" in t:
        codec = "x265"
    elif "x264" in t or "h.264" in t or "h264" in t:
        codec = "x264"
    elif "av1" in t:
        codec = "AV1"
    elif "xvid" in t:
        codec = "XviD"
    elif "divx" in t:
        codec = "DivX"

    hdr = "DV" if _DV_RE.search(t) else None
    for pattern, name in ((_HDR10P_RE, "HDR10+"), (_HDR10_RE, "HDR10"), (_HDR_RE, "HDR")):
        if pattern.search(t):
            hdr = name if hdr is None else f"{hdr}+{name}"
            break

    audio = None
    if "atmos" in t:
        audio = "Atmos"
    elif "truehd" in t or "true-hd" in t:
        audio = "TrueHD"
    elif "dts-hd" in t or "dtshd" in t:
        audio = "DTS-HD"
    elif "dts" in t:
        audio = "DTS"
    elif "dd5.1" in t or "dd 5.1" in t or "dd.5.1" in t or "ac3" in t:
        audio = "DD5.1"
    elif "aac" in t:
        audio = "AAC"

    subtitle = None
    sub = _SUB_RE.search(t)
    if sub:
        subtitle = "MultiSub" if sub.group(0).startswith("multi") else "RusSub"

    voice = next((name for name, pattern in _VOICE_RES if pattern.search(t)), None)

    return QualityInfo(
        resolution=resolution,
        source=source,
        codec=codec,
        hdr=hdr,
        audio=audio,
        subtitle=subtitle,
        voice=voice,
        is_remux="remux" in t,
        is_repack="repack" in t or "rerip" in t,
        is_proper="proper" in t,
    )


def enrich_from_title(quality: QualityInfo, title: str) -> QualityInfo:
    """*arr's quality name ("WEBDL-2160p") says resolution and source only.
    Fill codec, HDR/DV, audio, voice-over and subtitles from the title where
    *arr left them empty; never override what *arr said (GRAB-10)."""
    parsed = parse_title_quality(title)
    updates = {
        field: getattr(parsed, field)
        for field in ("codec", "hdr", "audio", "subtitle", "voice")
        if getattr(quality, field) is None and getattr(parsed, field) is not None
    }
    return quality.model_copy(update=updates) if updates else quality




# Round 2 / D3: multi-episode releases. "S01E04-E06" / "S01E04-06" are a
# range, "S01E04E05E06" lists every episode. The range needs a non-alnum after
# its last number, so "S01E05-720p" / "S01E05-4HD" stay a single episode (same
# rule as the Prowlarr pack detector). Fix round 1: a spaced dash needs an
# "E" ("S01E04 - E06"); "S01E04 - 12 из 20" is episode 4, not 4–12. A bare
# number is a range end only after a tight dash ("S01E04-06").
_EP_RANGE_RE = re.compile(
    r"s(\d{1,2})\s*e(\d{1,3})(?:\s*-\s*e(\d{1,3})|-(\d{1,3}))(?![0-9a-zа-яё])"
)
_EP_CHAIN_RE = re.compile(r"s\d{1,2}((?:e\d{1,3}){2,})(?![0-9])")
_EP_SINGLE_RE = re.compile(r"s\d{1,2}\s*e(\d{1,3})")
#: A longer "range" is more likely a misparse than one release.
_MAX_EPISODE_SPAN = 100


def parse_episode_numbers(title: str) -> list[int]:
    """Every episode number a release title covers, ascending; [] if none."""
    lower = title.lower()
    match = _EP_RANGE_RE.search(lower)
    if match:
        first, last = int(match.group(2)), int(match.group(3) or match.group(4))
        if first < last <= first + _MAX_EPISODE_SPAN:
            return list(range(first, last + 1))
    match = _EP_CHAIN_RE.search(lower)
    if match:
        return sorted({int(n) for n in re.findall(r"e(\d{1,3})", match.group(1))})
    match = _EP_SINGLE_RE.search(lower)
    if match:
        return [int(match.group(1))]
    return []
