"""Аудит 2026-10-30, сеть: NET-01, BUG-06, BUG-07, PERF-01.

Там, где важна настоящая сеть, сервер настоящий: localhost-сокет принимает
соединение и молчит. Там, где важно, что ушло в запрос, — настоящий httpx поверх
`MockTransport`: эффективный таймаут виден в `request.extensions["timeout"]`, и
мок клиента его не подменяет.
"""

import asyncio
import contextlib
import json
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import structlog.testing

from bot.clients import base
from bot.clients.base import (
    APIError,
    AuthenticationError,
    BaseAPIClient,
    ServiceConnectionError,
    background_calls,
)
from bot.clients.emby import EmbyClient, EmbyConnectionError, EmbyError
from bot.clients.lidarr import LidarrClient
from bot.clients.navidrome import NavidromeClient
from bot.clients.prowlarr import ProwlarrClient
from bot.clients.qbittorrent import QBittorrentClient, QBittorrentError
from bot.clients.radarr import RadarrClient
from bot.clients.slskd import SlskdClient
from bot.clients.sonarr import SonarrClient
from bot.clients.torrserver import TorrServerClient, TorrServerError
from bot.models import ArtistInfo, ContentType, MovieInfo, QualityInfo, SearchResult, SeriesInfo
from bot.services.add_service import GRAB_OUTCOME_UNREACHABLE, AddService, grab_outcome

URL = "http://arr.example.com"
TITLE = "Movie.2024.2160p.WEB-DL"


@asynccontextmanager
async def _hung_server():
    """Настоящий сервер: принимает соединение, читает и ничего не отвечает."""
    accepted: list[int] = []
    release = asyncio.Event()

    async def handler(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        accepted.append(1)
        await release.wait()
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}", accepted
    finally:
        release.set()
        server.close()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(server.wait_closed(), timeout=2)


def _retry_budget(timeout: float) -> tuple[int, float]:
    """(попытки, потолок времени) из настроек ретраев самого `_request`."""
    policy = BaseAPIClient._request.retry
    attempts = policy.stop.max_attempt_number
    wait = policy.wait
    backoff = sum(
        min(wait.max, max(wait.min, wait.multiplier * wait.exp_base**i))
        for i in range(attempts - 1)
    )
    return attempts, attempts * timeout + backoff


_wired: list[httpx.AsyncClient] = []


@pytest.fixture(autouse=True)
async def _close_wired_clients():
    yield
    while _wired:
        await _wired.pop().aclose()


def _wire(client, handler, http_timeout: float = 30.0):
    """Настоящий httpx-клиент поверх MockTransport, с таймаутом как у боевого."""
    client._settings = SimpleNamespace(http_timeout=http_timeout)
    client._client = httpx.AsyncClient(
        base_url=client.base_url,
        transport=httpx.MockTransport(handler),
        timeout=httpx.Timeout(http_timeout),
    )
    _wired.append(client._client)
    return client


_SEARCH_TIMEOUT = 77.0


@pytest.fixture
def search_settings(monkeypatch):
    """Настройки поиска без чтения окружения: get_settings здесь только за ними."""
    settings = SimpleNamespace(prowlarr_search_timeout=_SEARCH_TIMEOUT, prowlarr_search_retries=0)
    monkeypatch.setattr("bot.clients.base.get_settings", lambda: settings)
    monkeypatch.setattr("bot.clients.prowlarr.get_settings", lambda: settings)


def _drops_once(payload, error: type[Exception] = httpx.RemoteProtocolError):
    """Первый запрос обрывается, дальше сервер отвечает: стухший keep-alive."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            raise error("сбой", request=request)
        return httpx.Response(200, json=payload)

    return handler, calls


def _raising(error: type[Exception], seen: list[httpx.Request] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        raise error("сбой", request=request)

    return handler


def _raising_empty(error: type[Exception]):
    def handler(request: httpx.Request) -> httpx.Response:
        raise error("", request=request)

    return handler


def _generic_ok(seen: list[httpx.Request]):
    """Ответ, который переваривают все методы: нам важен только сам запрос."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"seasons": [{"seasonNumber": 1}], "records": []})

    return handler


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr("asyncio.sleep", AsyncMock())


