"""M1 (improvements round 2): «🔁 Докачать» under a music summary.

slskd diagnosis 2026-09-24: Lidarr already retries a failed slskd release on
its own (blocklist + next peer; 75 of 77 albums since 03.09 imported). So the
button is offered ONLY when Lidarr's album is still incomplete
(`statistics.trackFileCount < trackCount`) AND nothing for it sits in
Lidarr's queue. The summary's album folder is mapped to a Lidarr album
through Lidarr's `grabbed` history (slskd client): by the album key of the
release title, else by normalised artist + album title. No match -> no button.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.clients.lidarr import LidarrClient
from bot.db import Database
from bot.services import music_retry
from bot.services.music_notify import BatchSummary, album_key
from bot.services.music_retry import MusicRetryService, match_album
from bot.ui.callbacks import MusicRetryCB

T0 = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
KEY = album_key("music_readonly\\Artist - Album (2023) [FLAC]")


#: Live Tubifarry (slskd) grab title shape, 2026-09-24: tags with "/" in them.
LIVE_SOURCE = "Noize MC - Выход в город (2021) [FLAC 16bit/44.1kHz] [👤 peer1 ] [⚡ 2.64MB/s ] [WEB]"


def _tubifarry(title: str) -> str:
    return f"{title} [FLAC 16bit/44.1kHz] [👤 peer1 ] [⚡ 2.64MB/s ] [WEB]"


def _grab(album_id: int = 7, source: str = _tubifarry("Artist - Album (2023)"), *,
          client: Optional[str] = "Slskd", artist: str = "Artist", title: str = "Album") -> dict:
    """A Lidarr `grabbed` history record, include{Artist,Album} shape."""
    record: dict[str, Any] = {
        "id": album_id * 10, "albumId": album_id, "sourceTitle": source,
        "eventType": "grabbed", "date": "2026-09-24T11:00:00Z",
        "artist": {"artistName": artist}, "album": {"title": title},
        "data": {},
    }
    if client is not None:
        record["data"]["downloadClient"] = client
    return record


def _summary(title: str = "Artist — Album (2023)", failed: int = 2) -> BatchSummary:
    return BatchSummary(title=title, succeeded=10, failed_names=[f"t{i}" for i in range(failed)])


class FakeLidarr:
    def __init__(self, grabs=None, *, have: int = 10, total: int = 12, queued=()) -> None:
        self.grabs = list(grabs if grabs is not None else [_grab()])
        self.have, self.total = have, total
        self.queued = set(queued)
        self.searched: list[int] = []
        self.fail_history = False

    async def get_grab_history(self, page_size: int = 100) -> list[dict]:
        if self.fail_history:
            raise RuntimeError("down")
        return list(self.grabs)

    async def get_album(self, album_id: int) -> dict:
        return {"id": album_id, "title": "Album",
                "statistics": {"trackFileCount": self.have, "trackCount": self.total}}

    async def album_in_queue(self, album_id: int) -> bool:
        return album_id in self.queued

    async def search_album(self, album_id: int) -> dict:
        await asyncio.sleep(0)
        self.searched.append(album_id)
        return {"id": 1}


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "bot.db"))
    await database.connect()
    yield database
    await database.close()


def _service(db: Database, lidarr: Any, now: list[datetime] | None = None) -> MusicRetryService:
    clock = now if now is not None else [T0]

    async def get_lidarr():
        return lidarr

    return MusicRetryService(db, get_lidarr, clock=lambda: clock[0])


# ------------------------------------------------------------------ mapping
def test_match_by_album_key_of_the_release_title():
    assert match_album(KEY, [_grab(7)]) == 7


def test_match_live_tubifarry_title_by_key_alone():
    """M-1: "/" inside the tags must not be read as a path separator."""
    key = album_key(r"music_readonly\peer1\Noize MC - Выход в город (2021)")
    assert key == "album:noize mc - выход в город (2021)"
    record = _grab(11, source=LIVE_SOURCE, artist="", title="")
    assert match_album(key, [record]) == 11


def test_match_title_with_slash_in_the_artist_by_key_alone():
    key = album_key(r"music\AC DC - Back in Black (1980) [FLAC]")
    record = _grab(12, source="AC/DC - Back in Black (1980) [FLAC]", artist="", title="")
    assert match_album(key, [record]) == 12


def test_match_falls_back_to_artist_and_album_title():
    record = _grab(9, source="some.odd.release.name", artist="Artist", title="Album")
    assert match_album(KEY, [record]) == 9


def test_grabs_of_other_download_clients_do_not_match():
    assert match_album(KEY, [_grab(7, client="qBittorrent")]) is None


def test_no_match_gives_none():
    assert match_album(KEY, [_grab(7, source="Other - Thing", artist="Other", title="Thing")]) is None


def test_loose_files_never_match():
    loose = album_key("music", "peer1")
    assert match_album(loose, [_grab(7, source="music", artist="", title="")]) is None


# ------------------------------------------------------------------ offers
@pytest.mark.asyncio
async def test_incomplete_album_with_nothing_queued_gets_a_button(db):
    offers = await _service(db, FakeLidarr()).offers([(KEY, _summary())])
    assert len(offers) == 1
    offer = offers[0]
    assert "Album" in offer.label and offer.label.startswith("🔁 Докачать: ")
    assert len(offer.token) == 6
    assert await db.kv_get(f"music_retry:{offer.token}") is not None


@pytest.mark.asyncio
async def test_complete_album_gets_no_button(db):
    assert await _service(db, FakeLidarr(have=12, total=12)).offers([(KEY, _summary())]) == []


@pytest.mark.asyncio
async def test_album_already_queued_again_gets_no_button(db):
    """Lidarr's own retry is already in the queue."""
    assert await _service(db, FakeLidarr(queued={7})).offers([(KEY, _summary())]) == []


