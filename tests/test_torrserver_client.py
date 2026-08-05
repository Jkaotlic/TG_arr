"""Клиент TorrServer: разбор ответов реального сервера (контракты сняты
живьём 2026-08-05) и поведение при ошибках."""

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from bot.clients.base import AuthenticationError
from bot.clients.torrserver import TorrServerClient, TorrServerError

LIST_RESPONSE = [
    {
        "title": "Vice Principals - S2E1-9 [2017, WEB-DL 1080p]",
        "category": "tv",
        "poster": "https://image.tmdb.org/t/p/w300/x.jpg",
        "data": json.dumps({"TorrServer": {"Files": [
            {"id": 1, "path": "Vice Principals/S02E01.mkv", "length": 2423476098},
            {"id": 2, "path": "Vice Principals/S02E02.mkv", "length": 2257100799},
        ]}}),
        "timestamp": 1785923402,
        "hash": "536d2ce72b6ad45d21829c1eac5398276fa69be5",
        "stat": 5,
        "stat_string": "Torrent in db",
        "torrent_size": 20875200614,
    },
    {
        "title": "Broken data",
        "category": "movie",
        "data": "not json at all",
        "hash": "ffff",
        "stat": 5,
        "stat_string": "Torrent in db",
        "torrent_size": 100,
    },
]


@pytest.fixture
def client():
    return TorrServerClient("http://ts:8090", "admin", "pw")


def _patch_post(client, result):
    return patch.object(client, "post", new_callable=AsyncMock, return_value=result)


@pytest.mark.asyncio
async def test_list_torrents_parses_files_from_data(client):
    with _patch_post(client, LIST_RESPONSE):
        torrents = await client.list_torrents()

    assert len(torrents) == 2
    first = torrents[0]
    assert first.hash == "536d2ce72b6ad45d21829c1eac5398276fa69be5"
    assert first.size == 20875200614
    assert [f.id for f in first.files] == [1, 2]


@pytest.mark.asyncio
async def test_list_torrents_survives_unparseable_data(client):
    """Один битый элемент не должен ронять весь список."""
    with _patch_post(client, LIST_RESPONSE):
        torrents = await client.list_torrents()

    assert torrents[1].hash == "ffff"
    assert torrents[1].files == []


@pytest.mark.asyncio
async def test_list_torrents_sends_the_list_action(client):
    with _patch_post(client, []) as mocked:
        await client.list_torrents()

    assert mocked.await_args.kwargs["json_data"] == {"action": "list"}


@pytest.mark.asyncio
async def test_get_torrent_prefers_file_stats(client):
    payload = {
        "hash": "abc", "title": "T", "stat": 3, "stat_string": "Torrent working",
        "torrent_size": 276445467, "category": "movie", "poster": "",
        "file_stats": [
            {"id": 1, "path": "Big Buck Bunny/Big Buck Bunny.en.srt", "length": 140},
            {"id": 2, "path": "Big Buck Bunny/Big Buck Bunny.mp4", "length": 276134947},
        ],
        "data": "",
    }
    with _patch_post(client, payload):
        torrent = await client.get_torrent("abc")

    assert [f.id for f in torrent.files] == [1, 2]
    assert [f.id for f in torrent.video_files] == [2]


@pytest.mark.asyncio
async def test_get_torrent_returns_none_for_empty_answer(client):
    with _patch_post(client, {}):
        assert await client.get_torrent("nope") is None


@pytest.mark.asyncio
async def test_auth_error_is_translated_to_credentials_message(client):
    with patch.object(client, "post", new_callable=AsyncMock,
                      side_effect=AuthenticationError("boom", status_code=401)):
        with pytest.raises(TorrServerError, match="логин"):
            await client.list_torrents()


@pytest.mark.asyncio
async def test_get_stats_combines_version_settings_and_list(client):
    settings_payload = {"CacheSize": 1610612736, "UseDisk": False, "TorznabUrls": [
        {"Host": "http://p:9696/2", "Key": "k", "Name": "RuTracker.org"},
        {"Host": "http://p:9696/15", "Key": "k", "Name": "The Pirate Bay"},
    ]}

    async def fake_post(endpoint, json_data=None, **kwargs):
        if endpoint == "/settings":
            return settings_payload
        return LIST_RESPONSE

    with patch.object(client, "post", new=AsyncMock(side_effect=fake_post)), \
         patch.object(client, "get_version", new_callable=AsyncMock, return_value="MatriX.142.2"):
        stats = await client.get_stats()

    assert stats.version == "MatriX.142.2"
    assert stats.torrent_count == 2
    assert stats.total_size == 20875200614 + 100
    assert stats.cache_size == 1610612736
    assert stats.use_disk is False
    assert stats.source_count == 2


@pytest.mark.asyncio
async def test_basic_auth_header_is_built_from_credentials(client):
    headers = client._get_headers()
    assert headers["Authorization"] == "Basic YWRtaW46cHc="


@pytest.mark.asyncio
async def test_check_connection_reports_version(client):
    with patch.object(client, "get_version", new_callable=AsyncMock, return_value="MatriX.142.2"):
        available, version, elapsed = await client.check_connection()

    assert available is True
    assert version == "MatriX.142.2"
    assert elapsed >= 0


@pytest.mark.asyncio
async def test_check_connection_reports_failure(client):
    with patch.object(client, "get_version", new_callable=AsyncMock,
                      side_effect=TorrServerError("dead")):
        available, version, _ = await client.check_connection()

    assert available is False
    assert version is None


# --- get_version() retry policy (review finding: /echo must retry on
# transient network errors like every other call in this client) ---

@pytest.mark.asyncio
async def test_get_version_retries_transient_connect_error_then_succeeds(client, monkeypatch):
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    transport = AsyncMock()
    transport.get = AsyncMock(side_effect=[
        httpx.ConnectError("boom"),
        httpx.Response(200, text="MatriX.142.2"),
    ])
    monkeypatch.setattr(client, "_get_client", AsyncMock(return_value=transport))

    version = await client.get_version()

    assert version == "MatriX.142.2"
    assert transport.get.await_count == 2


@pytest.mark.asyncio
async def test_get_version_raises_torrserver_error_when_retries_exhausted(client, monkeypatch):
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    transport = AsyncMock()
    transport.get = AsyncMock(side_effect=httpx.ConnectError("boom"))
    monkeypatch.setattr(client, "_get_client", AsyncMock(return_value=transport))

    with pytest.raises(TorrServerError):
        await client.get_version()

    # stop_after_attempt(3): the raw httpx error must not leak past the retry
    # policy — the caller sees TorrServerError only after all 3 attempts.
    assert transport.get.await_count == 3


@pytest.mark.asyncio
async def test_get_version_does_not_retry_a_non_retryable_http_error(client, monkeypatch):
    """A 500 is a real answer, not a transient transport failure — it must
    fail immediately, not burn through the retry budget."""
    transport = AsyncMock()
    transport.get = AsyncMock(return_value=httpx.Response(500, text="oops"))
    monkeypatch.setattr(client, "_get_client", AsyncMock(return_value=transport))

    with pytest.raises(TorrServerError):
        await client.get_version()

    assert transport.get.await_count == 1
