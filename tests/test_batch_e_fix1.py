"""Batch E fix round 1 (review of E1–E3).

- An internal EMBY_URL (``http://emby:8096``) is not a valid Telegram button
  URL: no button then, and a rejected button never costs the message itself.
- Direct qBit grabs link even when the torrent name differs from the title.
- A tick's edit re-reads the row, so a concurrent import claim wins.
- Flood control: a per-chat "not before", other chats keep going.
- «Скачано, импортирую…» for a day without import → «Не импортировано».
- Digest: stuck/airing lines follow the user's mutes; the calendar starts at
  local midnight.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.db import Database
from bot.models import TorrentInfo, TorrentState, UserPreferences
from bot.services import live_status
from bot.services.digest import DigestData, render_digest
from tests.test_digest import Clock as DigestClock
from tests.test_digest import _clients, _local
from tests.test_digest import _service as digest_service
from tests.test_live_status import (
    HASH,
    Clock,
    FakeArr,
    FakeEditor,
    FakeQbit,
    _grabbed,
    _imported,
    _register,
    _service,
)


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "bot.db"))
    await database.connect()
    yield database
    await database.close()


# ------------------------------------------------------------------ IMPORTANT 1
@pytest.mark.parametrize(
    ("public", "internal", "expected"),
    [
        ("https://media.example.com", "http://emby:8096", "https://media.example.com"),
        (None, "https://emby.example.com", "https://emby.example.com"),
        (None, "http://emby:8096", None),
        (None, "http://server.lan:8096", None),
        (None, "http://nas.local:8096", None),
        (None, "http://box.home:8096", None),
        (None, "http://localhost:8096", None),
        (None, "ftp://emby.example.com", None),
        (None, None, None),
    ],
)
def test_button_base_only_for_public_looking_urls(public, internal, expected):
    assert live_status.button_base(public, internal) == expected


@pytest.mark.asyncio
async def test_edit_retries_without_markup_on_bad_button(monkeypatch):
    from aiogram.exceptions import TelegramBadRequest

    from bot import main

    monkeypatch.setattr(main, "_last_sent_at", {})
    monkeypatch.setattr(main, "_edit_not_before", {})
    bad = TelegramBadRequest(method=MagicMock(), message="Bad Request: BUTTON_URL_INVALID")
    bot = MagicMock()
    bot.edit_message_text = AsyncMock(side_effect=[bad, None])
    assert await main.edit_living_message(bot, 111, 5, "t", reply_markup=MagicMock()) == "ok"
    assert bot.edit_message_text.await_count == 2
    assert bot.edit_message_text.call_args.kwargs["reply_markup"] is None


@pytest.mark.asyncio
async def test_send_retries_without_markup_on_bad_button(monkeypatch):
    from aiogram.exceptions import TelegramBadRequest

    from bot import main

    monkeypatch.setattr(main, "_last_sent_at", {})
    bad = TelegramBadRequest(method=MagicMock(), message="Bad Request: BUTTON_URL_INVALID")
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=[bad, MagicMock(message_id=3)])
    msg = await main.send_telegram(bot, 111, "t", reply_markup=MagicMock())
    assert msg.message_id == 3
    assert bot.send_message.call_args.kwargs["reply_markup"] is None


@pytest.mark.asyncio
async def test_send_without_markup_still_raises_bad_request(monkeypatch):
    from aiogram.exceptions import TelegramBadRequest

    from bot import main

    monkeypatch.setattr(main, "_last_sent_at", {})
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=TelegramBadRequest(method=MagicMock(), message="chat not found"))
    with pytest.raises(TelegramBadRequest):
        await main.send_telegram(bot, 111, "t")
    assert bot.send_message.await_count == 1


# ------------------------------------------------------------------ IMPORTANT 2
@pytest.mark.asyncio
async def test_direct_link_falls_back_to_newest_unowned_tagged_torrent(db):
    """Tracker titles often differ from the torrent name qBit shows."""
    clock, editor, qbit = Clock(), FakeEditor(), FakeQbit()
    other = "B" * 40
    await live_status.register_grab(
        db, chat_id=222, message_id=1, kind="movie", title="Other", release="Other.Rel",
        arr="qbit", arr_id=None, guid=None, now=clock() - timedelta(minutes=5),
    )
    [owned] = await db.live_rows()
    await db.live_update(owned["key"], download_id=other, state="downloading")
    qbit.torrents = {
        # newest, but already another row's
        other.lower(): TorrentInfo(hash=other.lower(), name="x", tags=["tgarr-direct"],
                                   added_on=clock() + timedelta(seconds=20)),
        # tagged but added long before this grab
        "c" * 40: TorrentInfo(hash="c" * 40, name="old", tags=["tgarr-direct"],
                              added_on=clock() - timedelta(hours=1)),
        # not ours at all
        "d" * 40: TorrentInfo(hash="d" * 40, name="untagged", added_on=clock()),
        HASH.lower(): TorrentInfo(hash=HASH.lower(), name="Совсем другое имя", tags=["tgarr-direct"],
                                  added_on=clock() + timedelta(seconds=5), progress=0.1),
    }
    await _register(db, clock, arr="qbit", guid=None)
    await _service(db, clock, editor, qbit=qbit).tick()
    mine = [r for r in await db.live_rows() if r["chat_id"] == 111][0]
    assert mine["download_id"] == HASH


# ------------------------------------------------------------------ MINOR 3
@pytest.mark.asyncio
async def test_apply_rereads_state_so_a_concurrent_claim_wins(db):
    clock, editor, qbit = Clock(), FakeEditor(), FakeQbit()
    qbit.put(1.0, TorrentState.SEEDING)
    svc = _service(db, clock, editor, qbit=qbit)
    await _register(db, clock)
    rows = await db.live_rows()
    # the watcher imported it between the tick's read and its edit
    await db.live_update(rows[0]["key"], state="imported", download_id=HASH)
    stale = dict(rows[0], state="downloading", download_id=HASH)
    await svc._follow_torrent(stale)
    assert editor.calls == []
    assert (await db.live_rows())[0]["state"] == "imported"


# ------------------------------------------------------------------ MINOR 4
@pytest.mark.asyncio
async def test_flooded_chat_does_not_block_other_chats(db):
    clock, radarr, qbit = Clock(), FakeArr(), FakeQbit()
    calls: list[int] = []

    async def editor(chat_id, message_id, text, markup=None):
        calls.append(chat_id)
        return "retry" if chat_id == 111 else "ok"

    radarr.grabbed = [_grabbed()]
    qbit.put(1.0, TorrentState.SEEDING)
    svc = _service(db, clock, editor, radarr=radarr, qbit=qbit)
    await _register(db, clock, chat=111, msg=5)
    await _register(db, clock, chat=111, msg=6)
    await _register(db, clock, chat=222, msg=7)
    await svc.tick()
    assert calls == [111, 222]  # the second 111 row waits for the flood window


@pytest.mark.asyncio
async def test_edit_respects_flood_window_without_calling_telegram(monkeypatch):
    from aiogram.exceptions import TelegramRetryAfter

    from bot import main

    monkeypatch.setattr(main, "_last_sent_at", {})
    monkeypatch.setattr(main, "_edit_not_before", {})
    bot = MagicMock()
    bot.edit_message_text = AsyncMock(
        side_effect=TelegramRetryAfter(method=MagicMock(), message="Flood", retry_after=30),
    )
    assert await main.edit_living_message(bot, 111, 5, "t") == "retry"
    assert await main.edit_living_message(bot, 111, 5, "t") == "retry"
    assert bot.edit_message_text.await_count == 1


# ------------------------------------------------------------------ MINOR 9
@pytest.mark.asyncio
async def test_completed_without_import_for_a_day_gives_up(db):
    clock, editor, radarr, qbit = Clock(), FakeEditor(), FakeArr(), FakeQbit()
    radarr.grabbed = [_grabbed()]
    qbit.put(1.0, TorrentState.SEEDING)
    svc = _service(db, clock, editor, radarr=radarr, qbit=qbit)
    await _register(db, clock)
    await svc.tick()
    assert editor.texts[-1].endswith("✅ Скачано, импортирую…")
    clock.advance(23 * 3600)
    await svc.tick()
    assert len(editor.calls) == 1
    clock.advance(3601)
    await svc.tick()
    assert editor.texts[-1].endswith("⚠️ Не импортировано — проверь *arr")
    assert (await db.live_rows())[0]["state"] == "not_imported"
    radarr.history_calls.clear()
    qbit.calls = 0
    await svc.tick()
    assert radarr.history_calls == [] and qbit.calls == 0  # polling stopped
    # a late import still finishes the same message
    assert await svc.claim_import("radarr", HASH, [_imported()]) == {111}
    assert "в библиотеке" in editor.texts[-1]


# ------------------------------------------------------------------ MINOR 5
def test_stuck_and_airing_follow_mutes():
    data = DigestData(
        stuck=[("library", "торренты без движения больше суток: 1"),
               ("music", "музыка в очереди у пиров больше суток: 2"),
               ("library", "Radarr: 1 с предупреждением")],
        airing=["Show — S01E01"],
    )
    no_music = render_digest(data, UserPreferences(notify_music="off")) or ""
    assert "музыка в очереди" not in no_music and "Radarr" in no_music
    no_library = render_digest(data, UserPreferences(notify_library=False)) or ""
    assert "музыка в очереди" in no_library
    assert "Radarr" not in no_library and "торренты" not in no_library
    assert "Сегодня выходит" not in no_library
    assert render_digest(data, UserPreferences(notify_library=False, notify_music="off")) is None


# ------------------------------------------------------------------ MINOR 6
@pytest.mark.asyncio
async def test_calendar_starts_at_local_midnight(db):
    clients = _clients()
    clients[1].get_calendar.return_value = [
        {"series_title": "Night", "season": 1, "episode": 2, "air_date": "2026-09-24T22:30:00Z"},  # 01:30 MSK
    ]
    data = await digest_service(db, DigestClock(_local(9, 5)), clients, []).collect()
    kwargs = clients[1].get_calendar.call_args.kwargs
    assert kwargs["start"] == datetime(2026, 9, 24, 21, 0, tzinfo=timezone.utc)
    assert kwargs["days"] == 1
    assert data.airing == ["Night — S01E02"]


@pytest.mark.asyncio
async def test_sonarr_calendar_accepts_a_start_instant():
    from bot.clients.sonarr import SonarrClient

    client = SonarrClient("http://sonarr", "key")
    with patch.object(client, "get", AsyncMock(return_value=[])) as get:
        await client.get_calendar(days=1, start=datetime(2026, 9, 24, 21, 0, tzinfo=timezone.utc))
    params = get.call_args.kwargs["params"]
    assert params["start"] == "2026-09-24T21:00:00Z" and params["end"] == "2026-09-25T21:00:00Z"
