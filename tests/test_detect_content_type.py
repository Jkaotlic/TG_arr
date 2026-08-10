"""Regression tests for detect_content_type / parse_query.

Rollback 2026-08-10: detection fans out to Radarr/Sonarr/Lidarr in parallel
again instead of one Scryer `searchMetadataMulti` call — see
bot/services/search_service.py's module docstring for the full rationale.

Release-search coverage (search_releases) moved out of this file: that method
still depends on the removed Scryer client and is Task 9's responsibility
(see docs/superpowers/plans/2026-08-10-arr-restore.md) — it gets its own
tests/test_search_grab_flow.py coverage there.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from bot.models import ArtistInfo, ContentType, MovieInfo, SeriesInfo
from bot.services.search_service import SearchService


@pytest.fixture(autouse=True)
def _reset_detection_module_state():
    """Each test gets a clean cache and a closed circuit breaker.

    Both are module-level (shared across every SearchService instance), so
    without this a failure recorded by one test could trip the breaker for
    the next one. tests/conftest.py's process-wide autouse fixture calls the
    same `_reset_module_state()` helper — this local one is redundant with it
    but kept so this file's tests are self-contained if ever run in
    isolation with a different conftest.
    """
    import bot.services.search_service as search_service_mod

    search_service_mod._reset_module_state()
    yield
    search_service_mod._reset_module_state()


def _radarr(*, movies=None, fail=None):
    """A Radarr client whose movie lookup returns the given fixtures."""
    client = AsyncMock()
    if fail is not None:
        client.lookup_movie = AsyncMock(side_effect=fail)
    else:
        client.lookup_movie = AsyncMock(return_value=movies or [])
    return client


def _sonarr(*, series=None, fail=None):
    """A Sonarr client whose series lookup returns the given fixtures.

    `series` is Sonarr's real shape: ONE flat list mixing standard and
    Animation-genre results — detect_content_type is what splits them into
    series-vs-anime candidates, using the `genres` field.
    """
    client = AsyncMock()
    if fail is not None:
        client.lookup_series = AsyncMock(side_effect=fail)
    else:
        client.lookup_series = AsyncMock(return_value=series or [])
    return client


def _svc(
    *,
    movies=None,
    series=None,
    artists=None,
    lidarr_enabled=True,
    radarr=None,
    sonarr=None,
    radarr_fail=None,
    sonarr_fail=None,
    lidarr_fail=None,
):
    """Build a SearchService over mocked Radarr/Sonarr (+ optional Lidarr)."""
    lidarr = None
    if lidarr_enabled:
        lidarr = AsyncMock()
        if lidarr_fail is not None:
            lidarr.lookup_artist = AsyncMock(side_effect=lidarr_fail)
        else:
            lidarr.lookup_artist = AsyncMock(return_value=artists or [])

    return SearchService(
        radarr or _radarr(movies=movies, fail=radarr_fail),
        sonarr or _sonarr(series=series, fail=sonarr_fail),
        lidarr=lidarr,
    )


# ---------------------------------------------------------------------------
# Fix round 1 (code review), finding 1: the too_short guard (len < 2) must
# stay a real guard, not be weakened to fit a test's input. A 1-char query is
# a cheap way to trigger three external TMDb/TVDB/MusicBrainz lookups, and
# detect_content_type is a public method — it must not rely on the sole live
# caller (bot/handlers/search/commands.py) validating length first.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_too_short_query_does_not_touch_the_network():
    """A 1-char query must return UNKNOWN without calling any *arr lookup."""
    radarr = AsyncMock()
    sonarr = AsyncMock()
    svc = SearchService(radarr, sonarr)

    result = await svc.detect_content_type("X")

    assert result.content_type == ContentType.UNKNOWN
    radarr.lookup_movie.assert_not_awaited()
    sonarr.lookup_series.assert_not_awaited()


# ---------------------------------------------------------------------------
# Fix round 1 (code review), finding 2: `_CIRCUIT_BREAKER` is module-level,
# process-wide state. pytest runs the whole suite in one process, so a test
# in one file that trips the breaker for a service must not leave it open
# for an unrelated, later test in a *different* file — tests/conftest.py's
# autouse fixture now resets it via `_reset_module_state()` (same helper this
# file's own local fixture uses above).
# ---------------------------------------------------------------------------
def test_reset_module_state_clears_cache_and_breaker():
    """The one function conftest.py calls between every test must actually
    reset both pieces of shared state, not just the cache."""
    import bot.services.search_service as search_service_mod

    search_service_mod._cache_put("some-cache-key", "placeholder-result")
    for _ in range(3):
        search_service_mod._CIRCUIT_BREAKER.record_failure("radarr")
    assert search_service_mod._cache_get("some-cache-key") is not None
    assert search_service_mod._CIRCUIT_BREAKER.is_open("radarr")

    search_service_mod._reset_module_state()

    assert search_service_mod._cache_get("some-cache-key") is None
    assert not search_service_mod._CIRCUIT_BREAKER.is_open("radarr")


# ---------------------------------------------------------------------------
# Task 8 brief — mandatory TDD tests (verbatim scenarios).
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_detection_survives_one_dead_service():
    """Sonarr being down must not blank out a movie search."""
    radarr = AsyncMock()
    radarr.lookup_movie.return_value = [MovieInfo(tmdb_id=1, title="Dune", year=2021)]
    sonarr = AsyncMock()
    sonarr.lookup_series.side_effect = ConnectionError("Sonarr is down")

    service = SearchService(radarr, sonarr)
    result = await service.detect_content_type("Dune")

    assert result.content_type is ContentType.MOVIE


@pytest.mark.asyncio
async def test_detection_runs_the_three_lookups_concurrently():
    """Three sequential lookups would triple the user's wait.

    Uses a 2-char query ("XX") rather than a 1-char one: the too_short guard
    (len < 2) is production-critical — a 1-char query is a cheap way to
    trigger three external metadata lookups, and the guard must not be
    weakened just to make a single-character query exercise concurrency.
    """
    started = []

    async def slow_movie(query):
        started.append("movie")
        await asyncio.sleep(0.05)
        return [MovieInfo(tmdb_id=1, title="XX", year=2020)]

    async def slow_series(query):
        started.append("series")
        await asyncio.sleep(0.05)
        return []

    radarr, sonarr = AsyncMock(), AsyncMock()
    radarr.lookup_movie = slow_movie
    sonarr.lookup_series = slow_series

    service = SearchService(radarr, sonarr)
    start = asyncio.get_event_loop().time()
    await service.detect_content_type("XX")
    elapsed = asyncio.get_event_loop().time() - start

    assert set(started) == {"movie", "series"}
    assert elapsed < 0.09, "lookups ran sequentially"


@pytest.mark.asyncio
async def test_circuit_breaker_stops_hammering_a_failing_service():
    """After repeated failures the bot must stop calling the dead service."""
    from bot.services.search_service import _CIRCUIT_BREAKER

    _CIRCUIT_BREAKER.reset()
    radarr, sonarr = AsyncMock(), AsyncMock()
    radarr.lookup_movie.side_effect = ConnectionError("down")
    sonarr.lookup_series.return_value = []

    service = SearchService(radarr, sonarr)
    for _ in range(5):
        await service.detect_content_type(f"query {_}")

    calls_before = radarr.lookup_movie.await_count
    await service.detect_content_type("one more")
    assert radarr.lookup_movie.await_count == calls_before, "breaker did not open"


# ---------------------------------------------------------------------------
# BUG-04 (migrated again): CancelledError must not be swallowed by
# gather(return_exceptions=True) — that bug was fixed once already during the
# Scryer migration; _lookup_branch re-raises it explicitly.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cancelled_error_propagates_not_swallowed():
    radarr = AsyncMock()

    async def cancelled_lookup(*_a, **_kw):
        raise asyncio.CancelledError()

    radarr.lookup_movie = AsyncMock(side_effect=cancelled_lookup)
    sonarr = _sonarr(series=[])

    svc = SearchService(radarr, sonarr)
    with pytest.raises(asyncio.CancelledError):
        await svc.detect_content_type("anything at all")


# ---------------------------------------------------------------------------
# Anime now comes from genre evidence on Sonarr's one flat lookup_series
# list (no more separate Scryer facet) — the tie-break margin itself
# (_ANIME_OVER_SERIES_MARGIN) is unchanged, only the candidate source is new.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_animation_genre_series_is_classified_as_anime():
    """*arr has no anime facet: an 'Animation' genre on a lookup result is
    the only evidence before the title is added to the catalog."""
    frieren = SeriesInfo(
        title="Frieren: Beyond Journey's End",
        tvdb_id=1,
        year=2023,
        genres=["Animation", "Drama", "Fantasy"],
    )
    svc = _svc(series=[frieren])
    result = await svc.detect_content_type("Frieren")
    assert result.content_type == ContentType.ANIME


@pytest.mark.asyncio
async def test_non_animation_series_is_not_treated_as_anime():
    stranger = SeriesInfo(
        title="Stranger Things", tvdb_id=2, year=2016, genres=["Drama", "Horror"],
    )
    svc = _svc(series=[stranger])
    result = await svc.detect_content_type("Stranger Things")
    assert result.content_type == ContentType.SERIES


# ---------------------------------------------------------------------------
# Detection quality regressions carried over from the pre-rollback suite.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_bug03_movie_with_year_is_not_classified_as_music():
    """BUG-03 / LOGIC-03: 'Avatar 2009' must NOT pick MUSIC even if there's an
    artist named 'Avatar' in Lidarr — query has a year, music dropped."""
    svc = _svc(
        movies=[MovieInfo(title="Avatar", tmdb_id=19995, year=2009)],
        artists=[ArtistInfo(mb_id="x", name="Avatar")],
    )
    result = await svc.detect_content_type("Avatar 2009")
    assert result.content_type == ContentType.MOVIE


@pytest.mark.asyncio
async def test_bug01_substring_does_not_pick_music():
    """BUG-01 / LOGIC-02: short ambiguous query must not auto-pick music when
    a movie candidate also matches. 'Joker' should not silently go to music."""
    svc = _svc(
        movies=[MovieInfo(title="Joker", tmdb_id=475557, year=2019)],
        artists=[ArtistInfo(mb_id="y", name="Joker")],
    )
    result = await svc.detect_content_type("Joker")
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
    result = await svc.detect_content_type("Interstellar 2014")
    assert result.content_type == ContentType.MOVIE
    assert result.lookup_results == [movie]


@pytest.mark.asyncio
async def test_logic06_definitive_series_winner_carries_lookup_results():
    """Series counterpart of the above."""
    series = SeriesInfo(title="Stranger Things", tvdb_id=305288, year=2016)
    svc = _svc(series=[series])
    result = await svc.detect_content_type("Stranger Things S01")
    # "S01" narrows scoring to series-vs-anime rather than short-circuiting
    # before the lookup, so the winner still carries its metadata (needed by
    # the caller to add the title).
    assert result.content_type == ContentType.SERIES
    assert result.lookup_results == [series]


@pytest.mark.asyncio
async def test_logic06_low_confidence_unknown_has_empty_lookup_results():
    """UNKNOWN/ambiguous results must not carry stale lookup_results — the
    caller asks the user, there is no "winning" content_type to attach them to."""
    svc = _svc(movies=[], series=[], artists=[])
    result = await svc.detect_content_type("zzz_no_such_thing_zzz")
    assert result.content_type == ContentType.UNKNOWN
    assert result.lookup_results == []


@pytest.mark.asyncio
async def test_bug05_all_lookups_failing_returns_unknown():
    """BUG-05: when every backend raises, return UNKNOWN — don't silently
    treat empty results as 'music' or 'no match'."""
    svc = _svc(
        radarr_fail=Exception("Radarr down"),
        sonarr_fail=Exception("Sonarr down"),
        lidarr_fail=Exception("Lidarr down"),
    )
    result = await svc.detect_content_type("Whatever Movie")
    assert result.content_type == ContentType.UNKNOWN
    assert result.confidence == 0.0


@pytest.mark.asyncio
async def test_series_pattern_narrows_but_lookups_still_run():
    """A season/episode marker rules out movie/music, but the winner still
    needs a real lookup match (no metadata source left → episodic fallback)."""
    svc = _svc(movies=[], series=[])
    result = await svc.detect_content_type("Breaking Bad S01E05")
    assert result.content_type == ContentType.SERIES


@pytest.mark.asyncio
async def test_low_confidence_returns_unknown():
    """LOGIC-28: when no lookup matches strongly, return UNKNOWN so the user
    is presented the type-question."""
    svc = _svc(movies=[], series=[], artists=[])
    result = await svc.detect_content_type("bzzz qwerty 12345")
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


# ---------------------------------------------------------------------------
# TEST-02: detection under partial-service-failure (real prod case — one *arr
# is down while the others are alive and should still be able to classify).
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_metadata_failure_returns_unknown_not_a_wrong_guess():
    """When every video lookup fails there is nothing left to classify with —
    ask the user instead of guessing."""
    svc = _svc(
        radarr_fail=Exception("Radarr 503"),
        sonarr_fail=Exception("Sonarr 503"),
        lidarr_enabled=False,
    )
    result = await svc.detect_content_type("Breaking Bad")
    assert result.content_type == ContentType.UNKNOWN
    assert result.confidence == 0.0


@pytest.mark.asyncio
async def test_partial_failure_music_backend_down_movie_still_wins():
    """Lidarr raises, Radarr matches strongly → MOVIE, not UNKNOWN."""
    svc = _svc(
        movies=[MovieInfo(title="Interstellar", tmdb_id=157336, year=2014)],
        lidarr_fail=Exception("Lidarr 503"),
    )
    result = await svc.detect_content_type("Interstellar")
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
    svc = SearchService(radarr, _sonarr(series=[]))

    orig_timeout = search_service_mod._DETECT_TIMEOUT_S
    search_service_mod._DETECT_TIMEOUT_S = 0.05
    try:
        result = await svc.detect_content_type("Some Unresolvable Query")
    finally:
        search_service_mod._DETECT_TIMEOUT_S = orig_timeout

    assert result.content_type == ContentType.UNKNOWN
    assert result.confidence == 0.0


# ---------------------------------------------------------------------------
# PERF-01: the detection-burst guard. A single free-text message fans out
# concurrent Radarr+Sonarr+Lidarr lookups — the TTL cache stops a
# double-tap/retry from re-triggering that burst for an identical query.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_repeated_detect_hits_cache_lookup_called_once():
    """A repeated identical (normalized) query must not re-trigger lookups."""
    svc = _svc(movies=[MovieInfo(title="Dune", tmdb_id=1, year=2021)])

    await svc.detect_content_type("Dune 2021")
    await svc.detect_content_type("  dune   2021  ")  # normalizes to same key

    assert svc.radarr.lookup_movie.await_count == 1