# --------------------------------------------------------------- NET-01
async def test_hung_server_fails_in_bounded_time_and_counts_in_breaker():
    """NET-01: сервер принял и молчит → ошибка соединения за конечное время.

    Раньше вызов не возвращался вовсе: явный `timeout=None` в httpx значит «без
    таймаута». Потолок считается из настроек ретраев, а не задан числом.
    """
    http_timeout = 1.0
    attempts, budget = _retry_budget(http_timeout)

    async with _hung_server() as (url, accepted):
        client = RadarrClient(url, "key")
        client._settings = SimpleNamespace(http_timeout=http_timeout)
        started = time.monotonic()
        try:
            with pytest.raises(ServiceConnectionError):
                await asyncio.wait_for(client.get("/api/v3/system/status"), timeout=budget + 5)
        finally:
            await client.close()
        elapsed = time.monotonic() - started

    assert elapsed <= budget + 2, f"{elapsed:.1f} с при потолке {budget:.1f} с"
    assert len(accepted) == attempts
    assert client.breaker.is_down


@pytest.mark.parametrize(
    "factory",
    [
        lambda url: SlskdClient(url, "key", timeout=1.0),
        lambda url: NavidromeClient(url, "user", "secret", timeout=1.0),
    ],
    ids=["slskd", "navidrome"],
)
async def test_own_timeout_setting_applies_to_plain_calls(factory):
    """NET-01: SLSKD_TIMEOUT и NAVIDROME_TIMEOUT теперь действуют на get()."""
    async with _hung_server() as (url, _accepted):
        client = factory(url)
        try:
            with background_calls(), pytest.raises(ServiceConnectionError):
                await asyncio.wait_for(client.get("/ping"), timeout=5)
        finally:
            await client.close()


async def test_plain_calls_use_the_client_timeout():
    seen: list[httpx.Request] = []
    client = _wire(RadarrClient(URL, "key"), _generic_ok(seen))

    await client.get("/api/v3/system/status")
    await client.post("/api/v3/command", json_data={"name": "x"})
    await client._post_no_retry("/api/v3/release/push", json_data={})

    assert [r.extensions["timeout"]["read"] for r in seen] == [30.0, 30.0, 30.0]


async def test_slow_answer_survives_on_the_slow_timeout_only():
    """Ради этого и нужен SLOW_ARR_TIMEOUT: ответ дольше HTTP_TIMEOUT у lookup
    проходит, а у обычного вызова это таймаут. Сокет настоящий."""

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        await asyncio.sleep(1.0)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n[]")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    client = RadarrClient(url, "key")
    client._settings = SimpleNamespace(http_timeout=0.3)
    try:
        assert await client.lookup_movie("x") == []
        with background_calls(), pytest.raises(ServiceConnectionError):
            await client.get("/api/v3/system/status")
    finally:
        await client.close()
        server.close()


def test_slow_timeout_constant():
    assert base.SLOW_ARR_TIMEOUT >= 90


_MOVIE = MovieInfo(tmdb_id=1, title="X", year=2021)
_SERIES = SeriesInfo(tvdb_id=1, title="X")
_ARTIST = ArtistInfo(mb_id="m", name="X")
_SINCE = datetime(2026, 1, 1, tzinfo=timezone.utc)

_SLOW_CALLS = [
    # Прокси к TMDb/TVDB/MusicBrainz: Sonarr lookup ~34 с (замер 2026-08-10).
    (RadarrClient, lambda c: c.lookup_movie("x"), "GET", "/api/v3/movie/lookup"),
    (SonarrClient, lambda c: c.lookup_series("x"), "GET", "/api/v3/series/lookup"),
    (LidarrClient, lambda c: c.lookup_artist("x"), "GET", "/api/v1/artist/lookup"),
    (RadarrClient, lambda c: c.add_movie(_MOVIE, 1, "/m"), "POST", "/api/v3/movie"),
    (SonarrClient, lambda c: c.add_series(_SERIES, 1, "/s"), "POST", "/api/v3/series"),
    (LidarrClient, lambda c: c.add_artist(_ARTIST, 1, 1, "/a"), "POST", "/api/v1/artist"),
    # История с вложенными карточками: /history Radarr до 56 с (замер 2026-10-10).
    (RadarrClient, lambda c: c.get_history(include_details=True), "GET", "/api/v3/history"),
    (SonarrClient, lambda c: c.get_history(), "GET", "/api/v3/history"),
    (RadarrClient, lambda c: c.get_history_since(_SINCE), "GET", "/api/v3/history/since"),
    (LidarrClient, lambda c: c.get_grab_history(), "GET", "/api/v1/history"),
    # Те же выборки со вложенными карточками и большой страницей.
    (RadarrClient, lambda c: c.get_wanted_movies(), "GET", "/api/v3/wanted/missing"),
    (SonarrClient, lambda c: c.get_wanted_episodes(), "GET", "/api/v3/wanted/missing"),
    (RadarrClient, lambda c: c.get_calendar(), "GET", "/api/v3/calendar"),
    (SonarrClient, lambda c: c.get_calendar(), "GET", "/api/v3/calendar"),
    (LidarrClient, lambda c: c.get_calendar(), "GET", "/api/v1/calendar"),
    (RadarrClient, lambda c: c.get_queue(), "GET", "/api/v3/queue"),
    (RadarrClient, lambda c: c.find_in_queue("x"), "GET", "/api/v3/queue"),
    (LidarrClient, lambda c: c.album_in_queue(1), "GET", "/api/v1/queue"),
    (LidarrClient, lambda c: c.get_albums(1), "GET", "/api/v1/album"),
    # Запись: *arr сам ходит к индексеру, клиенту загрузки, диску.
    (RadarrClient, lambda c: c.grab_release("g", 1), "POST", "/api/v3/release"),
    (RadarrClient, lambda c: c.push_release("t", "http://example.com/x"), "POST", "/api/v3/release/push"),
    (SonarrClient, lambda c: c.set_season_monitoring(1, "all"), "POST", "/api/v3/seasonpass"),
    (SonarrClient, lambda c: c.monitor_season(1, 1, monitor_series=True), "PUT", "/api/v3/series/1"),
    (RadarrClient, lambda c: c.set_movie_monitored(1, True), "PUT", "/api/v3/movie/1"),
    (RadarrClient, lambda c: c.delete_movie(1), "DELETE", "/api/v3/movie/1"),
]


