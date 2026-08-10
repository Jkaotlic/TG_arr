"""Contract tests for the Prowlarr client."""

from unittest.mock import AsyncMock, patch

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
