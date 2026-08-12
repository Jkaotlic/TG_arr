"""Contract tests for the Prowlarr client."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.models import ContentType


@pytest.mark.asyncio
async def test_anime_searches_the_tv_categories():
    """Anime is not a Prowlarr category of its own — it lives in TV (5000s)."""
    from bot.clients.prowlarr import TV_CATEGORIES, ProwlarrClient

    client = ProwlarrClient("http://prowlarr", "key")
    with patch.object(client, "_do_search", new=AsyncMock(return_value=[])) as do_search:
        await client.search("Frieren", content_type=ContentType.ANIME)

    params = do_search.call_args.args[0]
    assert params["categories"] == TV_CATEGORIES
    assert params["query"] == "Frieren"


@pytest.mark.asyncio
async def test_search_retries_once_on_timeout_then_succeeds():
    """RuTracker behind Cloudflare stalls; the retry turns it into a success."""
    import httpx

    from bot.clients.prowlarr import ProwlarrClient

    client = ProwlarrClient("http://prowlarr", "key")
    attempts = []

    async def flaky(params, timeout, log, attempt):
        attempts.append(attempt)
        if attempt == 1:
            raise httpx.TimeoutException("stalled")
        return []

    with patch.object(client, "_do_search", new=flaky), \
         patch("bot.clients.prowlarr.asyncio.sleep", new=AsyncMock()):
        result = await client.search("Dune", content_type=ContentType.MOVIE)

    assert result == []
    assert attempts == [1, 2]


@pytest.mark.asyncio
async def test_search_raises_after_all_attempts_time_out():
    """When every attempt times out the user must get a clear failure."""
    import httpx

    from bot.clients.base import ServiceConnectionError
    from bot.clients.prowlarr import ProwlarrClient

    client = ProwlarrClient("http://prowlarr", "key")

    async def always_timeout(params, timeout, log, attempt):
        raise httpx.TimeoutException("stalled")

    with patch.object(client, "_do_search", new=always_timeout), \
         patch("bot.clients.prowlarr.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(ServiceConnectionError, match="Таймаут Prowlarr"):
            await client.search("Dune")


# ============================================================================
# Characterization tests — review round 1 finding: _normalize_result,
# _parse_quality, _extract_year, _extract_season_episode, _is_season_pack and
# _do_search's response handling were exercised by none of the three contract
# tests above (all patch _do_search itself out of the call path). These pin
# down what the RESTORED code actually does today, not what it should do —
# no production code was changed to make them pass.
# ============================================================================


def test_normalize_result_maps_a_realistic_prowlarr_item():
    """A realistic Prowlarr search hit normalises into the fields the rest
    of the bot reads off SearchResult."""
    from datetime import datetime, timezone

    from bot.clients.prowlarr import ProwlarrClient

    client = ProwlarrClient("http://prowlarr", "key")
    item = {
        "guid": "https://rutracker.org/forum/viewtopic.php?t=123456",
        "title": "Dune.2021.2160p.BluRay.REMUX.HEVC.DTS-HD.MA.5.1-GROUP",
        "downloadUrl": "https://rutracker.org/download/123456.torrent",
        "infoUrl": "https://rutracker.org/forum/viewtopic.php?t=123456",
        "indexer": "RuTracker.org",
        "indexerId": 5,
        "size": 47000000000,
        "seeders": 42,
        "leechers": 3,
        "protocol": "Torrent",
        "categories": [{"id": 2040, "name": "Movies/UHD"}],
        "publishDate": "2024-01-15T10:30:00Z",
    }

    result = client._normalize_result(item)

    assert result is not None
    assert result.guid == item["guid"]
    assert result.download_url == item["downloadUrl"]
    assert result.indexer == "RuTracker.org"
    assert result.indexer_id == 5
    # Fix round 1 (2026-08-10 review): this indexer_id is PROWLARR's own
    # numbering, not *arr's — origin="prowlarr" is what keeps
    # AddService.grab_release from handing it to *arr's native endpoint.
    # Fix round 2: renamed source -> origin (collided with QualityInfo.source,
    # which this same file also asserts on, e.g. quality.source == "BluRay").
    assert result.origin == "prowlarr"
    assert result.size == 47000000000
    assert result.seeders == 42
    assert result.leechers == 3
    assert result.protocol == "torrent"
    assert result.categories == [2040]
    assert result.category_names == ["Movies/UHD"]
    assert result.detected_type == ContentType.MOVIE
    assert result.publish_date == datetime(2024, 1, 15, 10, 30, tzinfo=timezone.utc)


def test_normalize_result_drops_item_without_any_guid_like_field():
    """No guid, downloadUrl, or infoUrl at all — nothing to key the release on."""
    from bot.clients.prowlarr import ProwlarrClient

    client = ProwlarrClient("http://prowlarr", "key")
    item = {"title": "Some.Movie.2024.1080p.WEB-DL", "size": 1000}

    assert client._normalize_result(item) is None


def test_normalize_result_drops_item_without_any_title_like_field():
    """Neither title nor fileName present."""
    from bot.clients.prowlarr import ProwlarrClient

    client = ProwlarrClient("http://prowlarr", "key")
    item = {"guid": "abc123", "size": 1000}

    assert client._normalize_result(item) is None


def test_normalize_result_handles_a_magnet_only_item():
    """No downloadUrl, only a magnet link — common on public trackers."""
    from bot.clients.prowlarr import ProwlarrClient

    client = ProwlarrClient("http://prowlarr", "key")
    item = {
        "guid": "magnet:?xt=urn:btih:ABCDEF1234567890",
        "title": "Some.Series.S01E01.720p.HDTV.x264-GROUP",
        "magnetUrl": "magnet:?xt=urn:btih:ABCDEF1234567890",
        "indexer": "The Pirate Bay",
    }

    result = client._normalize_result(item)

    assert result is not None
    assert result.magnet_url == item["magnetUrl"]
    assert result.download_url is None
    assert result.protocol == "torrent"
    assert result.indexer == "The Pirate Bay"


def test_parse_quality_2160p_via_explicit_marker():
    from bot.clients.prowlarr import ProwlarrClient

    client = ProwlarrClient("http://prowlarr", "key")
    quality = client._parse_quality("Movie.Name.2020.2160p.BluRay.x265-GROUP")

    assert quality.resolution == "2160p"
    assert quality.source == "BluRay"
    assert quality.codec == "x265"


def test_parse_quality_2160p_via_4k_marker():
    from bot.clients.prowlarr import ProwlarrClient

    client = ProwlarrClient("http://prowlarr", "key")
    quality = client._parse_quality("Interstellar.2014.4K.WEB-DL.x264-GROUP")

    assert quality.resolution == "2160p"
    assert quality.source == "WEB-DL"


def test_parse_quality_1080p_webrip():
    from bot.clients.prowlarr import ProwlarrClient

    client = ProwlarrClient("http://prowlarr", "key")
    quality = client._parse_quality("Show.Name.2023.1080p.WEBRip.x264-GROUP")

    assert quality.resolution == "1080p"
    assert quality.source == "WEBRip"


def test_parse_quality_720p_hdtv():
    from bot.clients.prowlarr import ProwlarrClient

    client = ProwlarrClient("http://prowlarr", "key")
    quality = client._parse_quality("Show.S01E01.720p.HDTV.x264-GROUP")

    assert quality.resolution == "720p"
    assert quality.source == "HDTV"


def test_parse_quality_bluray_remux():
    from bot.clients.prowlarr import ProwlarrClient

    client = ProwlarrClient("http://prowlarr", "key")
    quality = client._parse_quality("Movie.2020.2160p.BluRay.REMUX.HEVC-GROUP")

    assert quality.source == "BluRay"
    assert quality.is_remux is True
    assert quality.codec == "x265"


def test_parse_quality_camrip():
    from bot.clients.prowlarr import ProwlarrClient

    client = ProwlarrClient("http://prowlarr", "key")
    quality = client._parse_quality("Movie.Name.2024.CAMRip.XVID-GROUP")

    assert quality.source == "CAM"
    assert quality.resolution is None
    assert quality.codec == "XviD"


def test_parse_quality_russian_tracker_title():
    """Real Russian-tracker naming: dual title, bracketed translation marker."""
    from bot.clients.prowlarr import ProwlarrClient

    client = ProwlarrClient("http://prowlarr", "key")
    title = "Дюна: Часть вторая / Dune: Part Two (2024) BDRip 1080p [RusSub]"
    quality = client._parse_quality(title)

    assert quality.resolution == "1080p"
    assert quality.source == "BluRay"
    assert quality.subtitle == "RusSub"


def test_extract_year_finds_year_in_parens():
    from bot.clients.prowlarr import ProwlarrClient

    client = ProwlarrClient("http://prowlarr", "key")
    year = client._extract_year("Дюна: Часть вторая / Dune: Part Two (2024) BDRip 1080p")

    assert year == 2024


def test_extract_year_ignores_resolution_digits():
    """1080p is a 4-digit number followed by a letter — must not be read as a year."""
    from bot.clients.prowlarr import ProwlarrClient

    client = ProwlarrClient("http://prowlarr", "key")
    year = client._extract_year("Movie.Name.1080p.BluRay.x264-GROUP")

    assert year is None


def test_extract_year_ignores_an_out_of_range_four_digit_number():
    """A bare 4-digit number outside 1900-2100 (e.g. a pack/episode count)
    is rejected by the year-range guard even when it sits in a year-shaped
    position (surrounded by dots)."""
    from bot.clients.prowlarr import ProwlarrClient

    client = ProwlarrClient("http://prowlarr", "key")
    year = client._extract_year("Movie.Name.9999.BluRay.x264-GROUP")

    assert year is None


def test_extract_season_episode_s01e05():
    from bot.clients.prowlarr import ProwlarrClient

    client = ProwlarrClient("http://prowlarr", "key")

    assert client._extract_season_episode("Show.Name.S01E05.720p.HDTV.x264-GROUP") == (1, 5)


def test_extract_season_episode_season_only():
    from bot.clients.prowlarr import ProwlarrClient

    client = ProwlarrClient("http://prowlarr", "key")

    assert client._extract_season_episode("Show.Name.S01.Complete.1080p.WEB-DL") == (1, None)


def test_extract_season_episode_1x05_form():
    from bot.clients.prowlarr import ProwlarrClient

    client = ProwlarrClient("http://prowlarr", "key")

    assert client._extract_season_episode("Show.Name.1x05.480p.HDTV") == (1, 5)


def test_extract_season_episode_recognizes_cyrillic_season_episode_words():
    """Раньше здесь стоял характеризационный тест, фиксировавший обратное: что
    парсер русских форм «Сезон»/«Серия» НЕ понимает, потому что кириллические
    «с»/«е»/«х» — другие кодпоинты, чем латинские "s"/"e"/"x". Он был честен:
    дырка существовала. Безвредной она была ровно потому, что недостижима —
    единственный код, заполняющий эти поля, `ProwlarrClient.search()`, не имел
    производственного вызова.

    Свободный поиск (2026-08-12) вызов ему даёт, а русскоязычные индексеры —
    основной источник таких заголовков, так что дырку закрыли.
    """
    from bot.clients.prowlarr import ProwlarrClient

    client = ProwlarrClient("http://prowlarr", "key")

    assert client._extract_season_episode("Сериал.Сезон 1.Серия 5.1080p.WEB-DL") == (1, 5)


def test_is_season_pack_true_for_a_season_without_episode_marker():
    from bot.clients.prowlarr import ProwlarrClient

    client = ProwlarrClient("http://prowlarr", "key")

    assert client._is_season_pack("Show.Name.S01.Complete.1080p.WEB-DL") is True


def test_is_season_pack_false_for_a_single_episode():
    from bot.clients.prowlarr import ProwlarrClient

    client = ProwlarrClient("http://prowlarr", "key")

    assert client._is_season_pack("Show.Name.S01E05.720p.HDTV.x264-GROUP") is False


@pytest.mark.asyncio
async def test_do_search_non_list_response_returns_empty_list():
    """A non-list JSON payload (an error object, a dict) must not raise —
    just no results."""
    import httpx

    from bot.clients.prowlarr import ProwlarrClient

    client = ProwlarrClient("http://prowlarr", "key")
    transport = AsyncMock()
    transport.request = AsyncMock(return_value=httpx.Response(200, json={"error": "nope"}))

    with patch.object(client, "_get_client", new=AsyncMock(return_value=transport)):
        result = await client._do_search({"query": "x"}, 10.0, MagicMock(), attempt=1)

    assert result == []


@pytest.mark.asyncio
async def test_do_search_drops_one_malformed_item_keeps_the_rest():
    """One item that blows up during normalization (a non-numeric size)
    must not sink the whole batch."""
    import httpx

    from bot.clients.prowlarr import ProwlarrClient

    client = ProwlarrClient("http://prowlarr", "key")
    good_item = {"guid": "g1", "title": "Good.Movie.2020.1080p.BluRay.x264-GROUP", "size": 1000, "seeders": 5}
    bad_item = {"guid": "g2", "title": "Bad.Item", "size": "not-a-number"}
    transport = AsyncMock()
    transport.request = AsyncMock(return_value=httpx.Response(200, json=[good_item, bad_item]))

    with patch.object(client, "_get_client", new=AsyncMock(return_value=transport)):
        result = await client._do_search({"query": "x"}, 10.0, MagicMock(), attempt=1)

    assert len(result) == 1
    assert result[0].guid == "g1"


@pytest.mark.asyncio
async def test_empty_categories_list_falls_through_to_content_type_routing():
    """Minor finding, characterization only: `if categories:` treats an
    explicit categories=[] as falsy, so it falls through to content_type
    routing instead of sending an empty category filter. Inherited from the
    pre-migration file; no current caller passes []. Not changed here."""
    from bot.clients.prowlarr import MOVIE_CATEGORIES, ProwlarrClient

    client = ProwlarrClient("http://prowlarr", "key")
    with patch.object(client, "_do_search", new=AsyncMock(return_value=[])) as do_search:
        await client.search("Dune", content_type=ContentType.MOVIE, categories=[])

    params = do_search.call_args.args[0]
    assert params["categories"] == MOVIE_CATEGORIES


# ---------------------------------------------------------------------------
# Кириллица в разборе сезонов (2026-08-12).
#
# Этот код был безвреден ровно потому, что недостижим: ProwlarrClient.search()
# не имел производственного вызова. Свободный поиск его оживляет, а
# русскоязычные индексеры — основной источник таких заголовков.
# ---------------------------------------------------------------------------


def _prowlarr():
    from bot.clients.prowlarr import ProwlarrClient

    return ProwlarrClient("http://prowlarr", "key")


@pytest.mark.parametrize("title,season", [
    ("Друзья / Friends (Сезон 3) 1080p", 3),
    ("Ведьмак 2 сезон WEB-DL", 2),
    ("Тайны следствия 12-й сезон", 12),
    ("Сериал Сезон: 4", 4),
])
def test_cyrillic_season_is_extracted(title, season):
    assert _prowlarr()._extract_season_episode(title)[0] == season


def test_cyrillic_episode_is_extracted():
    assert _prowlarr()._extract_season_episode("Шоу Серия 5")[1] == 5


def test_cyrillic_season_and_episode_together():
    assert _prowlarr()._extract_season_episode("Шоу Сезон 2 Серия 7") == (2, 7)


@pytest.mark.parametrize("title", [
    "Сериал (01-12 из 12) 1080p",
    "Сериал 3 сезон целиком",
    "Сериал 2 сезон, все серии",
])
def test_cyrillic_season_pack_markers(title):
    assert _prowlarr()._is_season_pack(title) is True


def test_single_cyrillic_episode_is_not_a_pack():
    assert _prowlarr()._is_season_pack("Сериал 2 сезон Серия 5") is False


@pytest.mark.parametrize("title,expected", [
    ("Show S03E05 1080p", (3, 5)),
    ("Show S03 1080p", (3, None)),
    ("Show Season 2 Episode 4", (2, 4)),
    ("Show 1x01", (1, 1)),
])
def test_latin_parsing_is_unchanged(title, expected):
    """Страховка от регрессии: латинские паттерны имеют приоритет и не тронуты."""
    assert _prowlarr()._extract_season_episode(title) == expected


def test_latin_pack_detection_is_unchanged():
    client = _prowlarr()
    assert client._is_season_pack("Show S03 Complete Season") is True
    assert client._is_season_pack("Show S03E05") is False
