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
