"""Feature #4: pre-grab 'already in Emby library' dedup check."""

from unittest.mock import AsyncMock, patch

import pytest

from bot.models import MovieInfo


@pytest.mark.asyncio
async def test_item_exists_matches_by_name_and_year():
    from bot.clients.emby import EmbyClient

    c = EmbyClient("http://x", "k")
    c._request = AsyncMock(return_value={"Items": [{"Name": "Inception", "ProductionYear": 2010}], "TotalRecordCount": 1})
    assert await c.item_exists("Inception", 2010, "Movie") is True
    # name matches but year is far off -> not a duplicate
    assert await c.item_exists("Inception", 1999, "Movie") is False


@pytest.mark.asyncio
async def test_item_exists_true_when_year_unknown():
    from bot.clients.emby import EmbyClient

    c = EmbyClient("http://x", "k")
    c._request = AsyncMock(return_value={"Items": [{"Name": "Dune", "ProductionYear": 2021}]})
    assert await c.item_exists("Dune", None, "Movie") is True


@pytest.mark.asyncio
async def test_item_exists_false_on_empty_or_error():
    from bot.clients.emby import EmbyClient

    c = EmbyClient("http://x", "k")
    c._request = AsyncMock(return_value={"Items": [], "TotalRecordCount": 0})
    assert await c.item_exists("Nope", 2010, "Movie") is False

    # best-effort: any error means "unknown" -> False (never block a grab)
    c._request = AsyncMock(side_effect=Exception("emby down"))
    assert await c.item_exists("X", 2010, "Movie") is False


@pytest.mark.asyncio
async def test_emby_library_note_present_and_absent():
    from bot.handlers import search

    movie = MovieInfo(tmdb_id=1, title="X", year=2020)

    emby = AsyncMock()
    emby.item_exists = AsyncMock(return_value=True)
    with patch.object(search, "get_emby", AsyncMock(return_value=emby)):
        assert "Emby" in await search._emby_library_note(movie)

    emby.item_exists = AsyncMock(return_value=False)
    with patch.object(search, "get_emby", AsyncMock(return_value=emby)):
        assert await search._emby_library_note(movie) == ""

    # No Emby configured -> empty, no error
    with patch.object(search, "get_emby", AsyncMock(return_value=None)):
        assert await search._emby_library_note(movie) == ""
