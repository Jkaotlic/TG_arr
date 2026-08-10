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
