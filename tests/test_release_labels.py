"""Audit 2026-09-23 B6: release button labels, the release card and the
title parser (GRAB-10, GRAB-11, GRAB-17).

- Label ``N. 2160p Remux 🇬🇧💬 ⛔ 58.3 GB S:12``: the full size with its unit
  ("123.4 MB" and "123.4 GB" used to both read "123.4 "), the source, the
  language flags *arr parsed, ⛔ for a rejected release. Under Telegram's
  width, seeders go first; size and ⛔ are never cut.
- The card shows languages, matched custom formats, and HDR/DV/codec parsed
  from the title when *arr's quality name does not carry them.
- Parser: HDRip is not HDR; "cam" only as a token; "4k" only as a token;
  MVO/DVO/AVO/Dub are voice-over, not subtitles.
"""

import pytest

from bot.models import QualityInfo, SearchResult

GB = 1024 ** 3
MB = 1024 ** 2


def _res(**kw) -> SearchResult:
    data = dict(guid="g", origin="arr", title="Some.Movie.2021", size=0)
    data.update(kw)
    return SearchResult(**data)


# ------------------------------------------------------------------ labels
def test_label_full_shape():
    from bot.ui.formatters._common import release_button_label

    r = _res(
        size=int(58.3 * GB), seeders=12, rejected=True, languages=["English"],
        quality=QualityInfo(resolution="2160p", source="BluRay", is_remux=True, subtitle="RusSub"),
    )
    assert release_button_label(r, 0) == "1. 2160p Remux 🇬🇧💬 ⛔ 58.3 GB S:12"


def test_label_distinguishes_mb_from_gb():
    from bot.ui.formatters._common import release_button_label

    mb = release_button_label(_res(size=int(123.4 * MB), quality=QualityInfo(resolution="720p")), 4)
    gb = release_button_label(_res(size=int(123.4 * GB), quality=QualityInfo(resolution="720p")), 4)
    assert "123.4 MB" in mb
    assert "123.4 GB" in gb
    assert mb.startswith("5. 720p")


@pytest.mark.parametrize("source,expected", [
    ("WEB-DL", "WEB"), ("WEBRip", "WEB"), ("BluRay", "BD"), ("HDTV", "HDTV"),
])
def test_label_source_short_names(source, expected):
    from bot.ui.formatters._common import release_button_label

    label = release_button_label(_res(size=GB, quality=QualityInfo(resolution="1080p", source=source)), 0)
    assert label.startswith(f"1. 1080p {expected} ")


def test_label_language_flags():
    from bot.ui.formatters._common import release_button_label

    label = release_button_label(
        _res(size=GB, languages=["Russian", "English", "Klingon"], quality=QualityInfo(resolution="1080p")), 0,
    )
    assert "🇷🇺🇬🇧" in label


def test_long_label_drops_seeders_before_size_or_verdict():
    from bot.ui.formatters._common import LABEL_MAX, release_button_label

    r = _res(
        size=int(58.3 * GB), seeders=1234, rejected=True,
        languages=["English", "Russian", "Japanese", "French", "German", "Spanish"],
        quality=QualityInfo(resolution="2160p", source="BluRay", is_remux=True, subtitle="RusSub"),
    )
    label = release_button_label(r, 122)
    assert len(label) <= LABEL_MAX
    assert "S:1234" not in label
    assert label.endswith("⛔ 58.3 GB")
    assert label.startswith("123. 2160p")


def test_short_label_keeps_seeders():
    from bot.ui.formatters._common import release_button_label

    label = release_button_label(_res(size=GB, seeders=7, quality=QualityInfo(resolution="1080p")), 0)
    assert label == "1. 1080p 1.0 GB S:7"


def test_search_keyboard_uses_the_label():
    from bot.ui.keyboards import Keyboards

    r = _res(size=int(123.4 * GB), quality=QualityInfo(resolution="720p"))
    kb = Keyboards.search_results([r], 0, 1, list_token="abc123")
    assert kb.inline_keyboard[0][0].text == "1. 720p 123.4 GB"


def test_free_search_keyboard_uses_the_label():
    from bot.ui.keyboards import Keyboards

    r = _res(origin="prowlarr", size=int(123.4 * MB), quality=QualityInfo(resolution="720p"))
    kb = Keyboards.free_results([r], 0, 1, list_token="abc123")
    assert kb.inline_keyboard[0][0].text == "1. 720p 123.4 MB"


