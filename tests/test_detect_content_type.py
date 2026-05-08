"""Regression tests for detect_content_type / parse_query (raund 3, BUG-01..08)."""

from unittest.mock import AsyncMock

import pytest

from bot.models import ArtistInfo, ContentType, MovieInfo
from bot.services.search_service import SearchService


def _svc(*, movies=None, series=None, artists=None, lidarr_enabled=True):
    """Build a SearchService where Radarr/Sonarr/Lidarr lookups return fixtures."""
    radarr = AsyncMock()
    radarr.lookup_movie = AsyncMock(return_value=movies or [])
    sonarr = AsyncMock()
    sonarr.lookup_series = AsyncMock(return_value=series or [])

    lidarr = None
    if lidarr_enabled:
        lidarr = AsyncMock()
        lidarr.lookup_artist = AsyncMock(return_value=artists or [])

    prowlarr = AsyncMock()
    return SearchService(prowlarr, radarr, sonarr, lidarr=lidarr)


@pytest.mark.asyncio
async def test_bug03_movie_with_year_is_not_classified_as_music():
    """BUG-03 / LOGIC-03: 'Avatar 2009' must NOT pick MUSIC even if there's an
    artist named 'Avatar' in Lidarr — query has a year, music dropped."""
    svc = _svc(
        movies=[MovieInfo(title="Avatar", tmdb_id=19995, year=2009)],
        series=[],
        artists=[ArtistInfo(mb_id="x", name="Avatar")],
    )
    result = await svc.detect_with_confidence("Avatar 2009")
    assert result.content_type == ContentType.MOVIE


@pytest.mark.asyncio
async def test_bug01_substring_does_not_pick_music():
    """BUG-01 / LOGIC-02: short ambiguous query must not auto-pick music when
    movie/series candidates also match. 'Joker' should not silently go to music."""
    svc = _svc(
        movies=[MovieInfo(title="Joker", tmdb_id=475557, year=2019)],
        artists=[ArtistInfo(mb_id="y", name="Joker")],
    )
    result = await svc.detect_with_confidence("Joker")
    # Either MOVIE wins outright or low confidence → UNKNOWN (user asked).
    assert result.content_type in (ContentType.MOVIE, ContentType.UNKNOWN)
    assert result.content_type != ContentType.MUSIC


@pytest.mark.asyncio
async def test_bug05_all_lookups_failing_returns_unknown():
    """BUG-05: when Radarr/Sonarr/Lidarr all raise, return UNKNOWN — don't
    silently treat empty results as 'music' or 'no match'."""
    radarr = AsyncMock()
    radarr.lookup_movie = AsyncMock(side_effect=Exception("Radarr down"))
    sonarr = AsyncMock()
    sonarr.lookup_series = AsyncMock(side_effect=Exception("Sonarr down"))
    lidarr = AsyncMock()
    lidarr.lookup_artist = AsyncMock(side_effect=Exception("Lidarr down"))
    prowlarr = AsyncMock()

    svc = SearchService(prowlarr, radarr, sonarr, lidarr=lidarr)
    result = await svc.detect_with_confidence("Whatever Movie")
    assert result.content_type == ContentType.UNKNOWN
    assert result.confidence == 0.0


@pytest.mark.asyncio
async def test_series_pattern_short_circuit():
    """If query contains S01E05 we must classify as series before any lookup."""
    svc = _svc(movies=[], series=[])
    result = await svc.detect_with_confidence("Breaking Bad S01E05")
    assert result.content_type == ContentType.SERIES


@pytest.mark.asyncio
async def test_low_confidence_returns_unknown():
    """LOGIC-28: when no lookup matches strongly, return UNKNOWN so the user
    is presented the type-question."""
    # All services empty
    svc = _svc(movies=[], series=[], artists=[])
    result = await svc.detect_with_confidence("bzzz qwerty 12345")
    assert result.content_type == ContentType.UNKNOWN


def test_parse_query_keeps_year_in_original():
    """BUG-06 / LOGIC-05: parse_query strips the year from `title` (for lookup
    APIs) but exposes it in `year`. The handler is responsible for sending the
    *original* query to Prowlarr so the year survives."""
    svc = _svc()
    parsed = svc.parse_query("Blade Runner 2049")
    assert parsed["year"] == 2049
    assert "2049" not in parsed["title"]
    assert parsed["original"] == "Blade Runner 2049"


def test_parse_query_extracts_season_episode():
    svc = _svc()
    parsed = svc.parse_query("Mr. Robot S01E03 1080p")
    assert parsed["season"] == 1
    assert parsed["episode"] == 3
    assert parsed["quality"] == "1080p"


def test_parse_query_strips_quality_token():
    """BUG-29 / BUG-30: 4k and Cyrillic 4К removed from title."""
    svc = _svc()
    parsed = svc.parse_query("Дюна 4К 2160p")
    assert parsed["quality"] == "2160p"
    assert "4К" not in parsed["title"]
    assert "2160p" not in parsed["title"]


@pytest.mark.asyncio
async def test_search_releases_no_longer_filters_by_detected_type(monkeypatch):
    """LOGIC-04: results with mismatched detected_type are kept (Russian trackers
    mis-tag categories). Filtering is removed; scoring orders them."""
    from bot.models import SearchResult, QualityInfo

    movie_result = SearchResult(
        guid="g1", indexer="X", indexer_id=1, title="Movie 2024 1080p",
        size=10, quality=QualityInfo(resolution="1080p"),
        detected_type=ContentType.MOVIE,
    )
    misc_result = SearchResult(
        guid="g2", indexer="X", indexer_id=1, title="Movie 2024 720p",
        size=10, quality=QualityInfo(resolution="720p"),
        detected_type=ContentType.SERIES,  # mis-tagged
    )

    svc = _svc()
    svc.prowlarr.search = AsyncMock(return_value=[movie_result, misc_result])

    out = await svc.search_releases("Movie 2024", ContentType.MOVIE)
    titles = [r.title for r in out]
    assert "Movie 2024 720p" in titles  # mis-tagged result NOT dropped
