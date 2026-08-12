"""Сценарий «добавил раздачу — опубликовал в Emby».

Главное свойство: раздача уже добавлена, поэтому ни таймаут метаданных, ни
отказ хука не превращают операцию в неудачу — меняется только текст ответа.
"""

import asyncio
import itertools
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.clients.torrserver import TorrServerError
from bot.models import SyncHookResult, TorrServerFile, TorrServerTorrent
from bot.services.torrserver_service import TorrServerService

ADDED = TorrServerTorrent(hash="abc", title="Dune 2021", stat=1,
                          stat_string="Torrent getting info")
READY = TorrServerTorrent(
    hash="abc", title="Dune 2021", stat=3, stat_string="Torrent working", size=100,
    files=[
        TorrServerFile(id=1, path="Dune/Dune.2021.mkv", length=90),
        TorrServerFile(id=2, path="Dune/Dune.srt", length=1),
    ],
)


#: Ссылка, которую пропускает SSRF-гайка (SEC-16, врезана в add_and_publish
#: 2026-08-12). Публичный IP-литерал, а не имя: путь резолвинга не
#: задействован, и тесты этого файла — про оркестрацию метаданных и синка, а
#: не про валидацию URL. Отказ гайки покрыт отдельно в tests/test_torrserver_ssrf.py.
LINK = "http://1.2.3.4/download/abc.torrent"


def _client(get_results):
    client = MagicMock()
    client.add_torrent = AsyncMock(return_value=ADDED)
    client.get_source_hosts = AsyncMock(return_value=set())
    client.get_torrent = AsyncMock(side_effect=get_results)
    client.stream_url = MagicMock(return_value="http://ts:8090/stream/Dune.2021.mkv?link=abc&index=1&play")
    return client


def _hook(status="ok"):
    hook = MagicMock()
    hook.trigger_sync = AsyncMock(return_value=SyncHookResult(status=status, duration_s=1.0))
    return hook


@pytest.mark.asyncio
async def test_waits_for_metadata_then_triggers_sync():
    client = _client([ADDED, READY])
    hook = _hook()
    service = TorrServerService(client, hook, metadata_timeout=10.0, poll_interval=0)

    result = await service.add_and_publish(LINK, "Dune 2021")

    assert result.metadata_ready is True
    assert result.sync.status == "ok"
    assert result.stream_url.endswith("index=1&play")
    hook.trigger_sync.assert_awaited_once()


@pytest.mark.asyncio
async def test_metadata_timeout_still_counts_as_added_and_skips_sync():
    """Синк по раздаче без файлов создал бы пустышку — не зовём его.

    How many times the real deadline loop polls before 0.05s elapses depends
    on scheduler/mock overhead on the machine running the test — an
    ``itertools.repeat`` keeps every poll answering "still no files" instead
    of pinning the count to a fixed list length that a fast machine can
    outrun.

    That infinite mock only terminates because ``_wait_for_files`` breaks
    out through its deadline branch. If that branch ever regresses (dropped
    or inverted), the loop would spin forever on an ever-repeating mock —
    so the call is wrapped in ``asyncio.wait_for`` (same convention as
    tests/test_r4_C5-handler-perf.py and tests/test_r4_C2-qbit.py) to turn
    "hangs forever" into a fast, obvious failure. 5s is two orders of
    magnitude above this test's own 0.05s budget, so it never adds
    flakiness on a healthy implementation.
    """
    client = _client(itertools.repeat(ADDED))
    hook = _hook()
    service = TorrServerService(client, hook, metadata_timeout=0.05, poll_interval=0.01)

    result = await asyncio.wait_for(
        service.add_and_publish(LINK, "Dune 2021"), timeout=5
    )

    assert result.torrent.hash == "abc"
    assert result.metadata_ready is False
    assert result.sync is None
    assert result.stream_url is None
    hook.trigger_sync.assert_not_awaited()


@pytest.mark.asyncio
async def test_hook_failure_does_not_break_the_add():
    client = _client([READY])
    hook = _hook(status="failed")
    service = TorrServerService(client, hook, metadata_timeout=10.0, poll_interval=0)

    result = await service.add_and_publish(LINK, "Dune 2021")

    assert result.metadata_ready is True
    assert result.sync.status == "failed"
    assert result.stream_url


@pytest.mark.asyncio
async def test_missing_hook_is_allowed():
    client = _client([READY])
    service = TorrServerService(client, None, metadata_timeout=10.0, poll_interval=0)

    result = await service.add_and_publish(LINK, "Dune 2021")

    assert result.sync is None
    assert result.metadata_ready is True


@pytest.mark.asyncio
async def test_transient_poll_error_does_not_fail_the_add():
    """Раздача уже добавлена на сервер — временная ошибка одного опроса
    (сервер занят подтягиванием метаданных, отдал 500/таймаут) не должна
    превращать успешное добавление в ошибку. Опрос должен просто продолжиться."""
    client = _client([TorrServerError("TorrServer недоступен"), READY])
    hook = _hook()
    service = TorrServerService(client, hook, metadata_timeout=10.0, poll_interval=0)

    result = await service.add_and_publish(LINK, "Dune 2021")

    assert result.metadata_ready is True
    assert result.torrent.hash == "abc"
    hook.trigger_sync.assert_awaited_once()


@pytest.mark.asyncio
async def test_release_without_video_files_has_no_stream_link():
    audio_only = TorrServerTorrent(
        hash="abc", title="OST", stat=3, stat_string="Torrent working",
        files=[TorrServerFile(id=1, path="OST/track.flac", length=10)],
    )
    client = _client([audio_only])
    service = TorrServerService(client, _hook(), metadata_timeout=10.0, poll_interval=0)

    result = await service.add_and_publish(LINK, "OST")

    assert result.metadata_ready is True
    assert result.stream_url is None
