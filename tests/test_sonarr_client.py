"""Contract tests for the Sonarr client."""

from unittest.mock import AsyncMock, patch

import pytest

from bot.models import ContentType


@pytest.mark.asyncio
async def test_lookup_series_parses_seasons(sample_sonarr_series):
    from bot.clients.sonarr import SonarrClient

    client = SonarrClient("http://sonarr", "key")
    with patch.object(client, "get", new=AsyncMock(return_value=[sample_sonarr_series])):
        series = await client.lookup_series("Test Series")

    assert len(series) == 1
    assert series[0].tvdb_id == 654321
    assert series[0].title == "Test Series"


@pytest.mark.asyncio
async def test_add_series_sends_anime_series_type():
    """Anime differs from a series by exactly this field — nothing else."""
    from bot.clients.sonarr import SonarrClient
    from bot.models import SeriesInfo

    client = SonarrClient("http://sonarr", "key")
    series = SeriesInfo(tvdb_id=99, title="Frieren")
    response = {"tvdbId": 99, "title": "Frieren", "id": 3, "seriesType": "anime"}

    with patch.object(client, "post", new=AsyncMock(return_value=response)) as post:
        added = await client.add_series(
            series,
            quality_profile_id=7,
            root_folder_path="G:\\tv-sonarr\\Serials",
            series_type="anime",
        )

    payload = post.call_args.kwargs["json_data"]
    assert payload["seriesType"] == "anime"
    assert payload["qualityProfileId"] == 7
    assert payload["rootFolderPath"] == "G:\\tv-sonarr\\Serials"
    assert added.sonarr_id == 3
    assert added.content_type is ContentType.ANIME


@pytest.mark.asyncio
async def test_add_series_defaults_to_standard_type():
    """A plain series must not be silently filed as anime."""
    from bot.clients.sonarr import SonarrClient
    from bot.models import SeriesInfo

    client = SonarrClient("http://sonarr", "key")
    response = {"tvdbId": 1, "title": "Fargo", "id": 4}

    with patch.object(client, "post", new=AsyncMock(return_value=response)) as post:
        await client.add_series(
            SeriesInfo(tvdb_id=1, title="Fargo"),
            quality_profile_id=4,
            root_folder_path="G:\\tv-sonarr\\Serials",
        )

    assert post.call_args.kwargs["json_data"]["seriesType"] == "standard"


@pytest.mark.asyncio
async def test_search_season_targets_one_season():
    """The season picker must not trigger a full-series search."""
    from bot.clients.sonarr import SonarrClient

    client = SonarrClient("http://sonarr", "key")
    with patch.object(client, "post", new=AsyncMock(return_value={"id": 1})) as post:
        await client.search_season(3, 2)

    assert post.call_args.args[0] == "/api/v3/command"
    assert post.call_args.kwargs["json_data"] == {
        "name": "SeasonSearch", "seriesId": 3, "seasonNumber": 2,
    }


@pytest.mark.asyncio
async def test_delete_series_uses_the_series_resource():
    from bot.clients.sonarr import SonarrClient

    client = SonarrClient("http://sonarr", "key")
    with patch.object(client, "_request", new=AsyncMock(return_value={})) as req:
        ok = await client.delete_series(3, delete_files=True)

    assert ok is True
    assert req.call_args.args[1] == "/api/v3/series/3"
    assert req.call_args.kwargs["params"]["deleteFiles"] is True


@pytest.mark.asyncio
async def test_get_wanted_episodes_reads_the_missing_endpoint():
    """Fix round 1 (2026-08-10 review): Sonarr's wanted/monitor wrappers had
    no direct coverage while Radarr's did — evened up here."""
    from bot.clients.sonarr import SonarrClient

    client = SonarrClient("http://sonarr", "key")
    payload = {"records": [{"id": 1, "title": "Missing Episode Series", "seasonNumber": 2}]}
    with patch.object(client, "get", new=AsyncMock(return_value=payload)) as get:
        wanted = await client.get_wanted_episodes()

    assert get.call_args.args[0] == "/api/v3/wanted/missing"
    assert get.call_args.kwargs["params"]["pageSize"] == 50
    assert wanted[0]["title"] == "Missing Episode Series"