@pytest.mark.parametrize(
    ("client_cls", "call", "method", "path"),
    _SLOW_CALLS,
    ids=[f"{c[0].__name__[:-6]}-{c[2]}-{c[3]}" for c in _SLOW_CALLS],
)
async def test_slow_endpoints_get_the_slow_timeout(client_cls, call, method, path):
    """Эти вызовы проходили только потому, что таймаута не было вовсе."""
    seen: list[httpx.Request] = []
    client = _wire(client_cls(URL, "key"), _generic_ok(seen))

    with contextlib.suppress(APIError):  # пустой ответ на add — не предмет теста
        await call(client)

    hits = [r for r in seen if r.method == method and r.url.path == path]
    assert hits, [f"{r.method} {r.url.path}" for r in seen]
    timeouts = hits[-1].extensions["timeout"]
    assert timeouts["read"] >= 90
    # Долгим бывает ответ, а не рукопожатие: мёртвый хост не держит лишнего.
    assert timeouts["connect"] == 30.0


@pytest.mark.parametrize(
    ("factory", "call", "read"),
    [
        (lambda: RadarrClient(URL, "key"), lambda c: c.get_releases(1), _SEARCH_TIMEOUT),
        (lambda: TorrServerClient(URL, "u", "p", search_timeout=60.0), lambda c: c.search("x"), 60.0),
    ],
    ids=["release", "torrserver"],
)
async def test_explicit_search_timeouts_are_not_shortened(search_settings, factory, call, read):
    seen: list[httpx.Request] = []
    client = _wire(factory(), _generic_ok(seen))

    await call(client)

    assert seen[0].extensions["timeout"]["read"] == read


@pytest.mark.parametrize(
    ("call", "method"),
    [
        (lambda c: c.lookup_movie("x"), "GET"),
        (lambda c: c.add_movie(_MOVIE, 1, "/m"), "POST"),
        (lambda c: c.delete_movie(1), "DELETE"),
    ],
    ids=["lookup", "add", "delete"],
)
async def test_slow_timeout_is_never_shorter_than_http_timeout(call, method):
    """HTTP_TIMEOUT без верхней границы: при 120 с «долгий» вызов не должен
    получать меньше обычного."""
    seen: list[httpx.Request] = []
    client = _wire(RadarrClient(URL, "key"), _generic_ok(seen), http_timeout=120.0)

    with contextlib.suppress(APIError):
        await call(client)

    hit = next(r for r in seen if r.method == method)
    assert hit.extensions["timeout"]["read"] == 120.0


# --------------------------------------------------------------- PERF-01
@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.get_releases(1),
        lambda c: c.lookup_movie("x"),
        lambda c: c.get_history(),
    ],
    ids=["release", "lookup", "history"],
)
async def test_slow_reads_are_not_retried_on_a_read_timeout(no_sleep, search_settings, call):
    """PERF-01: *arr ещё работает над первым запросом — второй лишь множит
    нагрузку и трату квоты индексаторов."""
    seen: list[httpx.Request] = []
    client = _wire(RadarrClient(URL, "key"), _raising(httpx.ReadTimeout, seen))

    with pytest.raises(ServiceConnectionError):
        await call(client)

    assert len(seen) == 1
    assert client.breaker.is_down


async def test_slow_read_still_retries_when_the_connection_never_opened(no_sleep, search_settings):
    """Запрос до сервера не дошёл — повтор ничего не удваивает."""
    seen: list[httpx.Request] = []
    client = _wire(RadarrClient(URL, "key"), _raising(httpx.ConnectError, seen))

    with pytest.raises(ServiceConnectionError):
        await client.get_releases(1)

    assert len(seen) == 3