@pytest.mark.asyncio
async def test_unmapped_album_gets_no_button(db):
    lidarr = FakeLidarr([_grab(7, source="Other - Thing", artist="Other", title="Thing")])
    assert await _service(db, lidarr).offers([(KEY, _summary())]) == []


@pytest.mark.asyncio
async def test_summary_without_failures_asks_lidarr_nothing(db):
    lidarr = FakeLidarr()
    lidarr.get_grab_history = AsyncMock(side_effect=AssertionError("no call expected"))
    assert await _service(db, lidarr).offers([(KEY, _summary(failed=0))]) == []


@pytest.mark.asyncio
async def test_no_lidarr_or_lidarr_down_gives_no_button(db):
    assert await _service(db, None).offers([(KEY, _summary())]) == []
    lidarr = FakeLidarr()
    lidarr.fail_history = True
    assert await _service(db, lidarr).offers([(KEY, _summary())]) == []


@pytest.mark.asyncio
async def test_at_most_three_buttons(db):
    keys = [album_key(f"music\\Artist - Album {n}") for n in range(5)]
    grabs = [_grab(n + 1, source=_tubifarry(f"Artist - Album {n}"), title=f"Album {n}") for n in range(5)]
    offers = await _service(db, FakeLidarr(grabs)).offers(
        [(k, _summary(f"Artist — Album {n}")) for n, k in enumerate(keys)]
    )
    assert len(offers) == 3


def test_callback_data_fits_telegram_limit():
    packed = MusicRetryCB(tok="abcdef").pack()
    assert len(packed.encode()) <= 64


# ------------------------------------------------------------------ retry
@pytest.mark.asyncio
async def test_retry_starts_album_search_once(db):
    lidarr = FakeLidarr()
    service = _service(db, lidarr)
    [offer] = await service.offers([(KEY, _summary())])

    assert await service.retry(offer.token) == "started"
    assert await service.retry(offer.token) == "already"
    assert lidarr.searched == [7]


@pytest.mark.asyncio
async def test_concurrent_double_tap_searches_once(db):
    lidarr = FakeLidarr()
    service = _service(db, lidarr)
    [offer] = await service.offers([(KEY, _summary())])

    results = await asyncio.gather(service.retry(offer.token), service.retry(offer.token))
    assert sorted(results) == ["already", "started"]
    assert lidarr.searched == [7]


@pytest.mark.asyncio
async def test_retry_is_allowed_again_after_the_cooldown(db):
    lidarr = FakeLidarr()
    clock = [T0]
    service = _service(db, lidarr, clock)
    [offer] = await service.offers([(KEY, _summary())])
    await service.retry(offer.token)
    clock[0] = T0 + timedelta(seconds=music_retry.RETRY_COOLDOWN_S + 1)
    assert await service.retry(offer.token) == "started"
    assert lidarr.searched == [7, 7]


