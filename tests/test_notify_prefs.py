"""Per-user notification preferences (audit 2026-09-23, NOTIF-06).

The user asked not to be messaged about music downloads; there was no way to
say so. `UserPreferences.notify_music` ("summary"/"off") and
`notify_library` (bool) now gate who receives each category.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.db import Database
from bot.handlers import settings as settings_handlers
from bot.models import User, UserPreferences
from bot.ui.callbacks import SettingCB
from bot.ui.keyboards import Keyboards


@pytest.fixture
async def db():
    database = Database(":memory:")
    await database.connect()
    yield database
    await database.close()


def test_defaults_keep_notifications_on():
    prefs = UserPreferences()
    assert prefs.notify_music == "summary"
    assert prefs.notify_library is True


@pytest.mark.asyncio
async def test_recipients_are_filtered_per_category(db, monkeypatch):
    from bot import main

    await db.create_user(User(tg_id=111))  # defaults
    await db.create_user(User(tg_id=222, preferences=UserPreferences(notify_music="off")))
    await db.create_user(User(tg_id=333, preferences=UserPreferences(notify_library=False)))
    await db.add_allowed_user(444, added_by=111)  # never opened the bot: defaults

    fake_settings = MagicMock(allowed_tg_ids=[111, 222], admin_tg_ids=[333])
    monkeypatch.setattr(main, "get_settings", lambda: fake_settings)

    assert await main.notification_recipients(db, "music") == [111, 333, 444]
    assert await main.notification_recipients(db, "library") == [111, 222, 444]


def _cb() -> MagicMock:
    cb = MagicMock()
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    return cb


def _svc() -> AsyncMock:
    svc = AsyncMock()
    for name in ("get_radarr_profiles", "get_radarr_root_folders",
                 "get_sonarr_profiles", "get_sonarr_root_folders"):
        setattr(svc, name, AsyncMock(return_value=[]))
    return svc


@pytest.mark.asyncio
async def test_music_toggle_persists(db):
    user = await db.create_user(User(tg_id=111))
    cb = _cb()

    with patch.object(settings_handlers, "_get_add_service", AsyncMock(return_value=_svc())):
        await settings_handlers.handle_set_notify(
            cb, SettingCB(key="notify_music", value="off"), user, db
        )

    stored = await db.get_user(111)
    assert stored is not None
    assert stored.preferences.notify_music == "off"
    assert cb.answer.call_count == 1


@pytest.mark.asyncio
async def test_library_toggle_persists(db):
    user = await db.create_user(User(tg_id=111))
    cb = _cb()

    with patch.object(settings_handlers, "_get_add_service", AsyncMock(return_value=_svc())):
        await settings_handlers.handle_set_notify(
            cb, SettingCB(key="notify_library", value="0"), user, db
        )

    stored = await db.get_user(111)
    assert stored is not None
    assert stored.preferences.notify_library is False


def _buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


def test_settings_menu_shows_both_toggles_with_current_state():
    on = _buttons(Keyboards.settings_menu(prefs=UserPreferences()))
    texts = [b.text for b in on]
    assert "🎵 Уведомления о музыке: сводка" in texts
    assert "🎬 «В библиотеке»: вкл" in texts

    off = _buttons(Keyboards.settings_menu(prefs=UserPreferences(notify_music="off", notify_library=False)))
    texts = [b.text for b in off]
    assert "🎵 Уведомления о музыке: выкл" in texts
    assert "🎬 «В библиотеке»: выкл" in texts

    # each button flips to the other value
    music = next(b for b in on if b.text.startswith("🎵 Уведомления"))
    assert SettingCB.unpack(music.callback_data) == SettingCB(key="notify_music", value="off")
    library = next(b for b in off if b.text.startswith("🎬 «В библиотеке»"))
    assert SettingCB.unpack(library.callback_data) == SettingCB(key="notify_library", value="1")


def test_toggle_callback_data_fits_telegram_limit():
    for markup in (
        Keyboards.settings_menu(prefs=UserPreferences()),
        Keyboards.settings_menu(prefs=UserPreferences(notify_music="off", notify_library=False)),
    ):
        for button in _buttons(markup):
            assert len((button.callback_data or "").encode()) <= 64


@pytest.mark.asyncio
async def test_watcher_tags_each_message_with_its_category():
    from bot.services.library_watcher import LibraryWatcher

    calls: list[tuple[str, str]] = []

    async def notify(text: str, category: str) -> None:
        calls.append((category, text))

    radarr, sonarr = AsyncMock(), AsyncMock()
    radarr.get_history.return_value = []
    sonarr.get_history.return_value = []
    watcher = LibraryWatcher(radarr, sonarr, notify=notify)
    await watcher.poll()
    radarr.get_history.return_value = [
        {"id": 5, "sourceTitle": "Film", "date": "2026-09-24T10:00:00Z", "downloadId": "abc"}
    ]
    await watcher.poll()
    await watcher._announce("🎵 x", "music")

    assert [c for c, _ in calls] == ["library", "music"]