@pytest.mark.parametrize(
    ("error", "attempts"),
    [
        (httpx.ConnectTimeout, 3),
        (httpx.PoolTimeout, 3),
        (httpx.ReadError, 3),
        (httpx.RemoteProtocolError, 3),
        (httpx.WriteError, 3),
        (httpx.ReadTimeout, 1),
        (httpx.WriteTimeout, 1),
    ],
)
async def test_slow_get_retries_everything_but_read_and_write_timeouts(no_sleep, error, attempts):
    """Обрыв падает быстро, и довод «3 × 90 с» к нему не относится."""
    seen: list[httpx.Request] = []
    client = _wire(RadarrClient(URL, "key"), _raising(error, seen))

    with pytest.raises(ServiceConnectionError):
        await client.get_history()

    assert len(seen) == attempts


async def test_one_drop_does_not_lose_the_queue_check(no_sleep):
    """Стухшее соединение после рестарта Radarr не должно выглядеть как «релиза
    нет в очереди»: дальше автопоиск и вторая закачка."""
    handler, calls = _drops_once({"records": [{"title": TITLE}]})
    client = _wire(RadarrClient(URL, "key"), handler)

    assert await client.find_in_queue(TITLE) is True
    assert len(calls) == 2
    assert not client.breaker.is_down


@pytest.mark.parametrize(
    ("client_cls", "call", "row", "id_attr"),
    [
        (RadarrClient, lambda c: c.lookup_movie("x"), {"tmdbId": 7, "title": "X"}, "tmdb_id"),
        (SonarrClient, lambda c: c.lookup_series("x"), {"tvdbId": 7, "title": "X"}, "tvdb_id"),
        (LidarrClient, lambda c: c.lookup_artist("x"), {"foreignArtistId": "7", "artistName": "X"}, "mb_id"),
    ],
    ids=["radarr", "sonarr", "lidarr"],
)
async def test_one_drop_does_not_lose_a_lookup(no_sleep, client_cls, call, row, id_attr):
    handler, _calls = _drops_once([row])
    client = _wire(client_cls(URL, "key"), handler)

    found = await call(client)

    assert [str(getattr(item, id_attr)) for item in found] == ["7"]
    assert not client.breaker.is_down


@pytest.mark.parametrize("error", [httpx.ReadError, httpx.RemoteProtocolError])
async def test_fan_out_searches_stay_strict_on_a_drop(no_sleep, search_settings, error):
    """Обрыв не значит, что рассылку по индексаторам не начали: второй раз не шлём."""
    release: list[httpx.Request] = []
    torznab: list[httpx.Request] = []

    with pytest.raises(ServiceConnectionError):
        await _wire(RadarrClient(URL, "key"), _raising(error, release)).get_releases(1)
    with pytest.raises(TorrServerError):
        await _wire(TorrServerClient(URL, "u", "p"), _raising(error, torznab)).search("x")

    assert (len(release), len(torznab)) == (1, 1)


async def test_drop_on_grab_post_does_not_cost_the_queue_check_its_retry(no_sleep):
    """Обрыв на POST и обрыв на проверке очереди — один и тот же стухший пул."""
    posts: list[httpx.Request] = []
    gets: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append(request)
            raise httpx.ReadError("сбой", request=request)
        gets.append(request)
        if len(gets) == 1:
            raise httpx.RemoteProtocolError("сбой", request=request)
        return httpx.Response(200, json={"records": [{"title": TITLE}]})

    radarr = _wire(RadarrClient(URL, "key"), handler)

    ok, _action = await AddService(radarr, AsyncMock()).grab_release(
        _arr_release(), ContentType.MOVIE, arr_id=15,
    )

    assert ok is True
    assert (len(posts), len(gets)) == (1, 2)
    assert not radarr.breaker.is_down


@pytest.mark.parametrize(
    ("payload", "call"),
    [
        ({"action": "list"}, lambda c: c.list_torrents()),
        ({"action": "get", "hash": "h"}, lambda c: c.get_torrent("h")),
        ({"action": "get"}, lambda c: c.get_server_settings()),
    ],
    ids=["list", "get", "settings"],
)
async def test_torrserver_read_only_posts_keep_their_timeout_retry(no_sleep, payload, call):
    seen: list[httpx.Request] = []
    client = _wire(TorrServerClient(URL, "u", "p"), _raising(httpx.ReadTimeout, seen))

    with pytest.raises(TorrServerError):
        await call(client)

    assert len(seen) == 3
    assert json.loads(seen[0].content) == payload


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.add_torrent("magnet:?xt=urn:btih:abc", "X"),
        lambda c: c.remove_torrent("h"),
    ],
    ids=["add", "rem"],
)
async def test_torrserver_changing_posts_stay_single_attempt(no_sleep, call):
    seen: list[httpx.Request] = []
    client = _wire(TorrServerClient(URL, "u", "p"), _raising(httpx.ReadTimeout, seen))

    with pytest.raises(TorrServerError):
        await call(client)

    assert len(seen) == 1