@pytest.mark.asyncio
async def test_unknown_or_expired_token_is_stale(db):
    clock = [T0]
    service = _service(db, FakeLidarr(), clock)
    assert await service.retry("zzzzzz") == "stale"
    [offer] = await service.offers([(KEY, _summary())])
    clock[0] = T0 + music_retry.TOKEN_TTL + timedelta(seconds=1)
    assert await service.retry(offer.token) == "stale"


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [
    "not json",
    '{"album_id": 7, "issued_at": "2026-09-24T12:00:00+00:00", "retried_at": "garbage"}',
    '{"issued_at": "2026-09-24T12:00:00+00:00"}',
])
async def test_corrupt_entry_reads_as_stale(db, raw):
    """M-3: a broken notify_kv row must still get the tap answered."""
    lidarr = FakeLidarr()
    await db.kv_set("music_retry:abc123", raw)
    assert await _service(db, lidarr).retry("abc123") == "stale"
    assert lidarr.searched == []


@pytest.mark.asyncio
async def test_lidarr_failure_on_retry_is_reported_and_can_be_retried(db):
    lidarr = FakeLidarr()
    service = _service(db, lidarr)
    [offer] = await service.offers([(KEY, _summary())])
    lidarr.search_album = AsyncMock(side_effect=RuntimeError("500"))
    assert await service.retry(offer.token) == "failed"
    lidarr.search_album = AsyncMock(return_value={})
    assert await service.retry(offer.token) == "started"


@pytest.mark.asyncio
async def test_old_tokens_are_pruned_when_new_ones_are_issued(db):
    clock = [T0]
    service = _service(db, FakeLidarr(), clock)
    [old] = await service.offers([(KEY, _summary())])
    clock[0] = T0 + music_retry.TOKEN_TTL + timedelta(days=1)
    await service.offers([(KEY, _summary())])
    assert await db.kv_get(f"music_retry:{old.token}") is None


# ------------------------------------------------------------------ Lidarr client
@pytest.mark.asyncio
async def test_client_grab_history_asks_for_grabbed_with_album_and_artist():
    client = LidarrClient("http://lidarr", "key")
    with patch.object(client, "get", new=AsyncMock(return_value={"records": [_grab()]})) as get:
        records = await client.get_grab_history(page_size=50)
    assert records == [_grab()]
    assert get.call_args.args[0] == "/api/v1/history"
    params = get.call_args.kwargs["params"]
    assert params["eventType"] == 1 and params["pageSize"] == 50
    assert params["includeAlbum"] == "true" and params["includeArtist"] == "true"


@pytest.mark.asyncio
async def test_client_get_album_and_queue_check():
    client = LidarrClient("http://lidarr", "key")
    with patch.object(client, "get", new=AsyncMock(return_value={"id": 7})) as get:
        assert await client.get_album(7) == {"id": 7}
    assert get.call_args.args[0] == "/api/v1/album/7"

    queue = {"records": [{"albumId": 3}, {"albumId": 7}]}
    with patch.object(client, "get", new=AsyncMock(return_value=queue)) as get:
        assert await client.album_in_queue(7) is True
        assert await client.album_in_queue(8) is False
    assert get.call_args.args[0] == "/api/v1/queue"


# ------------------------------------------------------------------ watcher wiring
@pytest.mark.asyncio
async def test_watcher_passes_retry_offers_with_the_music_summary(db):
    from tests.test_music_notify import ALBUM_DIR, Clock, FakeSlskd, _NoHistory, _prime, _t

    from bot.services.library_watcher import LibraryWatcher

    slskd, clock, sent = FakeSlskd(), Clock(), []

    async def notify(text: str, category: str, **extra) -> None:
        sent.append((text, category, extra))

    async def get_slskd():
        return slskd

    seen_batches: list = []

    async def offers(batches):
        seen_batches.extend(batches)
        return ["OFFER"]

    watcher = LibraryWatcher(
        _NoHistory(), _NoHistory(), notify=notify, get_slskd=get_slskd, db=db, clock=clock,
        music_settle_seconds=300, music_stall_seconds=3600, poll_interval_seconds=3600,
        music_retry=offers,
    )
    await _prime(watcher, clock)
    slskd.transfers = [_t(1, "Completed, Succeeded", ended=clock.now),
                       _t(2, "Completed, Errored", ended=clock.now)]
    await watcher.poll()
    clock.advance(301)
    await watcher.poll()

    [(text, category, extra)] = sent
    assert category == "music" and extra == {"retry": ["OFFER"]}
    [(key, summary)] = seen_batches
    assert key == album_key(ALBUM_DIR) and summary.failed_names


