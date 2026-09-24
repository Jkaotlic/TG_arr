"""«▶️ Смотреть в Emby» (improvements round 2, E2).

After an import the item is looked up in Emby by provider id (TMDb for
movies, TVDB for series); found → a URL button on the living message and on
the plain «в библиотеке» notice. Emby may still be scanning, so the lookup is
retried a few times over ~5 minutes, then given up silently.
"""

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.db import Database
from bot.services import live_status
from bot.services.live_status import LiveStatusService
from bot.ui.keyboards import Keyboards

T0 = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
HASH = "ABCDEF0123456789ABCDEF0123456789ABCDEF01"


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "bot.db"))
    await database.connect()
    yield database
    await database.close()


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


class Editor:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int, str, Any]] = []

    async def __call__(self, chat_id, message_id, text, markup=None):
        self.calls.append((chat_id, message_id, text, markup))
        return "ok"


# ------------------------------------------------------------------ client
def _emby():
    from bot.clients.emby import EmbyClient

    return EmbyClient("http://emby.example.com:8096", "key")


@pytest.mark.asyncio
async def test_find_item_by_provider_id_checks_the_returned_ids():
    client = _emby()
    items = {"Items": [{"Id": "abc", "ProviderIds": {"Tmdb": "603"}}]}
    with patch.object(client, "_request", AsyncMock(return_value=items)) as req:
        assert await client.find_item_id("tmdb.603") == "abc"
    params = req.call_args.kwargs["params"]
    assert params["Recursive"] == "true"
    assert "tmdb.603" in params["AnyProviderIdEquals"] and "Tmdb.603" in params["AnyProviderIdEquals"]


@pytest.mark.asyncio
async def test_find_item_ignores_an_item_with_other_ids():
    """If Emby ignored the filter it would return some first item — never link it."""
    client = _emby()
    items = {"Items": [{"Id": "zzz", "ProviderIds": {"Tmdb": "999"}}]}
    with patch.object(client, "_request", AsyncMock(return_value=items)):
        assert await client.find_item_id("tmdb.603") is None


@pytest.mark.asyncio
async def test_find_item_swallows_errors():
    client = _emby()
    with patch.object(client, "_request", AsyncMock(side_effect=RuntimeError("down"))):
        assert await client.find_item_id("tvdb.1") is None


@pytest.mark.asyncio
async def test_watch_url_uses_public_base_and_server_id():
    client = _emby()
    responses = {
        "/Items": {"Items": [{"Id": "abc", "ProviderIds": {"Tvdb": "81189"}}]},
        "/System/Info/Public": {"Id": "srv1"},
    }

    async def fake(method, endpoint, params=None, json_data=None):
        return responses[endpoint]

    with patch.object(client, "_request", side_effect=fake) as req:
        url = await client.watch_url("tvdb.81189", public_base="https://media.example.com")
        again = await client.watch_url("tvdb.81189", public_base="https://media.example.com")
    assert url == "https://media.example.com/web/index.html#!/item?id=abc&serverId=srv1"
    assert again == url
    # the server id is fetched once
    assert [c.args[1] for c in req.call_args_list].count("/System/Info/Public") == 1


@pytest.mark.asyncio
async def test_watch_url_none_when_not_found():
    client = _emby()
    with patch.object(client, "_request", AsyncMock(return_value={"Items": []})):
        assert await client.watch_url("tmdb.1", public_base="https://media.example.com") is None


# ------------------------------------------------------------------ config
def test_public_url_falls_back_to_emby_url(monkeypatch):
    from bot.config import Settings

    monkeypatch.setenv("EMBY_URL", "http://emby.example.com:8096/")
    monkeypatch.setenv("EMBY_API_KEY", "k")
    monkeypatch.setenv("EMBY_PUBLIC_URL", "")
    assert Settings().emby_watch_base == "http://emby.example.com:8096"
    monkeypatch.setenv("EMBY_PUBLIC_URL", "https://media.example.com/")
    assert Settings().emby_watch_base == "https://media.example.com"


# ------------------------------------------------------------------ keyboard
def test_watch_button_is_a_url_button():
    kb = Keyboards.watch_in_emby("https://media.example.com/web/index.html#!/item?id=a&serverId=b")
    [[button]] = kb.inline_keyboard
    assert button.text == "▶️ Смотреть в Emby"
    assert button.url.startswith("https://media.example.com/")


# ------------------------------------------------------------------ living message
async def _imported_row(db, clock, svc):
    await live_status.register_grab(
        db, chat_id=111, message_id=5, kind="movie", title="Movie (2020)", release="Rel",
        arr="radarr", arr_id=7, guid="g", provider_id="tmdb.603", now=clock(),
    )
    [row] = await db.live_rows()
    await db.live_update(row["key"], download_id=HASH, state="completed")
    return await svc.claim_import("radarr", HASH, [{"downloadId": HASH}])