@pytest.mark.parametrize(("error", "attempts"), [(httpx.ReadTimeout, 1), (httpx.ConnectError, 3)])
async def test_torrserver_search_is_retried_only_when_not_sent(no_sleep, error, attempts):
    """PERF-01: поиск TorrServer — та же веерная рассылка по индексаторам."""
    seen: list[httpx.Request] = []
    client = _wire(TorrServerClient(URL, "u", "p"), _raising(error, seen))

    with pytest.raises(TorrServerError):
        await client.search("x")

    assert len(seen) == attempts


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.search_movie(1),
        lambda c: c.delete_movie(1),
        lambda c: c.set_movie_monitored(1, True),
    ],
    ids=["command", "delete", "put"],
)
async def test_mutations_are_not_repeated_after_a_read_timeout(no_sleep, call):
    """С настоящими таймаутами повтор стал достижим: второй POST /command — ещё
    один обход индексаторов, второй DELETE — 404 по удалённому тайтлу."""
    mutations: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"id": 1})
        mutations.append(request)
        raise httpx.ReadTimeout("сбой", request=request)

    client = _wire(RadarrClient(URL, "key"), handler)

    with pytest.raises(ServiceConnectionError):
        await call(client)

    assert len(mutations) == 1


async def test_post_still_retries_when_the_connection_never_opened(no_sleep):
    seen: list[httpx.Request] = []
    client = _wire(RadarrClient(URL, "key"), _raising(httpx.ConnectError, seen))

    with pytest.raises(ServiceConnectionError):
        await client.search_movie(1)

    assert len(seen) == 3


