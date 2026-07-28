"""Regression tests for detect_content_type / parse_query (raund 3, BUG-01..08)."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from bot.models import ArtistInfo, ContentType, MovieInfo, SeriesInfo
from bot.services.search_service import SearchService


def _scryer(*, movies=None, series=None, anime=None, fail=None):
    """A Scryer client whose metadata search returns the given fixtures."""
    client = AsyncMock()
    if fail is not None:
        client.search_metadata_multi = AsyncMock(side_effect=fail)
    else:
        client.search_metadata_multi = AsyncMock(return_value={
            ContentType.MOVIE: movies or [],
            ContentType.SERIES: series or [],
            ContentType.ANIME: anime or [],
        })
    return client


def _svc(*, movies=None, series=None, anime=None, artists=None, lidarr_enabled=True, scryer=None):
    """Build a SearchService over a mocked Scryer (+ optional Lidarr).

    Migration 2026-07-28: the video side is one `searchMetadataMulti` call
    rather than parallel Radarr/Sonarr lookups.
    """
    lidarr = None
    if lidarr_enabled:
        lidarr = AsyncMock()
        lidarr.lookup_artist = AsyncMock(return_value=artists or [])

    return SearchService(
        scryer or _scryer(movies=movies, series=series, anime=anime),
        lidarr=lidarr,
    )


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
async def test_logic06_definitive_movie_winner_carries_lookup_results():
    """LOGIC-06: a confident MOVIE winner must carry the full MovieInfo
    objects in `lookup_results` (not just titles in `candidates`) so callers
    can skip a second Radarr lookup for the same query."""
    movie = MovieInfo(title="Interstellar", tmdb_id=157336, year=2014)
    svc = _svc(movies=[movie])
    result = await svc.detect_with_confidence("Interstellar 2014")
    assert result.content_type == ContentType.MOVIE
    assert result.lookup_results == [movie]


@pytest.mark.asyncio
async def test_logic06_definitive_series_winner_carries_lookup_results():
    """Series counterpart of the above."""
    series = SeriesInfo(title="Stranger Things", tvdb_id=305288, year=2016)
    svc = _svc(series=[series])
    result = await svc.detect_with_confidence("Stranger Things S01")
    # Migration 2026-07-28: "S01" no longer short-circuits before the lookup —
    # it only narrows the candidates to series-vs-anime, so the winner still
    # carries its metadata (which the caller needs to add the title).
    assert result.content_type == ContentType.SERIES
    assert result.lookup_results == [series]


@pytest.mark.asyncio
async def test_logic06_low_confidence_unknown_has_empty_lookup_results():
    """UNKNOWN/ambiguous results must not carry stale lookup_results — the
    caller asks the user, there is no "winning" content_type to attach them to."""
    svc = _svc(movies=[], series=[], artists=[])
    result = await svc.detect_with_confidence("zzz_no_such_thing_zzz")
    assert result.content_type == ContentType.UNKNOWN
    assert result.lookup_results == []


@pytest.mark.asyncio
async def test_bug05_all_lookups_failing_returns_unknown():
    """BUG-05: when every backend raises, return UNKNOWN — don't silently
    treat empty results as 'music' or 'no match'."""
    lidarr = AsyncMock()
    lidarr.lookup_artist = AsyncMock(side_effect=Exception("Lidarr down"))

    svc = SearchService(_scryer(fail=Exception("Scryer down")), lidarr=lidarr)
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
async def test_search_releases_keeps_every_candidate_scryer_returned():
    """LOGIC-04 (migrated): the bot does not drop releases on its own — Scryer
    already decided what is allowed, and mis-tagged Russian-tracker releases
    must stay visible so the user can force one."""
    from bot.models import SearchResult, QualityInfo

    allowed = SearchResult(
        guid="g1", indexer="X", title="Movie 2024 1080p",
        size=10, quality=QualityInfo(resolution="1080p"),
        scryer_allowed=True, scryer_score=100, scryer_title_id="t1",
    )
    blocked = SearchResult(
        guid="g2", indexer="X", title="Movie 2024 720p",
        size=10, quality=QualityInfo(resolution="720p"),
        scryer_allowed=False, scryer_score=-1000, scryer_title_id="t1",
    )

    svc = _svc()
    svc.scryer.search_releases = AsyncMock(return_value=[allowed, blocked])

    out = await svc.search_releases("t1", ContentType.MOVIE)
    titles = [r.title for r in out]
    assert "Movie 2024 720p" in titles  # blocked, but not dropped
    assert titles[0] == "Movie 2024 1080p"  # allowed ranks first


# ---------------------------------------------------------------------------
# TEST-02: detection under partial-service-failure (real prod case — one *arr
# is 503 while the others are alive and should still be able to classify).
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_metadata_failure_returns_unknown_not_a_wrong_guess():
    """TEST-02 (migrated): when Scryer's metadata search fails there is nothing
    left to classify with — ask the user instead of guessing."""
    svc = _svc(scryer=_scryer(fail=Exception("Scryer 503")))
    result = await svc.detect_with_confidence("Breaking Bad")
    assert result.content_type == ContentType.UNKNOWN
    assert result.confidence == 0.0


@pytest.mark.asyncio
async def test_partial_failure_music_backend_down_movie_still_wins():
    """Lidarr raises, Scryer matches strongly → MOVIE, not UNKNOWN."""
    lidarr = AsyncMock()
    lidarr.lookup_artist = AsyncMock(side_effect=Exception("Lidarr 503"))

    svc = SearchService(
        _scryer(movies=[MovieInfo(title="Interstellar", tmdb_id=157336, year=2014)]),
        lidarr=lidarr,
    )
    result = await svc.detect_with_confidence("Interstellar")
    assert result.content_type == ContentType.MOVIE


@pytest.mark.asyncio
async def test_detect_timeout_returns_unknown_zero_confidence():
    """A lookup that never resolves within _DETECT_TIMEOUT_S → UNKNOWN, confidence 0."""
    import bot.services.search_service as search_service_mod

    async def _never_returns(*_args, **_kwargs):
        await asyncio.sleep(999)
        return {}

    svc = _svc(scryer=_scryer(fail=None))
    svc.scryer.search_metadata_multi = AsyncMock(side_effect=_never_returns)

    orig_timeout = search_service_mod._DETECT_TIMEOUT_S
    search_service_mod._DETECT_TIMEOUT_S = 0.05
    try:
        result = await svc.detect_with_confidence("Some Unresolvable Query")
    finally:
        search_service_mod._DETECT_TIMEOUT_S = orig_timeout

    assert result.content_type == ContentType.UNKNOWN
    assert result.confidence == 0.0


# ---------------------------------------------------------------------------
# PERF-01: the detection-burst guard. The semaphore and the per-service circuit
# breaker were removed with the migration — they existed because one free-text
# message fanned out concurrent Radarr+Sonarr+Lidarr lookups and could take all
# three down. Scryer answers every video facet in ONE call, so only the TTL
# cache (which stops a double-tap re-querying) is still needed.
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _reset_detection_module_state():
    """Each test gets a clean cache."""
    import bot.services.search_service as search_service_mod

    search_service_mod._cache_clear()
    yield
    search_service_mod._cache_clear()


@pytest.mark.asyncio
async def test_repeated_detect_hits_cache_lookup_called_once():
    """A repeated identical (normalized) query must not re-trigger lookups."""
    svc = _svc(movies=[MovieInfo(title="Dune", tmdb_id=1, year=2021)])

    await svc.detect_with_confidence("Dune 2021")
    await svc.detect_with_confidence("  dune   2021  ")  # normalizes to same key

    assert svc.scryer.search_metadata_multi.await_count == 1


@pytest.mark.asyncio
async def test_detection_is_a_single_upstream_call():
    """The whole point of the migration: movie/series/anime in one round-trip."""
    svc = _svc(movies=[MovieInfo(title="Dune", tmdb_id=1, year=2021)])

    await svc.detect_with_confidence("Dune 2021")

    assert svc.scryer.search_metadata_multi.await_count == 1


@pytest.mark.asyncio
async def test_search_releases_empty_response_returns_empty_list():
    """TEST-17: an empty release list must not crash search_releases."""
    svc = _svc()
    svc.scryer.search_releases = AsyncMock(return_value=[])
    out = await svc.search_releases("title-id", ContentType.MOVIE)
    assert out == []
