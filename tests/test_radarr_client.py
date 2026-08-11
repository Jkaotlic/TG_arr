"""Contract tests for the Radarr client."""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_lookup_movie_parses_poster_and_ratings(sample_radarr_movie):
    """Images and ratings arrive as nested lists/dicts and must be flattened."""
    from bot.clients.radarr import RadarrClient

    client = RadarrClient("http://radarr", "key")
    with patch.object(client, "get", new=AsyncMock(return_value=[sample_radarr_movie])):
        movies = await client.lookup_movie("Test Movie")

    assert len(movies) == 1
    movie = movies[0]
    assert movie.tmdb_id == 123456
    assert movie.title == "Test Movie"
    assert movie.poster_url == "http://example.com/poster.jpg"
    assert movie.fanart_url == "http://example.com/fanart.jpg"
    assert movie.ratings["imdb"] == 7.5


@pytest.mark.asyncio
async def test_lookup_skips_entries_without_a_tmdb_id():
    """A metadata row with no tmdbId cannot be added later — drop it now."""
    from bot.clients.radarr import RadarrClient

    client = RadarrClient("http://radarr", "key")
    payload = [{"title": "No id"}, {"tmdbId": 7, "title": "Good", "year": 2021}]
    with patch.object(client, "get", new=AsyncMock(return_value=payload)):
        movies = await client.lookup_movie("whatever")

    assert [m.tmdb_id for m in movies] == [7]


@pytest.mark.asyncio
async def test_add_movie_sends_the_profile_folder_and_search_option():
    """The add payload is what actually starts a download — pin its shape."""
    from bot.clients.radarr import RadarrClient
    from bot.models import MovieInfo

    client = RadarrClient("http://radarr", "key")
    movie = MovieInfo(tmdb_id=42, title="Dune", year=2021)
    response = {"tmdbId": 42, "title": "Dune", "year": 2021, "id": 15}

    with patch.object(client, "post", new=AsyncMock(return_value=response)) as post:
        added = await client.add_movie(
            movie, quality_profile_id=7, root_folder_path="G:\\radarr\\Films",
        )

    endpoint, = post.call_args.args
    payload = post.call_args.kwargs["json_data"]
    assert endpoint == "/api/v3/movie"
    assert payload["tmdbId"] == 42
    assert payload["qualityProfileId"] == 7
    assert payload["rootFolderPath"] == "G:\\radarr\\Films"
    assert payload["addOptions"]["searchForMovie"] is True
    assert added.radarr_id == 15


@pytest.mark.asyncio
async def test_search_movie_issues_the_command():
    """The auto-search fallback is a command, not a release push."""
    from bot.clients.radarr import RadarrClient

    client = RadarrClient("http://radarr", "key")
    with patch.object(client, "post", new=AsyncMock(return_value={"id": 1})) as post:
        await client.search_movie(15)

    assert post.call_args.args[0] == "/api/v3/command"
    assert post.call_args.kwargs["json_data"] == {"name": "MoviesSearch", "movieIds": [15]}


@pytest.mark.asyncio
async def test_get_wanted_movies_reads_the_missing_endpoint():
    from bot.clients.radarr import RadarrClient

    client = RadarrClient("http://radarr", "key")
    payload = {"records": [{"id": 1, "title": "Missing One", "year": 2024}]}
    with patch.object(client, "get", new=AsyncMock(return_value=payload)) as get:
        wanted = await client.get_wanted_movies()

    assert get.call_args.args[0] == "/api/v3/wanted/missing"
    assert get.call_args.kwargs["params"]["pageSize"] == 50
    assert wanted[0]["title"] == "Missing One"


@pytest.mark.asyncio
async def test_get_wanted_movies_does_not_send_include_series():
    """Review fix round 1 (2026-08-10): Sonarr's wanted query needed
    `includeSeries=true` to embed the parent title (see the Sonarr-side
    test) — Radarr's `/wanted/missing` has no series concept, so the shared
    `_get_wanted` must not leak that param onto Radarr's call."""
    from bot.clients.radarr import RadarrClient

    client = RadarrClient("http://radarr", "key")
    payload = {"records": []}
    with patch.object(client, "get", new=AsyncMock(return_value=payload)) as get:
        await client.get_wanted_movies()

    assert "includeSeries" not in get.call_args.kwargs["params"]


@pytest.mark.asyncio
async def test_set_movie_monitored_patches_the_resource():
    """Unmonitoring 102 unobtainable episodes must not need a hand-written script."""
    from bot.clients.radarr import RadarrClient

    client = RadarrClient("http://radarr", "key")
    with patch.object(client, "get", new=AsyncMock(return_value={"id": 15, "monitored": True})), \
         patch.object(client, "_request", new=AsyncMock(return_value={"id": 15, "monitored": False})) as req:
        ok = await client.set_movie_monitored(15, False)

    assert ok is True
    assert req.call_args.args[0] == "PUT"
    assert req.call_args.args[1] == "/api/v3/movie/15"
    assert req.call_args.kwargs["json_data"]["monitored"] is False


