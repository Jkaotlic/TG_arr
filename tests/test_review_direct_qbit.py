"""Final branch review (2026-09-24): a torrent the bot pushes straight into
qBittorrent (free search "direct", catalogue "Принудительно (qBit)") is never
grabbed by Radarr/Sonarr, so library_watch never announces it. It must still
get the qBit completion message even though its category is radarr/tv-sonarr.

The bot marks such torrents with the qBit tag ``tgarr-direct`` at add time;
the tag lives in qBittorrent itself, so it survives bot restarts and works for
.torrent URLs whose info-hash the bot never sees.
"""

from unittest.mock import AsyncMock, patch

import pytest

from bot.clients.qbittorrent import DIRECT_TAG, QBittorrentClient
from bot.models import ContentType
from bot.services.notification_service import NotificationService
from tests.test_add_service import _release, _service


def test_direct_tag_value():
    assert DIRECT_TAG == "tgarr-direct"


@pytest.mark.asyncio
async def test_add_torrent_url_sends_tags():
    client = QBittorrentClient("http://198.51.100.10:8080", "u", "p")
    with patch.object(client, "_request", new_callable=AsyncMock) as req:
        req.return_value = "Ok."
        await client.add_torrent_url("magnet:?xt=urn:btih:abc", category="radarr", tags=[DIRECT_TAG, "x"])
    assert req.call_args.kwargs["data"]["tags"] == "tgarr-direct,x"


@pytest.mark.asyncio
async def test_add_torrent_url_without_tags_sends_none():
    client = QBittorrentClient("http://198.51.100.10:8080", "u", "p")
    with patch.object(client, "_request", new_callable=AsyncMock) as req:
        req.return_value = "Ok."
        await client.add_torrent_url("magnet:?xt=urn:btih:abc")
    assert "tags" not in req.call_args.kwargs["data"]


@pytest.mark.asyncio
async def test_force_download_tags_the_torrent_as_direct():
    qbt = AsyncMock()
    qbt.add_torrent_url.return_value = True
    service = _service(qbt=qbt)

    ok, _ = await service.grab_release(
        _release(download_url=None, magnet_url="magnet:?xt=urn:btih:abcdef0123456789"),
        ContentType.MOVIE, arr_id=15, force_download=True,
    )

    assert ok is True
    assert DIRECT_TAG in qbt.add_torrent_url.await_args.kwargs["tags"]


async def _complete_one(category: str, tags: str) -> AsyncMock:
    qbit, sender = AsyncMock(), AsyncMock(return_value=True)
    svc = NotificationService(qbit, sender)
    svc.subscribe_user(1)
    h = "d" * 40
    svc._tracked_torrents[h] = {"completed": False, "notified": False, "name": "x", "added_on": None}
    svc._torrents_raw[h] = {"name": "Some.Release.2160p", "progress": 0.9, "state": "downloading",
                            "category": category, "tags": tags}
    qbit.get_maindata.return_value = {
        "rid": 2, "torrents": {h: {"progress": 1.0, "state": "uploading"}},
        "torrents_removed": [], "full_update": False,
    }
    await svc._check_for_completions()
    assert h not in svc._tracked_torrents
    return sender


@pytest.mark.asyncio
@pytest.mark.parametrize("category", ["radarr", "tv-sonarr"])
async def test_direct_torrent_in_arr_category_gets_the_completion_message(category):
    sender = await _complete_one(category, f"foo, {DIRECT_TAG}")
    sender.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("category", ["radarr", "tv-sonarr"])
async def test_arr_torrent_without_direct_tag_is_still_left_to_library_watch(category):
    sender = await _complete_one(category, "foo")
    sender.assert_not_awaited()