# --------------------------------------------------------------- BUG-06
async def test_get_retries_a_dropped_connection(no_sleep):
    """Стухшее keep-alive соединение — обычное дело после простоя."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ReadError("сбой", request=request)
        return httpx.Response(200, json={"ok": True})

    client = _wire(RadarrClient(URL, "key"), handler)

    assert await client.get("/api/v3/system/status") == {"ok": True}
    assert len(calls) == 2
    assert not client.breaker.is_down


@pytest.mark.parametrize("error", [httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError])
async def test_transport_errors_count_in_the_breaker(no_sleep, error):
    client = _wire(RadarrClient(URL, "key"), _raising(error))

    with pytest.raises(ServiceConnectionError):
        await client.get("/api/v3/system/status")

    assert client.breaker.is_down


async def test_transport_errors_count_in_the_breaker_for_background_calls():
    seen: list[httpx.Request] = []
    client = _wire(RadarrClient(URL, "key"), _raising(httpx.RemoteProtocolError, seen))

    with background_calls(), pytest.raises(ServiceConnectionError):
        await client.get("/api/v3/system/status")

    assert len(seen) == 1
    assert client.breaker.is_down


def _arr_release(**kw) -> SearchResult:
    data = dict(
        guid="g1", title=TITLE, indexer="RuTracker", origin="arr", indexer_id=3,
        size=1024, quality=QualityInfo(resolution="2160p"),
    )
    data.update(kw)
    return SearchResult(**data)


def _grab_handler(seen: list[httpx.Request], queue_titles: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST":
            raise httpx.ReadError("Radarr перезапускается", request=request)
        return httpx.Response(200, json={"records": [{"title": t} for t in queue_titles]})

    return handler


async def test_read_error_on_grab_finds_the_release_in_the_queue():
    """BUG-06: `ReadError` на граб-POST раньше пролетал мимо проверки очереди, и
    пользователю предлагали автопоиск — возможна вторая закачка."""
    seen: list[httpx.Request] = []
    radarr = _wire(RadarrClient(URL, "key"), _grab_handler(seen, [TITLE]))

    ok, action = await AddService(radarr, AsyncMock()).grab_release(
        _arr_release(), ContentType.MOVIE, arr_id=15,
    )

    assert ok is True and action.success is True
    assert [r.method for r in seen] == ["POST", "GET"]


async def test_read_error_on_grab_not_in_queue_is_unreachable():
    seen: list[httpx.Request] = []
    radarr = _wire(RadarrClient(URL, "key"), _grab_handler(seen, ["другое"]))

    ok, action = await AddService(radarr, AsyncMock()).grab_release(
        _arr_release(), ContentType.MOVIE, arr_id=15,
    )

    assert ok is False
    assert grab_outcome(action) == GRAB_OUTCOME_UNREACHABLE


@pytest.mark.parametrize(("landed", "expected"), [(True, True), (False, False)])
async def test_second_grab_after_404_checks_the_queue_too(landed, expected):
    """Повторный граб после 404 раньше не мог упасть по таймауту — он висел."""
    radarr = AsyncMock()
    radarr.grab_release.side_effect = [
        APIError("Ошибка Radarr: 404", status_code=404),
        ServiceConnectionError("Таймаут соединения с Radarr"),
    ]
    radarr.get_releases.return_value = [_arr_release()]
    radarr.find_in_queue.return_value = landed

    ok, action = await AddService(radarr, AsyncMock()).grab_release(
        _arr_release(), ContentType.MOVIE, arr_id=15,
    )

    assert ok is expected
    if not landed:
        assert grab_outcome(action) == GRAB_OUTCOME_UNREACHABLE


@pytest.mark.parametrize(("landed", "expected"), [(True, True), (False, False)])
async def test_push_lost_answer_checks_the_queue(landed, expected):
    """Push тоже мог дойти: «нет вердикта» без проверки очереди вело к автопоиску."""
    radarr = AsyncMock()
    radarr.push_release.side_effect = ServiceConnectionError("Таймаут соединения с Radarr")
    radarr.find_in_queue.return_value = landed
    release = _arr_release(origin="prowlarr", download_url="http://example.com/x.torrent")

    with patch("bot.services.add_service._validate_download_url", new=AsyncMock(return_value=True)):
        ok, action = await AddService(radarr, AsyncMock()).grab_release(
            release, ContentType.MOVIE, arr_id=15,
        )

    assert ok is expected
    if not landed:
        assert grab_outcome(action) == GRAB_OUTCOME_UNREACHABLE


async def test_grab_request_error_message_stays_domain():
    client = _wire(RadarrClient(URL, "key"), _raising(httpx.RemoteProtocolError))

    with pytest.raises(ServiceConnectionError):
        await client.grab_release("g", 1)


@pytest.mark.parametrize("background", [False, True], ids=["interactive", "background"])
async def test_empty_timeout_message_never_reaches_the_logs(no_sleep, background):
    """У настоящих таймаутов httpx `str(e)` пустой: error='' в service_down."""
    client = _wire(RadarrClient(URL, "key"), _raising_empty(httpx.ReadTimeout))

    with structlog.testing.capture_logs() as logs, pytest.raises(ServiceConnectionError):
        if background:
            with background_calls():
                await client.get("/api/v3/system/status")
        else:
            await client.get("/api/v3/system/status")

    errors = {
        e["event"]: e["error"] for e in logs
        if e["event"] in ("service_down", "request_retries_exhausted", "request_retry_attempt")
    }
    assert "service_down" in errors
    assert all(errors.values()), errors


async def test_prowlarr_dropped_connection_is_a_domain_error(search_settings):
    """Поиск Prowlarr идёт мимо `_safe_request` и ловит ошибки сам."""
    client = _wire(ProwlarrClient(URL, "key"), _raising(httpx.ReadError))

    with pytest.raises(ServiceConnectionError):
        await client.search("x")


async def test_torrserver_echo_retries_a_dropped_connection(no_sleep):
    """`_fetch_echo` обещает ту же политику, что и `_request`."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ReadError("сбой", request=request)
        return httpx.Response(200, text="MatriX.142.2")

    client = _wire(TorrServerClient(URL, "u", "p"), handler)

    assert await client.get_version() == "MatriX.142.2"


# ------------------------------------------------- qBittorrent (свой httpx)
def _qbit(handler, *, authenticated: bool = True) -> QBittorrentClient:
    client = QBittorrentClient(URL, "user", "secret")
    client._client = httpx.AsyncClient(base_url=URL, transport=httpx.MockTransport(handler))
    _wired.append(client._client)
    client._authenticated = authenticated
    return client


@pytest.mark.parametrize("error", [httpx.ReadError, httpx.RemoteProtocolError])
async def test_qbit_transport_errors_are_domain_errors(error):
    with pytest.raises(QBittorrentError):
        await _qbit(_raising(error)).get_version()


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ReadTimeout, httpx.ReadError])
async def test_qbit_failed_login_is_a_domain_error(no_sleep, error):
    """BUG-06: логин шёл вне try в `_request`, и наружу летел сырой httpx."""
    with pytest.raises(QBittorrentError):
        await _qbit(_raising(error), authenticated=False).get_version()