@pytest.mark.asyncio
async def test_set_series_monitored_patches_the_resource():
    from bot.clients.sonarr import SonarrClient

    client = SonarrClient("http://sonarr", "key")
    with patch.object(client, "get", new=AsyncMock(return_value={"id": 3, "monitored": True})), \
         patch.object(client, "_request", new=AsyncMock(return_value={"id": 3, "monitored": False})) as req:
        ok = await client.set_series_monitored(3, False)

    assert ok is True
    assert req.call_args.args[0] == "PUT"
    assert req.call_args.args[1] == "/api/v3/series/3"
    assert req.call_args.kwargs["json_data"]["monitored"] is False


@pytest.mark.asyncio
async def test_set_series_monitored_surfaces_service_connection_error_on_persistent_failure():
    """Same _safe_request fix as the Radarr equivalent — a persistent
    connection failure must surface as ServiceConnectionError, not a raw
    httpx exception."""
    import httpx

    from bot.clients.base import ServiceConnectionError
    from bot.clients.sonarr import SonarrClient

    client = SonarrClient("http://sonarr", "key")
    with patch.object(client, "get", new=AsyncMock(return_value={"id": 3, "monitored": True})), \
         patch.object(client, "_request", new=AsyncMock(side_effect=httpx.ConnectError("refused"))):
        with pytest.raises(ServiceConnectionError):
            await client.set_series_monitored(3, False)


@pytest.mark.asyncio
async def test_delete_series_surfaces_service_connection_error_on_persistent_failure():
    import httpx

    from bot.clients.base import ServiceConnectionError
    from bot.clients.sonarr import SonarrClient

    client = SonarrClient("http://sonarr", "key")
    with patch.object(client, "_request", new=AsyncMock(side_effect=httpx.ConnectError("refused"))):
        with pytest.raises(ServiceConnectionError):
            await client.delete_series(3)


# ============================================================================
# Characterization tests — mandated by Task 3's review: restoring a large file
# against a handful of contract tests leaves _parse_series, get_calendar and
# _should_monitor_season's response handling untested (every contract test
# above either patches lookup_series's whole response or never touches these
# code paths at all). These pin down what the RESTORED code actually does
# today, not what it should do — no production code is changed to make them
# pass.
# ============================================================================


def test_parse_series_falls_back_to_sort_title_when_title_missing():
    from bot.clients.sonarr import SonarrClient

    client = SonarrClient("http://sonarr", "key")
    series = client._parse_series({"tvdbId": 1, "sortTitle": "sort only"})

    assert series is not None
    assert series.title == "sort only"


def test_parse_series_returns_none_without_a_tvdb_id():
    from bot.clients.sonarr import SonarrClient

    client = SonarrClient("http://sonarr", "key")

    assert client._parse_series({"title": "No id"}) is None


def test_parse_series_returns_none_without_any_title():
    from bot.clients.sonarr import SonarrClient

    client = SonarrClient("http://sonarr", "key")

    assert client._parse_series({"tvdbId": 5}) is None


def test_parse_series_season_count_excludes_season_zero(sample_sonarr_series):
    """Season 0 is Sonarr's "Specials" bucket — not a real season."""
    from bot.clients.sonarr import SonarrClient

    client = SonarrClient("http://sonarr", "key")
    series = client._parse_series(sample_sonarr_series)

    assert series.season_count == 3
    assert series.total_episode_count == 28


def test_parse_series_ratings_use_default_key_for_a_flat_value():
    from bot.clients.sonarr import SonarrClient

    client = SonarrClient("http://sonarr", "key")
    series = client._parse_series({"tvdbId": 1, "title": "X", "ratings": {"value": 8.4}})

    assert series.ratings == {"default": 8.4}


def test_parse_series_ratings_use_per_source_keys_when_nested():
    from bot.clients.sonarr import SonarrClient

    client = SonarrClient("http://sonarr", "key")
    series = client._parse_series({
        "tvdbId": 1,
        "title": "X",
        "ratings": {"imdb": {"value": 8.4}, "tvdb": {"value": 9.0}},
    })

    assert series.ratings == {"imdb": 8.4, "tvdb": 9.0}


