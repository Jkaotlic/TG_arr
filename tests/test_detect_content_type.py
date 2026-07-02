"""Regression tests for detect_content_type / parse_query (raund 3, BUG-01..08)."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from bot.clients.base import ServiceConnectionError
from bot.models import ArtistInfo, ContentType, MovieInfo, SeriesInfo
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


# ---------------------------------------------------------------------------
# TEST-02: detection under partial-service-failure (real prod case — one *arr
# is 503 while the others are alive and should still be able to classify).
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_partial_failure_radarr_down_sonarr_matches_returns_series():
    """Radarr raises, Sonarr is alive and matches strongly → SERIES, not UNKNOWN."""
    radarr = AsyncMock()
    radarr.lookup_movie = AsyncMock(side_effect=Exception("Radarr 503"))
    sonarr = AsyncMock()
    sonarr.lookup_series = AsyncMock(
        return_value=[SeriesInfo(tvdb_id=1, title="Breaking Bad")]
    )
    lidarr = AsyncMock()
    lidarr.lookup_artist = AsyncMock(return_value=[])
    prowlarr = AsyncMock()

    svc = SearchService(prowlarr, radarr, sonarr, lidarr=lidarr)
    result = await svc.detect_with_confidence("Breaking Bad")
    assert result.content_type == ContentType.SERIES


@pytest.mark.asyncio
async def test_partial_failure_lidarr_down_movie_matches_returns_movie():
    """Lidarr raises, Radarr matches strongly → MOVIE, not UNKNOWN."""
    radarr = AsyncMock()
    radarr.lookup_movie = AsyncMock(
        return_value=[MovieInfo(title="Interstellar", tmdb_id=157336, year=2014)]
    )
    sonarr = AsyncMock()
    sonarr.lookup_series = AsyncMock(return_value=[])
    lidarr = AsyncMock()
    lidarr.lookup_artist = AsyncMock(side_effect=Exception("Lidarr 503"))
    prowlarr = AsyncMock()

    svc = SearchService(prowlarr, radarr, sonarr, lidarr=lidarr)
    result = await svc.detect_with_confidence("Interstellar")
    assert result.content_type == ContentType.MOVIE


@pytest.mark.asyncio
async def test_detect_timeout_returns_unknown_zero_confidence():
    """A lookup that never resolves within _DETECT_TIMEOUT_S → UNKNOWN, confidence 0."""
    import bot.services.search_service as search_service_mod

    async def _never_returns(*_args, **_kwargs):
        await asyncio.sleep(999)
        return []

    radarr = AsyncMock()
    radarr.lookup_movie = AsyncMock(side_effect=_never_returns)
    sonarr = AsyncMock()
    sonarr.lookup_series = AsyncMock(side_effect=_never_returns)
    prowlarr = AsyncMock()

    svc = SearchService(prowlarr, radarr, sonarr)
    orig_timeout = search_service_mod._DETECT_TIMEOUT_S
    search_service_mod._DETECT_TIMEOUT_S = 0.05
    try:
        result = await svc.detect_with_confidence("Some Unresolvable Query")
    finally:
        search_service_mod._DETECT_TIMEOUT_S = orig_timeout

    assert result.content_type == ContentType.UNKNOWN
    assert result.confidence == 0.0


# ---------------------------------------------------------------------------
# PERF-01: detection-burst guard — module-level semaphore(2), TTL cache (300s)
# by normalized query, and a 30s circuit-breaker per service after a
# ServiceConnectionError/503. This is the fix for the prod incident where a
# single free-text message fires concurrent Radarr+Sonarr+Lidarr lookups.
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _reset_detection_module_state():
    """Each test gets a clean cache/circuit-breaker/semaphore state."""
    import bot.services.search_service as search_service_mod

    search_service_mod._DETECTION_CACHE.clear()
    search_service_mod._CIRCUIT_BREAKER.clear()
    yield
    search_service_mod._DETECTION_CACHE.clear()
    search_service_mod._CIRCUIT_BREAKER.clear()


@pytest.mark.asyncio
async def test_repeated_detect_hits_cache_lookup_called_once():
    """A repeated identical (normalized) query must not re-trigger lookups."""
    svc = _svc(movies=[MovieInfo(title="Dune", tmdb_id=1, year=2021)])

    await svc.detect_with_confidence("Dune 2021")
    await svc.detect_with_confidence("  dune   2021  ")  # normalizes to same key

    assert svc.radarr.lookup_movie.await_count == 1


@pytest.mark.asyncio
async def test_circuit_breaker_skips_service_after_service_connection_error():
    """After Radarr raises ServiceConnectionError, detection must not call
    radarr.lookup_movie again for 30s — even for a different query."""
    radarr = AsyncMock()
    radarr.lookup_movie = AsyncMock(side_effect=ServiceConnectionError("Radarr down", status_code=503))
    sonarr = AsyncMock()
    sonarr.lookup_series = AsyncMock(return_value=[])
    prowlarr = AsyncMock()

    svc = SearchService(prowlarr, radarr, sonarr)

    await svc.detect_with_confidence("First Query Here")
    assert radarr.lookup_movie.await_count == 1

    # Different query — cache can't be the reason it's skipped this time.
    await svc.detect_with_confidence("Second Different Query")
    assert radarr.lookup_movie.await_count == 1  # breaker open — not called again


@pytest.mark.asyncio
async def test_semaphore_caps_concurrent_detection_lookups():
    """No more than 2 detection-lookup coroutines run concurrently, globally."""
    active = 0
    max_active = 0
    lock = asyncio.Lock()
    release_event = asyncio.Event()

    async def _tracked_lookup(*_args, **_kwargs):
        nonlocal active, max_active
        async with lock:
            active += 1
            max_active = max(max_active, active)
        await release_event.wait()
        async with lock:
            active -= 1
        return []

    services = []
    for _ in range(3):
        radarr = AsyncMock()
        radarr.lookup_movie = AsyncMock(side_effect=_tracked_lookup)
        sonarr = AsyncMock()
        sonarr.lookup_series = AsyncMock(side_effect=_tracked_lookup)
        prowlarr = AsyncMock()
        services.append(SearchService(prowlarr, radarr, sonarr))

    tasks = [
        asyncio.create_task(svc.detect_with_confidence(f"Unique Query {i}"))
        for i, svc in enumerate(services)
    ]
    # Give tasks a chance to queue up against the semaphore.
    await asyncio.sleep(0.05)
    release_event.set()
    await asyncio.gather(*tasks)

    assert max_active <= 2


# ---------------------------------------------------------------------------
# TEST-17: empty Prowlarr response must not crash search_releases.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_search_releases_empty_prowlarr_response_returns_empty_list():
    svc = _svc()
    svc.prowlarr.search = AsyncMock(return_value=[])
    out = await svc.search_releases("nonexistent query xyz", ContentType.MOVIE)
    assert out == []