async def test_qbit_transport_error_on_the_reissued_request_is_a_domain_error():
    answers = iter([httpx.Response(403)])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(200, text="Ok.", headers={"Set-Cookie": "SID=abc; Path=/"})
        answer = next(answers, None)
        if answer is None:
            raise httpx.ReadError("сбой", request=request)
        return answer

    with pytest.raises(QBittorrentError):
        await _qbit(handler).get_version()


async def test_qbit_hung_server_is_bounded():
    """Клиент qBittorrent NET-01 не касался: таймаут и так брался из его httpx."""
    async with _hung_server() as (url, _accepted):
        client = QBittorrentClient(url, "user", "secret", timeout=0.3)
        client._authenticated = True
        try:
            with pytest.raises(QBittorrentError):
                await asyncio.wait_for(client.get_version(), timeout=5)
        finally:
            await client.close()


async def test_emby_hung_server_is_bounded(no_sleep):
    """Emby — тоже свой httpx без `timeout=None`, таймаут работал и раньше."""
    async with _hung_server() as (url, accepted):
        client = EmbyClient(url, "key", timeout=0.2)
        try:
            with pytest.raises(EmbyError):
                await asyncio.wait_for(client.get_server_info(), timeout=5)
        finally:
            await client.close()

    assert len(accepted) == 3


def _emby(handler) -> EmbyClient:
    client = EmbyClient(URL, "key")
    client._client = httpx.AsyncClient(base_url=URL, transport=httpx.MockTransport(handler))
    _wired.append(client._client)
    return client


@pytest.mark.parametrize("error", [httpx.ReadError, httpx.RemoteProtocolError])
async def test_emby_dropped_connection_is_a_domain_error_without_retry(no_sleep, error):
    seen: list[httpx.Request] = []

    with pytest.raises(EmbyError) as caught:
        await _emby(_raising(error, seen)).get_sessions()

    assert not isinstance(caught.value, EmbyConnectionError)
    assert len(seen) == 1


@pytest.mark.parametrize(
    ("call", "path", "flag"),
    [
        (lambda c: c.restart_server(), "/System/Restart", "CanSelfRestart"),
        (lambda c: c.install_update(), "/System/Update", "CanSelfUpdate"),
    ],
    ids=["restart", "update"],
)
async def test_emby_system_actions_are_not_repeated_after_a_timeout(no_sleep, call, path, flag):
    """Сервер мог принять команду: второй POST перезапустит его ещё раз."""
    posts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={flag: True, "HasUpdateAvailable": True})
        posts.append(request)
        raise httpx.ReadTimeout("сбой", request=request)

    with pytest.raises(EmbyError):
        await call(_emby(handler))

    assert [r.url.path for r in posts] == [path]


async def test_emby_library_refresh_keeps_its_retries(no_sleep):
    seen: list[httpx.Request] = []

    with pytest.raises(EmbyError):
        await _emby(_raising(httpx.ReadTimeout, seen)).scan_library()

    assert len(seen) == 3


# --------------------------------------------------------------- BUG-07
def _found_movie() -> dict:
    return {"tmdbId": 1, "title": "X", "year": 2021, "id": 15}


_ADDS = [
    (RadarrClient, lambda c: c.add_movie(_MOVIE, 1, "/m"), "/api/v3/movie", _found_movie(), "radarr_id"),
    (
        SonarrClient, lambda c: c.add_series(_SERIES, 1, "/s"), "/api/v3/series",
        {"tvdbId": 1, "title": "X", "id": 15}, "sonarr_id",
    ),
    (
        LidarrClient, lambda c: c.add_artist(_ARTIST, 1, 1, "/a"), "/api/v1/artist",
        {"foreignArtistId": "m", "artistName": "X", "id": 15}, "lidarr_id",
    ),
]
_ADD_IDS = ["radarr", "sonarr", "lidarr"]