# ------------------------------------------------------------------ parser
def _parse(title: str) -> QualityInfo:
    from bot.services.release_parser import parse_title_quality

    return parse_title_quality(title)


def test_hdrip_is_not_hdr():
    q = _parse("Movie.2020.HDRip.1080p.x264")
    assert q.hdr is None


def test_hdr_tokens_still_parse():
    assert _parse("Movie.2020.2160p.WEB-DL.HDR.x265").hdr == "HDR"
    assert _parse("Movie.2020.2160p.WEB-DL.HDR10+.x265").hdr == "HDR10+"
    assert _parse("Movie.2020.2160p.WEB-DL.DV.HDR10.x265").hdr == "DV+HDR10"


def test_cam_only_as_a_token():
    assert _parse("Camera.Obscura.2020.1080p.WEB-DL").source == "WEB-DL"
    assert _parse("American.Cameo.2020.720p").source is None
    assert _parse("Movie.2024.CAMRip.XviD").source == "CAM"
    assert _parse("Movie.2024.CAM.x264").source == "CAM"
    assert _parse("Movie 2024 HDCAM").source == "CAM"


def test_4k_only_as_a_token():
    assert _parse("Movie.2020.4K.HDR.x265").resolution == "2160p"
    assert _parse("Movie.2020.UHD.BluRay").resolution == "2160p"
    assert _parse("The.4Kids.Show.S01.720p").resolution == "720p"
    assert _parse("Movie.2020.x264-G4KX").resolution is None


@pytest.mark.parametrize("title,voice", [
    ("Дюна (2021) WEB-DL 1080p MVO", "MVO"),
    ("Movie.2021.1080p.DVO.x264", "DVO"),
    ("Movie 2021 1080p AVO", "AVO"),
    ("Movie.2021.1080p.Dub.x264", "Dub"),
    ("Movie (2021) BDRip 1080p | Дубляж", "Dub"),
])
def test_voice_over_is_not_subtitles(title, voice):
    q = _parse(title)
    assert q.voice == voice
    assert q.subtitle is None


def test_subtitles_still_parse_alongside_voice():
    q = _parse("Дюна / Dune (2024) BDRip 1080p MVO [RusSub]")
    assert q.subtitle == "RusSub"
    assert q.voice == "MVO"


def test_prowlarr_client_uses_the_shared_parser():
    from bot.clients.prowlarr import ProwlarrClient

    q = ProwlarrClient("http://prowlarr.example.com", "key")._parse_quality("Movie.2020.HDRip.MVO")
    assert q.hdr is None and q.voice == "MVO"


# ---------------------------------------------------- *arr path enrichment
def test_arr_release_gets_hdr_and_codec_from_the_title():
    from bot.clients.radarr import RadarrClient

    client = RadarrClient("http://radarr.example.com", "key")
    r = client._parse_release({
        "guid": "g1", "title": "Movie.2021.2160p.WEB-DL.DV.HDR10.DDP5.1.Atmos.H.265-GRP",
        "quality": {"quality": {"name": "WEBDL-2160p"}},
        "languages": [{"name": "English"}], "customFormats": [{"name": "English"}],
    })
    assert r is not None
    assert r.quality.resolution == "2160p"
    assert r.quality.source == "WEB-DL"
    assert r.quality.hdr == "DV+HDR10"
    assert r.quality.codec == "x265"


def test_arr_music_release_keeps_its_format_codec():
    from bot.clients.radarr import RadarrClient

    client = RadarrClient("http://radarr.example.com", "key")
    r = client._parse_release({
        "guid": "g2", "title": "Artist - Album (2020) [MP3 320]",
        "quality": {"quality": {"name": "FLAC"}},
    })
    assert r is not None and r.quality.codec == "FLAC"


# ------------------------------------------------------------------ card
def test_card_shows_languages_custom_formats_and_voice():
    from bot.ui.formatters import Formatters

    r = _res(
        title="Movie.2021.1080p.WEB-DL.MVO",
        languages=["English", "Russian"], custom_formats=["English", "RusSubs"],
        quality=QualityInfo(resolution="1080p", voice="MVO"),
    )
    text = Formatters.format_release_details(r)
    assert "English, Russian" in text
    assert "RusSubs" in text
    assert "🎙" in text and "MVO" in text