@pytest.mark.asyncio
async def test_button_present_when_found(db):
    clock, editor = Clock(), Editor()
    link = AsyncMock(return_value="https://media.example.com/x")
    svc = LiveStatusService(db, editor, clock=clock, emby_link=link, make_markup=Keyboards.watch_in_emby)
    await _imported_row(db, clock, svc)
    text, markup = editor.calls[-1][2], editor.calls[-1][3]
    assert "в библиотеке" in text
    assert markup.inline_keyboard[0][0].url == "https://media.example.com/x"
    assert (await db.live_rows())[0]["state"] == "imported"


@pytest.mark.asyncio
async def test_button_added_later_after_emby_scans(db):
    clock, editor = Clock(), Editor()
    link = AsyncMock(side_effect=[None, None, "https://media.example.com/x"])
    svc = LiveStatusService(db, editor, clock=clock, emby_link=link, make_markup=Keyboards.watch_in_emby)
    await _imported_row(db, clock, svc)
    assert editor.calls[-1][3] is None  # «в библиотеке» at once, no button yet
    for _ in range(2):
        clock.now += timedelta(seconds=61)
        await svc.tick()
    assert editor.calls[-1][3] is not None
    assert len(editor.calls) == 2  # only the button edit on top
    assert (await db.live_rows())[0]["state"] == "imported"


@pytest.mark.asyncio
async def test_gives_up_silently_without_button(db):
    clock, editor = Clock(), Editor()
    link = AsyncMock(return_value=None)
    svc = LiveStatusService(db, editor, clock=clock, emby_link=link, make_markup=Keyboards.watch_in_emby)
    await _imported_row(db, clock, svc)
    for _ in range(10):
        clock.now += timedelta(seconds=61)
        await svc.tick()
    assert len(editor.calls) == 1 and editor.calls[0][3] is None
    assert link.await_count == 5
    assert (await db.live_rows())[0]["state"] == "imported"


@pytest.mark.asyncio
async def test_plain_notice_gets_its_button_in_place(db):
    clock, editor = Clock(), Editor()
    link = AsyncMock(side_effect=[None, "https://media.example.com/x"])
    svc = LiveStatusService(db, editor, clock=clock, emby_link=link, make_markup=Keyboards.watch_in_emby)
    assert await svc.emby_url("tmdb.603") is None
    await svc.register_notice(222, 9, "✅ <b>Rel</b> — в библиотеке.", "tmdb.603", "radarr")
    clock.now += timedelta(seconds=61)
    await svc.tick()
    assert editor.calls == [(222, 9, "✅ <b>Rel</b> — в библиотеке.", editor.calls[0][3])]
    assert editor.calls[0][3].inline_keyboard[0][0].url == "https://media.example.com/x"


# ------------------------------------------------------------------ library notice delivery
@pytest.mark.asyncio
async def test_library_notice_carries_button_when_found(db, monkeypatch):
    import structlog

    from bot import main

    monkeypatch.setattr(main, "_last_sent_at", {})
    monkeypatch.setattr(main, "get_settings", lambda: MagicMock(allowed_tg_ids=[111], admin_tg_ids=[]))
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=9))
    live = LiveStatusService(
        db, Editor(), emby_link=AsyncMock(return_value="https://media.example.com/x"),
        make_markup=Keyboards.watch_in_emby,
    )
    await main.deliver_library_notice(
        bot, db, "✅ <b>X</b> — в библиотеке.", "library", structlog.get_logger(),
        provider="tmdb.603", live=live,
    )
    markup = bot.send_message.call_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].url == "https://media.example.com/x"
    assert await db.live_rows() == []


@pytest.mark.asyncio
async def test_library_notice_without_button_is_queued_for_retry(db, monkeypatch):
    import structlog

    from bot import main

    monkeypatch.setattr(main, "_last_sent_at", {})
    monkeypatch.setattr(main, "get_settings", lambda: MagicMock(allowed_tg_ids=[111], admin_tg_ids=[]))
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=9))
    live = LiveStatusService(
        db, Editor(), emby_link=AsyncMock(return_value=None), make_markup=Keyboards.watch_in_emby,
    )
    await main.deliver_library_notice(
        bot, db, "✅ <b>X</b> — в библиотеке.", "library", structlog.get_logger(),
        provider="tmdb.603", live=live,
    )
    assert bot.send_message.call_args.kwargs.get("reply_markup") is None
    [row] = await db.live_rows()
    assert (row["chat_id"], row["message_id"], row["state"], row["kind"]) == (111, 9, "emby", "notice")


@pytest.mark.asyncio
async def test_watcher_reads_history_with_details_and_passes_provider():
    from bot.services.library_watcher import LibraryWatcher

    radarr, sonarr = AsyncMock(), AsyncMock()
    radarr.get_history.return_value = [{
        "id": 1, "downloadId": HASH, "sourceTitle": "Rel", "date": "2026-09-24T12:00:00Z",
        "movie": {"tmdbId": 603, "title": "Movie"},
    }]
    sonarr.get_history.return_value = []
    sent: list[dict] = []

    async def notify(text, category, **extra):
        sent.append(extra)

    watcher = LibraryWatcher(radarr, sonarr, notify=notify)
    watcher._primed_services.update({"radarr", "sonarr"})
    await watcher.poll()
    assert radarr.get_history.call_args.kwargs.get("include_details") is True
    assert sent == [{"provider": "tmdb.603"}]