def _add_handler(path: str, post, found: list[dict], posts: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == path
        if request.method == "POST":
            posts.append(request)
            return post(request)
        return httpx.Response(200, json=found)

    return handler


def _lost_answer(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("ответ не успел", request=request)


def _already_added(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(400, json=[{"errorMessage": "This title has already been added"}])


@pytest.mark.parametrize(("client_cls", "call", "path", "found", "id_attr"), _ADDS, ids=_ADD_IDS)
@pytest.mark.parametrize("post", [_lost_answer, _already_added], ids=["lost-answer", "already-added"])
async def test_add_post_once_then_recheck_finds_the_title(no_sleep, client_cls, call, path, found, id_attr, post):
    """BUG-07: POST дошёл, ответ нет. Повтора быть не должно, а тайтл в
    библиотеке — это успех, а не ошибка."""
    posts: list[httpx.Request] = []
    client = _wire(client_cls(URL, "key"), _add_handler(path, post, [found], posts))

    added = await call(client)

    assert getattr(added, id_attr) == 15
    assert len(posts) == 1


@pytest.mark.parametrize(("client_cls", "call", "path", "found", "id_attr"), _ADDS, ids=_ADD_IDS)
async def test_add_recheck_empty_keeps_the_original_error(no_sleep, client_cls, call, path, found, id_attr):
    posts: list[httpx.Request] = []
    client = _wire(client_cls(URL, "key"), _add_handler(path, _lost_answer, [], posts))

    with pytest.raises(ServiceConnectionError):
        await call(client)

    assert len(posts) == 1


@pytest.mark.parametrize(("client_cls", "call", "path", "found", "id_attr"), _ADDS, ids=_ADD_IDS)
async def test_add_recheck_ignores_another_title(no_sleep, client_cls, call, path, found, id_attr):
    """Сервис, игнорирующий фильтр, не должен выдать чужой тайтл за добавленный."""
    other = {k: (2 if k in ("tmdbId", "tvdbId") else "other" if k == "foreignArtistId" else v) for k, v in found.items()}
    posts: list[httpx.Request] = []
    client = _wire(client_cls(URL, "key"), _add_handler(path, _lost_answer, [other], posts))

    with pytest.raises(ServiceConnectionError):
        await call(client)


@pytest.mark.parametrize(("client_cls", "call", "path", "found", "id_attr"), _ADDS, ids=_ADD_IDS)
async def test_add_recheck_picks_the_requested_title(no_sleep, client_cls, call, path, found, id_attr):
    other = {k: (2 if k in ("tmdbId", "tvdbId") else "other" if k == "foreignArtistId" else v) for k, v in found.items()}
    other["id"] = 99
    posts: list[httpx.Request] = []
    client = _wire(client_cls(URL, "key"), _add_handler(path, _lost_answer, [other, found], posts))

    added = await call(client)

    assert getattr(added, id_attr) == 15


async def test_hung_add_post_counts_in_breaker_and_recheck_is_one_attempt(no_sleep):
    """Сервис молчал весь таймаут: перепроверка не тратит ещё 3 × 30 + 3 с."""
    gets: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            gets.append(request)
        raise httpx.ReadTimeout("", request=request)

    client = _wire(RadarrClient(URL, "key"), handler)

    with structlog.testing.capture_logs() as logs, pytest.raises(ServiceConnectionError):
        await client.add_movie(_MOVIE, 1, "/m")

    assert len(gets) == 1
    assert client.breaker.is_down
    assert all(e["error"] for e in logs if e["event"] == "service_down")


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.ConnectError, httpx.ConnectTimeout])
async def test_unanswered_grab_counts_in_breaker(error):
    client = _wire(RadarrClient(URL, "key"), _raising(error))

    with pytest.raises(ServiceConnectionError):
        await client.grab_release("g", 1)

    assert client.breaker.is_down


@pytest.mark.parametrize("error", [httpx.ReadError, httpx.RemoteProtocolError, httpx.WriteError])
async def test_single_drop_on_a_post_does_not_mark_the_service_down(error):
    """Разовый обрыв на POST — обычно стухшее keep-alive: на GET он лечится
    повтором и в breaker не попадает. Сервис жив, а breaker перевёл бы следующую
    проверку очереди на одну попытку."""
    client = _wire(RadarrClient(URL, "key"), _raising(error))

    with pytest.raises(ServiceConnectionError):
        await client.grab_release("g", 1)

    assert not client.breaker.is_down


async def test_add_recheck_failure_keeps_the_original_error(no_sleep):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(400, text="bad payload")
        raise httpx.ConnectError("сбой", request=request)

    client = _wire(RadarrClient(URL, "key"), handler)

    with pytest.raises(APIError) as caught:
        await client.add_movie(_MOVIE, 1, "/m")

    assert caught.value.status_code == 400


async def test_add_with_a_bad_key_does_not_recheck():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(401)

    client = _wire(RadarrClient(URL, "key"), handler)

    with pytest.raises(AuthenticationError):
        await client.add_movie(_MOVIE, 1, "/m")

    assert [r.method for r in seen] == ["POST"]


async def test_add_service_reports_success_after_a_lost_answer(no_sleep):
    """Весь путь: проверка «уже есть» → POST без ответа → перепроверка."""
    library: list[dict] = []
    posts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append(request)
            library.append(json.loads(request.content) | {"id": 15})
            raise httpx.ReadTimeout("ответ не успел", request=request)
        return httpx.Response(200, json=library)

    radarr = _wire(RadarrClient(URL, "key"), handler)

    added, action = await AddService(radarr, AsyncMock()).add_movie(_MOVIE, 1, "/m")

    assert added is not None and added.radarr_id == 15
    assert action.success is True
    assert len(posts) == 1