@pytest.mark.asyncio
async def test_delete_movie_defaults_to_keeping_files():
    """A catalog removal must never delete media unless explicitly asked."""
    from bot.clients.radarr import RadarrClient

    client = RadarrClient("http://radarr", "key")
    with patch.object(client, "_request", new=AsyncMock(return_value={})) as req:
        ok = await client.delete_movie(15)

    assert ok is True
    assert req.call_args.args[0] == "DELETE"
    assert req.call_args.args[1] == "/api/v3/movie/15"
    assert req.call_args.kwargs["params"]["deleteFiles"] is False


@pytest.mark.asyncio
async def test_set_movie_monitored_surfaces_service_connection_error_on_persistent_failure():
    """Fix round 1 (2026-08-10 review): _set_monitored must go through
    _safe_request, not call _request directly — otherwise a connection
    failure that survives tenacity's retries leaks a raw httpx exception
    instead of the domain ServiceConnectionError every other public method
    raises."""
    import httpx

    from bot.clients.base import ServiceConnectionError
    from bot.clients.radarr import RadarrClient

    client = RadarrClient("http://radarr", "key")
    with patch.object(client, "get", new=AsyncMock(return_value={"id": 15, "monitored": True})), \
         patch.object(client, "_request", new=AsyncMock(side_effect=httpx.ConnectError("refused"))):
        with pytest.raises(ServiceConnectionError):
            await client.set_movie_monitored(15, False)


@pytest.mark.asyncio
async def test_delete_movie_surfaces_service_connection_error_on_persistent_failure():
    """Same fix as above, DELETE path."""
    import httpx

    from bot.clients.base import ServiceConnectionError
    from bot.clients.radarr import RadarrClient

    client = RadarrClient("http://radarr", "key")
    with patch.object(client, "_request", new=AsyncMock(side_effect=httpx.ConnectError("refused"))):
        with pytest.raises(ServiceConnectionError):
            await client.delete_movie(15)


# ============================================================================
# Characterization tests — mandated by Task 3's review: restoring a large file
# against a handful of contract tests leaves _parse_movie and get_calendar's
# response handling untested (every contract test above either patches
# lookup_movie's whole response or never touches these code paths at all).
# These pin down what the RESTORED code actually does today, not what it
# should do — no production code is changed to make them pass.
# ============================================================================


def test_parse_movie_falls_back_to_original_title_when_title_missing():
    from bot.clients.radarr import RadarrClient

    client = RadarrClient("http://radarr", "key")
    movie = client._parse_movie({"tmdbId": 1, "originalTitle": "Original Only"})

    assert movie is not None
    assert movie.title == "Original Only"


def test_parse_movie_returns_none_without_a_tmdb_id():
    from bot.clients.radarr import RadarrClient

    client = RadarrClient("http://radarr", "key")

    assert client._parse_movie({"title": "No id"}) is None


def test_parse_movie_returns_none_without_any_title():
    from bot.clients.radarr import RadarrClient

    client = RadarrClient("http://radarr", "key")

    assert client._parse_movie({"tmdbId": 5}) is None


def test_parse_movie_defaults_year_to_zero_when_missing():
    from bot.clients.radarr import RadarrClient

    client = RadarrClient("http://radarr", "key")
    movie = client._parse_movie({"tmdbId": 5, "title": "No Year"})

    assert movie.year == 0


def test_parse_movie_root_folder_path_falls_back_to_path_field():
    """Some Radarr responses (library reads) use `path` instead of
    `rootFolderPath`."""
    from bot.clients.radarr import RadarrClient

    client = RadarrClient("http://radarr", "key")
    movie = client._parse_movie({"tmdbId": 5, "title": "X", "path": "G:\\radarr\\Films\\X"})

    assert movie.root_folder_path == "G:\\radarr\\Films\\X"


def test_parse_movie_ratings_skip_entries_without_a_value_key():
    from bot.clients.radarr import RadarrClient

    client = RadarrClient("http://radarr", "key")
    movie = client._parse_movie({
        "tmdbId": 5,
        "title": "X",
        "ratings": {"imdb": {"value": 8.1}, "rottenTomatoes": {"votes": 100}},
    })

    assert movie.ratings == {"imdb": 8.1}


def test_parse_movie_raises_when_ratings_is_present_but_not_a_dict():
    """Discrepancy flag (see fix report): unlike _parse_series's
    `isinstance(rating_data, dict)` guard, _parse_movie calls
    `item["ratings"].items()` unconditionally once the key is present — a
    non-dict value (None, a list) raises AttributeError instead of being
    skipped. Real Radarr always sends a dict here, so this has not been
    observed live, but add_movie's own `_parse_movie(result)` call has no
    try/except around it (only lookup_movie does), so a malformed response
    would surface as an unhandled AttributeError rather than the intended
    APIError. Production code is NOT changed by this test."""
    from bot.clients.radarr import RadarrClient

    client = RadarrClient("http://radarr", "key")

    with pytest.raises(AttributeError):
        client._parse_movie({"tmdbId": 5, "title": "X", "ratings": None})