def test_parse_series_series_type_defaults_to_standard_when_key_absent():
    from bot.clients.sonarr import SonarrClient
    from bot.models import ContentType

    client = SonarrClient("http://sonarr", "key")
    series = client._parse_series({"tvdbId": 1, "title": "X"})

    assert series.series_type == "standard"
    assert series.content_type is ContentType.SERIES


@pytest.mark.parametrize(
    "monitor_type,season_num,total_seasons,expected",
    [
        ("all", 2, 3, True),
        ("none", 2, 3, False),
        ("future", 2, 3, False),
        ("missing", 2, 3, True),
        ("existing", 2, 3, True),
        ("pilot", 1, 3, True),
        ("pilot", 2, 3, False),
        ("firstSeason", 1, 3, True),
        ("firstSeason", 2, 3, False),
        ("latestSeason", 3, 3, True),
        ("latestSeason", 2, 3, False),
        ("some-unrecognized-value", 2, 3, True),
    ],
)
def test_should_monitor_season_by_monitor_type(monitor_type, season_num, total_seasons, expected):
    """Direct characterization of every branch, including the unconditional
    True fallback for a monitor_type this helper doesn't recognize."""
    from bot.clients.sonarr import SonarrClient

    client = SonarrClient("http://sonarr", "key")

    assert client._should_monitor_season(season_num, monitor_type, total_seasons) is expected


@pytest.mark.asyncio
async def test_get_calendar_reads_the_nested_series_title():
    from bot.clients.sonarr import SonarrClient

    client = SonarrClient("http://sonarr", "key")
    ep = {
        "series": {"title": "Test Series"},
        "seasonNumber": 2,
        "episodeNumber": 5,
        "title": "The Episode",
        "airDateUtc": "2026-08-15T00:00:00Z",
        "hasFile": False,
    }
    with patch.object(client, "get", new=AsyncMock(return_value=[ep])):
        entries = await client.get_calendar()

    assert entries[0]["series_title"] == "Test Series"
    assert entries[0]["season"] == 2
    assert entries[0]["episode"] == 5
    assert entries[0]["has_file"] is False


@pytest.mark.asyncio
async def test_get_calendar_defaults_series_title_to_unknown_when_series_key_absent():
    from bot.clients.sonarr import SonarrClient

    client = SonarrClient("http://sonarr", "key")
    ep = {"seasonNumber": 1, "episodeNumber": 1}
    with patch.object(client, "get", new=AsyncMock(return_value=[ep])):
        entries = await client.get_calendar()

    assert entries[0]["series_title"] == "Unknown"


@pytest.mark.asyncio
async def test_get_calendar_returns_empty_list_for_a_non_list_response():
    from bot.clients.sonarr import SonarrClient

    client = SonarrClient("http://sonarr", "key")
    with patch.object(client, "get", new=AsyncMock(return_value={"error": "nope"})):
        entries = await client.get_calendar()

    assert entries == []


# ============================================================================
# Task 9: interactive search — releases carry *arr's own verdict
# ============================================================================


@pytest.mark.asyncio
async def test_get_releases_can_narrow_to_one_season():
    """A season pick must reach Sonarr, not just filter locally."""
    from bot.clients.sonarr import SonarrClient

    client = SonarrClient("http://sonarr", "key")
    with patch.object(client, "get", new=AsyncMock(return_value=[])) as get:
        await client.get_releases(3, season_number=2)

    assert get.call_args.args[0] == "/api/v3/release"
    assert get.call_args.kwargs["params"] == {"seriesId": 3, "seasonNumber": 2}


@pytest.mark.asyncio
async def test_get_releases_without_season_asks_for_the_whole_series():
    from bot.clients.sonarr import SonarrClient

    client = SonarrClient("http://sonarr", "key")
    with patch.object(client, "get", new=AsyncMock(return_value=[])) as get:
        await client.get_releases(3)

    assert get.call_args.kwargs["params"] == {"seriesId": 3}