@pytest.mark.asyncio
async def test_failing_retry_hook_still_sends_the_summary(db):
    from tests.test_music_notify import Clock, FakeSlskd, _NoHistory, _prime, _t

    from bot.services.library_watcher import LibraryWatcher

    slskd, clock, sent = FakeSlskd(), Clock(), []

    async def notify(text: str, category: str, **extra) -> None:
        sent.append((text, extra))

    async def get_slskd():
        return slskd

    async def offers(batches):
        raise RuntimeError("lidarr exploded")

    watcher = LibraryWatcher(
        _NoHistory(), _NoHistory(), notify=notify, get_slskd=get_slskd, db=db, clock=clock,
        music_settle_seconds=300, music_stall_seconds=3600, poll_interval_seconds=3600,
        music_retry=offers,
    )
    await _prime(watcher, clock)
    slskd.transfers = [_t(1, "Completed, Errored", ended=clock.now)]
    await watcher.poll()
    clock.advance(301)
    await watcher.poll()
    assert len(sent) == 1 and sent[0][1] == {}


# ------------------------------------------------------------------ delivery
@pytest.mark.asyncio
async def test_music_notice_carries_retry_buttons_and_respects_mute(db, monkeypatch):
    import structlog

    from bot import main
    from bot.models import User, UserPreferences

    monkeypatch.setattr(main, "_last_sent_at", {})
    monkeypatch.setattr(main, "get_settings", lambda: MagicMock(allowed_tg_ids=[111, 222], admin_tg_ids=[]))
    await db.create_user(User(tg_id=222, preferences=UserPreferences(notify_music="off")))
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=9))
    offer = music_retry.RetryOffer(token="abc123", label="🔁 Докачать: Artist — Album")

    await main.deliver_library_notice(
        bot, db, "🎵 <b>Artist — Album</b>: 10/12", "music", structlog.get_logger(), retry=[offer],
    )
    [call] = bot.send_message.call_args_list
    assert call.args[0] == 111
    [[button]] = call.kwargs["reply_markup"].inline_keyboard
    assert button.text == offer.label
    assert MusicRetryCB.unpack(button.callback_data) == MusicRetryCB(tok="abc123")


# ------------------------------------------------------------------ handler
def _callback(markup=None):
    callback = MagicMock()
    callback.from_user.id = 111
    callback.answer = AsyncMock()
    callback.message.reply_markup = markup
    callback.message.edit_reply_markup = AsyncMock()
    return callback


@pytest.mark.asyncio
async def test_handler_starts_search_and_answers(db, monkeypatch):
    from bot.handlers import music as music_handlers
    from bot.ui.keyboards import Keyboards

    lidarr = FakeLidarr()
    # The handler builds its own MusicRetryService on the real clock, so the
    # token is issued on it too — a fixed T0 turned stale TOKEN_TTL after T0.
    service = _service(db, lidarr, now=[datetime.now(timezone.utc)])
    [offer] = await service.offers([(KEY, _summary())])
    monkeypatch.setattr(music_handlers, "get_lidarr", AsyncMock(return_value=lidarr))

    markup = Keyboards.music_retry([offer])
    callback = _callback(markup)
    await music_handlers.handle_music_retry(callback, MusicRetryCB(tok=offer.token), db)

    callback.answer.assert_awaited_once()
    assert callback.answer.call_args.args[0] == "🔁 Lidarr ищет альбом заново"
    assert lidarr.searched == [7]
    # The pressed button leaves this message (it was the only one).
    callback.message.edit_reply_markup.assert_awaited_once()
    assert callback.message.edit_reply_markup.call_args.kwargs["reply_markup"] is None

    second = _callback(markup)
    await music_handlers.handle_music_retry(second, MusicRetryCB(tok=offer.token), db)
    assert lidarr.searched == [7]
    assert "уже ищет" in second.answer.call_args.args[0]


@pytest.mark.asyncio
async def test_handler_stale_token(db, monkeypatch):
    from bot.handlers import music as music_handlers

    monkeypatch.setattr(music_handlers, "get_lidarr", AsyncMock(return_value=FakeLidarr()))
    callback = _callback()
    await music_handlers.handle_music_retry(callback, MusicRetryCB(tok="zzzzzz"), db)
    assert callback.answer.call_args.kwargs.get("show_alert") is True