@pytest.mark.asyncio
async def test_get_calendar_prefers_digital_release_over_physical_and_cinema():
    from bot.clients.radarr import RadarrClient

    client = RadarrClient("http://radarr", "key")
    item = {
        "title": "Dune",
        "year": 2021,
        "digitalRelease": "2021-10-22",
        "physicalRelease": "2021-11-01",
        "inCinemas": "2021-10-01",
        "hasFile": True,
    }
    with patch.object(client, "get", new=AsyncMock(return_value=[item])):
        entries = await client.get_calendar()

    assert entries[0]["release_date"] == "2021-10-22"
    assert entries[0]["has_file"] is True


@pytest.mark.asyncio
async def test_get_calendar_falls_back_to_cinema_release_when_others_absent():
    from bot.clients.radarr import RadarrClient

    client = RadarrClient("http://radarr", "key")
    item = {"title": "Dune", "inCinemas": "2021-10-01"}
    with patch.object(client, "get", new=AsyncMock(return_value=[item])):
        entries = await client.get_calendar()

    assert entries[0]["release_date"] == "2021-10-01"
    assert entries[0]["has_file"] is False


@pytest.mark.asyncio
async def test_get_calendar_returns_empty_list_for_a_non_list_response():
    """A dict error body (or any non-list JSON) must not raise."""
    from bot.clients.radarr import RadarrClient

    client = RadarrClient("http://radarr", "key")
    with patch.object(client, "get", new=AsyncMock(return_value={"error": "nope"})):
        entries = await client.get_calendar()

    assert entries == []


# ============================================================================
# Task 9: interactive search — releases carry *arr's own verdict
# ============================================================================


@pytest.mark.asyncio
async def test_get_releases_carries_the_arr_verdict():
    """*arr already scored these against the user's profile and custom formats.

    Live shape captured from Radarr 6.3.0 on 2026-08-10.
    """
    from bot.clients.radarr import RadarrClient

    payload = [
        {
            "guid": "abc-1", "indexerId": 3, "title": "Dune 2021 2160p BluRay",
            "size": 50_000_000_000, "seeders": 100, "leechers": 2,
            "protocol": "torrent", "downloadUrl": "http://prowlarr/1/download?apikey=x",
            "customFormatScore": 500, "rejected": False, "rejections": [],
            "languages": [{"id": 1, "name": "English"}],
            "quality": {"quality": {"name": "Bluray-2160p"}},
        },
        {
            "guid": "abc-2", "indexerId": 4, "title": "Дюна 2021 2160p DUB",
            "size": 40_000_000_000, "seeders": 50, "leechers": 1,
            "protocol": "torrent", "downloadUrl": "http://prowlarr/2/download?apikey=x",
            "customFormatScore": -1000, "rejected": True,
            "rejections": ["English is wanted, but found Russian"],
            "languages": [{"id": 11, "name": "Russian"}],
            "quality": {"quality": {"name": "WEBDL-2160p"}},
        },
    ]

    client = RadarrClient("http://radarr", "key")
    with patch.object(client, "get", new=AsyncMock(return_value=payload)) as get:
        releases = await client.get_releases(15)

    assert get.call_args.args[0] == "/api/v3/release"
    assert get.call_args.kwargs["params"] == {"movieId": 15}

    good, bad = releases
    assert good.custom_format_score == 500
    assert good.rejected is False
    assert good.languages == ["English"]
    assert bad.rejected is True
    assert bad.rejections == ["English is wanted, but found Russian"]
    assert bad.guid == "abc-2"
    assert bad.indexer_id == 4
    # Fix round 1 (2026-08-10 review): origin="arr" is what actually gates
    # AddService.grab_release's native path — indexer_id truthiness alone
    # isn't safe (ProwlarrClient's free-text results also carry one). Fix
    # round 2: renamed source -> origin (collided with QualityInfo.source).
    assert good.origin == "arr"
    assert bad.origin == "arr"


@pytest.mark.asyncio
async def test_get_releases_survives_a_malformed_entry():
    """One bad row from one indexer must not blank the whole result."""
    from bot.clients.radarr import RadarrClient

    payload = [
        {"guid": "ok", "title": "Good", "size": 1, "seeders": 1, "leechers": 0,
         "protocol": "torrent", "downloadUrl": "http://x", "customFormatScore": 0},
        {"title": "no guid at all"},
    ]
    client = RadarrClient("http://radarr", "key")
    with patch.object(client, "get", new=AsyncMock(return_value=payload)):
        releases = await client.get_releases(15)

    assert [r.guid for r in releases] == ["ok"]


@pytest.mark.asyncio
async def test_grab_release_posts_guid_and_indexer_id():
    """Task 10: the native path — *arr already knows this release from its
    own interactive search, so grabbing it is just guid+indexerId."""
    from bot.clients.radarr import RadarrClient

    client = RadarrClient("http://radarr", "key")
    with patch.object(client, "_post_no_retry", new=AsyncMock(return_value={})) as post:
        ok = await client.grab_release("abc-1", 3)

    assert ok is True
    assert post.call_args.args[0] == "/api/v3/release"
    assert post.call_args.kwargs["json_data"] == {"guid": "abc-1", "indexerId": 3}
