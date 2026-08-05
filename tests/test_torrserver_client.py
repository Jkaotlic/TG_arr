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


# --- search() — torznab bridge ---

SEARCH_RESPONSE = [
    {
        "Title": "Interstellar 2014 BDRemux 1080p", "Name": "Interstellar 2014 BDRemux 1080p",
        "Size": "2.5 GCiB", "CreateDate": "2026-07-18T20:49:00+03:00", "Tracker": "",
        "Link": "http://192.168.0.95:9696/2/download?apikey=k&link=abc&file=x",
        "Year": 2014, "Peer": 5, "Seed": 5, "Magnet": "", "Hash": "", "IMDBID": "tt0816692",
    },
    {
        "Title": "Interstellar 2014 WEBDL 2160p", "Name": "Interstellar 2014 WEBDL 2160p",
        "Size": "40 GCiB", "CreateDate": "", "Tracker": "Knaben",
        "Link": "http://192.168.0.95:9696/13/download?apikey=k&link=def",
        "Year": 0, "Peer": 90, "Seed": 120, "Magnet": "magnet:?xt=urn:btih:dead", "Hash": "",
    },
]

SETTINGS_WITH_SOURCES = {"CacheSize": 1, "UseDisk": False, "TorznabUrls": [
    {"Host": "http://192.168.0.95:9696/2", "Key": "k", "Name": "RuTracker.org"},
    {"Host": "http://192.168.0.95:9696/13", "Key": "k", "Name": "Knaben"},
]}


@pytest.mark.asyncio
async def test_search_sorts_by_seeders_and_parses_sizes(client):
    with patch.object(client, "get", new_callable=AsyncMock, return_value=SEARCH_RESPONSE), \
         patch.object(client, "get_server_settings", new_callable=AsyncMock,
                      return_value=SETTINGS_WITH_SOURCES):
        releases = await client.search("Interstellar")

    assert [r.seeders for r in releases] == [120, 5]
    assert releases[1].size == int(2.5 * 1024 ** 3)
    assert releases[0].link.endswith("link=def")


@pytest.mark.asyncio
async def test_search_fills_tracker_name_from_link_when_empty(client):
    with patch.object(client, "get", new_callable=AsyncMock, return_value=SEARCH_RESPONSE), \
         patch.object(client, "get_server_settings", new_callable=AsyncMock,
                      return_value=SETTINGS_WITH_SOURCES):
        releases = await client.search("Interstellar")

    by_title = {r.title: r for r in releases}
    assert by_title["Interstellar 2014 BDRemux 1080p"].tracker == "RuTracker.org"
    assert by_title["Interstellar 2014 WEBDL 2160p"].tracker == "Knaben"


@pytest.mark.asyncio
async def test_search_passes_query_as_query_string_not_path(client):
    """Гоча: /torznab/search/<строка> уходит пустым запросом и возвращает
    ленту последних раздач — «поиск работает, но нерелевантно»."""
    with patch.object(client, "get", new_callable=AsyncMock, return_value=[]) as mocked, \
         patch.object(client, "get_server_settings", new_callable=AsyncMock, return_value={}):
        await client.search("Дюна 2021")

    assert mocked.await_args.args[0] == "/torznab/search/"
    assert mocked.await_args.kwargs["params"] == {"query": "Дюна 2021"}


@pytest.mark.asyncio
async def test_search_truncates_to_limit(client):
    many = [dict(SEARCH_RESPONSE[0], Seed=i) for i in range(50)]
    with patch.object(client, "get", new_callable=AsyncMock, return_value=many), \
         patch.object(client, "get_server_settings", new_callable=AsyncMock, return_value={}):
        releases = await client.search("x", limit=30)

    assert len(releases) == 30


@pytest.mark.asyncio
async def test_search_returns_empty_list_on_unexpected_payload(client):
    with patch.object(client, "get", new_callable=AsyncMock, return_value={"error": "nope"}), \
         patch.object(client, "get_server_settings", new_callable=AsyncMock, return_value={}):
        assert await client.search("x") == []


@pytest.mark.asyncio
async def test_search_survives_missing_source_settings(client):
    """Настройки не прочитались — поиск всё равно работает, просто без имён."""
    with patch.object(client, "get", new_callable=AsyncMock, return_value=SEARCH_RESPONSE), \
         patch.object(client, "get_server_settings", new_callable=AsyncMock,
                      side_effect=TorrServerError("boom")):
        releases = await client.search("Interstellar")

    assert len(releases) == 2
    assert releases[1].tracker == ""


@pytest.mark.asyncio
async def test_search_skips_malformed_torznab_url_entries(client):
    """Сервер может отдать в TorznabUrls битую запись (не словарь) — она не
    TorrServerError, поэтому голый except TorrServerError в search() её не
    ловит. Один плохой элемент не должен ронять весь поиск, а валидный
    сосед рядом с ним должен по-прежнему резолвиться в имя трекера."""
    settings_with_junk = {"CacheSize": 1, "UseDisk": False, "TorznabUrls": [
        "not a dict",
        None,
        {"Host": "http://192.168.0.95:9696/2", "Key": "k", "Name": "RuTracker.org"},
    ]}
    with patch.object(client, "get", new_callable=AsyncMock, return_value=SEARCH_RESPONSE), \
         patch.object(client, "get_server_settings", new_callable=AsyncMock,
                      return_value=settings_with_junk):
        releases = await client.search("Interstellar")

    by_title = {r.title: r for r in releases}
    assert len(releases) == 2
    assert by_title["Interstellar 2014 BDRemux 1080p"].tracker == "RuTracker.org"


# --- add_torrent / remove_torrent / stream_url ---

@pytest.mark.asyncio
async def test_add_torrent_sends_sanitized_title(client):
    """Слэш в названии ломает листинг WebDAV — чистим до добавления."""
    added = {"title": "X", "hash": "abc", "stat": 1,
             "stat_string": "Torrent getting info", "torrent_size": None}
    with _patch_post(client, added) as mocked:
        torrent = await client.add_torrent(
            "http://p:9696/2/download?link=a",
            "Холодное сердце 2 / Frozen II [2019]",
        )

    payload = mocked.await_args.kwargs["json_data"]
    assert payload["action"] == "add"
    assert payload["title"] == "Холодное сердце 2 - Frozen II [2019]"
    assert payload["link"] == "http://p:9696/2/download?link=a"
    assert payload["save_to_db"] is True
    assert torrent.hash == "abc"
    assert torrent.size == 0  # torrent_size приходит null сразу после добавления


@pytest.mark.asyncio
async def test_add_torrent_without_hash_is_an_error(client):
    with _patch_post(client, {"title": "X"}):
        with pytest.raises(TorrServerError, match="не принял"):
            await client.add_torrent("http://link", "X")


@pytest.mark.asyncio
async def test_remove_torrent_sends_rem_action(client):
    with _patch_post(client, "") as mocked:
        await client.remove_torrent("abc")

    assert mocked.await_args.kwargs["json_data"] == {"action": "rem", "hash": "abc"}


def test_stream_url_matches_the_working_sync_script(client):
    url = client.stream_url("abc", 2, "Big Buck Bunny/Big Buck Bunny.mp4")
    assert url == "http://ts:8090/stream/Big%20Buck%20Bunny.mp4?link=abc&index=2&play"
