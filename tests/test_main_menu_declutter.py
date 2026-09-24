"""Task C4 (user request 2026-09-24: «куча кнопок и все в кучу»).

Reply keyboard: [🔍 Поиск][🎵 Музыка] / [📥 Загрузки][⋯ Ещё]; "⋯ Ещё" opens an
inline panel with the rest; 📊 qBit moved into the header of 📥 Загрузки.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import Chat, Message
from aiogram.types import User as TgUser

from bot.models import QBittorrentStatus, TorrentFilter, TorrentInfo, TorrentState, User


def _cb(user_id: int = 42) -> MagicMock:
    cb = MagicMock()
    cb.from_user = MagicMock(id=user_id)
    cb.message = MagicMock()
    cb.message.delete = AsyncMock()
    cb.message.edit_text = AsyncMock()
    cb.message.answer = AsyncMock()
    cb.answer = AsyncMock()
    return cb


# ---------------------------------------------------------------- reply keyboard
def test_main_menu_is_two_rows_of_two():
    from bot.ui.keyboards import Keyboards

    kb = Keyboards.main_menu()
    assert [[b.text for b in row] for row in kb.keyboard] == [
        ["🔍 Поиск", "🎵 Музыка"],
        ["📥 Загрузки", "⋯ Ещё"],
    ]
    assert kb.resize_keyboard is True
    assert kb.input_field_placeholder == "Введите название для поиска..."


def test_more_panel_layout():
    from bot.ui.callbacks import MoreCB
    from bot.ui.keyboards import Keyboards

    kb = Keyboards.more_menu()
    assert [[b.text for b in row] for row in kb.inline_keyboard] == [
        ["🔥 Топ", "📅 Календарь"],
        ["▶️ Смотреть", "📺 Emby"],
        ["📋 История", "🔌 Статус"],
        ["⚙️ Настройки"],
    ]
    for row in kb.inline_keyboard:
        for b in row:
            assert b.callback_data.startswith("more:")
            MoreCB.unpack(b.callback_data)


@pytest.mark.asyncio
async def test_more_button_sends_the_panel():
    from bot.handlers import start
    from bot.ui.menu import MENU_MORE

    assert MENU_MORE == "⋯ Ещё"
    msg = MagicMock(answer=AsyncMock())
    await start.handle_menu_more(msg)
    assert msg.answer.await_args.args[0] == "Ещё:"
    assert msg.answer.await_args.kwargs["reply_markup"].inline_keyboard[0][0].text == "🔥 Топ"


_TARGETS = {
    "trending": ("bot.handlers.trending", "handle_trending_menu"),
    "calendar": ("bot.handlers.calendar", "handle_calendar_menu"),
    "watch": ("bot.handlers.torrserver", "cmd_torrserver"),
    "emby": ("bot.handlers.emby", "cmd_emby"),
    "history": ("bot.handlers.history", "cmd_history"),
    "status": ("bot.handlers.status", "cmd_status"),
    "settings": ("bot.handlers.settings", "cmd_settings"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("key", sorted(_TARGETS))
async def test_every_more_item_dispatches_to_the_old_handler(key):
    import importlib

    from bot.handlers import start
    from bot.ui.callbacks import MoreCB

    module_name, func_name = _TARGETS[key]
    module = importlib.import_module(module_name)
    cb = _cb()
    db_user, db = User(tg_id=42), MagicMock()
    with patch.object(module, func_name, autospec=True) as target:
        await start.handle_more_item(cb, MoreCB(key=key), db_user=db_user, db=db, is_admin=True)

    target.assert_awaited_once()
    kwargs = target.await_args.kwargs
    if "db_user" in kwargs:
        assert kwargs["db_user"] is db_user
    if key in ("emby", "history"):
        assert kwargs["is_admin"] is True
    cb.message.delete.assert_awaited_once()  # no leftover panel
    cb.answer.assert_awaited()


@pytest.mark.asyncio
async def test_more_item_acts_as_the_tapping_user_not_the_bot():
    from bot.handlers import start

    panel = Message(
        message_id=5, date=0, chat=Chat(id=42, type="private"),
        from_user=TgUser(id=999, is_bot=True, first_name="bot"), text="Ещё:",
    )
    user = TgUser(id=42, is_bot=False, first_name="u")
    as_user = start._as_user_message(panel, user)
    assert as_user.from_user.id == 42
    assert as_user.chat.id == 42


@pytest.mark.asyncio
async def test_unknown_more_key_is_answered():
    from bot.handlers import start
    from bot.ui.callbacks import MoreCB

    cb = _cb()
    await start.handle_more_item(cb, MoreCB(key="nope"), db_user=User(tg_id=42), db=MagicMock(), is_admin=False)
    assert cb.answer.await_args.kwargs.get("show_alert")


# ---------------------------------------------------------------- legacy reply texts
@pytest.mark.parametrize("text", ["🔥 Топ", "📅 Календарь", "📊 qBit", "📺 Emby", "🔌 Статус",
                                  "⚙️ Настройки", "📋 История", "▶️ Смотреть"])
def test_legacy_button_texts_are_never_a_search_query(text):
    from bot.ui.menu import MENU_BUTTONS

    assert text in MENU_BUTTONS


def test_more_is_never_a_search_query():
    from bot.ui.menu import MENU_BUTTONS, MENU_MORE

    assert MENU_MORE in MENU_BUTTONS


@pytest.mark.asyncio
async def test_legacy_qbit_text_still_reaches_qstatus():
    import bot.handlers as hp
    from bot.handlers import downloads

    message = Message(
        message_id=2, date=0, chat=Chat(id=42, type="private"),
        from_user=TgUser(id=42, is_bot=False, first_name="u"), text="📊 qBit",
    )
    bot = MagicMock()
    bot.me = AsyncMock(return_value=MagicMock(username="bot"))
    roots = [hp.start_router, hp.torrserver_router, hp.music_prompt_router, hp.search_router,
             hp.music_router, hp.settings_router, hp.users_router, hp.status_router,
             hp.history_router, hp.downloads_router]
    found = None
    stack = list(roots)
    while stack and found is None:
        router = stack.pop(0)
        for h in router.message.handlers:
            ok, _ = await h.check(message, bot=bot)
            if ok:
                found = h
                break
        stack = list(router.sub_routers) + stack
    assert found is not None and found.callback is downloads.cmd_qstatus


# ---------------------------------------------------------------- downloads header
def _torrent() -> TorrentInfo:
    return TorrentInfo(hash="a" * 40, name="T", size=1, progress=0.5, state=TorrentState.DOWNLOADING)


def test_torrent_list_header_carries_qbit_summary():
    from bot.ui.formatters import Formatters

    status = QBittorrentStatus(
        download_speed=2 * 1024 * 1024, upload_speed=512 * 1024, active_downloads=3,
        active_uploads=1, free_space=100 * 1024**3,
    )
    text = Formatters.format_torrent_list([_torrent()], 0, 1, TorrentFilter.ALL, 1, status=status)
    assert "⬇️ 2.0 MB/s" in text and "⬆️ 512.0 KB/s" in text
    assert "активных: 3" in text
    assert "свободно: 100.0 GB" in text


def test_header_without_free_space_omits_it():
    from bot.ui.formatters import Formatters

    status = QBittorrentStatus(download_speed=0, upload_speed=0, active_downloads=0)
    text = Formatters.format_torrent_list([_torrent()], 0, 1, TorrentFilter.ALL, 1, status=status)
    assert "свободно" not in text
    assert "⬇️ 0 B/s" in text


@pytest.mark.asyncio
async def test_downloads_command_shows_summary_header():
    from bot.handlers import downloads

    qbt = MagicMock()
    qbt.get_torrents = AsyncMock(return_value=[_torrent()])
    qbt.get_status = AsyncMock(return_value=QBittorrentStatus(download_speed=1024, active_downloads=1))
    status_msg = MagicMock(edit_text=AsyncMock())
    msg = MagicMock(answer=AsyncMock(return_value=status_msg), from_user=MagicMock(id=42))
    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)), \
         patch.object(downloads, "_soulseek_section", AsyncMock(return_value="")):
        await downloads.cmd_downloads(msg, User(tg_id=42))
    text = status_msg.edit_text.await_args.args[0]
    assert "⬇️ 1.0 KB/s" in text and "активных: 1" in text


@pytest.mark.asyncio
async def test_downloads_list_survives_a_status_failure():
    from bot.handlers import downloads

    qbt = MagicMock()
    qbt.get_torrents = AsyncMock(return_value=[_torrent()])
    qbt.get_status = AsyncMock(side_effect=RuntimeError("maindata timeout"))
    status_msg = MagicMock(edit_text=AsyncMock())
    msg = MagicMock(answer=AsyncMock(return_value=status_msg), from_user=MagicMock(id=42))
    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)), \
         patch.object(downloads, "_soulseek_section", AsyncMock(return_value="")):
        await downloads.cmd_downloads(msg, User(tg_id=42))
    assert "Загрузки" in status_msg.edit_text.await_args.args[0]


# ---------------------------------------------------------------- new keyboard reaches users
@pytest.mark.asyncio
async def test_startup_card_carries_the_new_keyboard(monkeypatch):
    import bot.main as main_mod

    monkeypatch.setattr(main_mod, "get_settings", lambda: MagicMock(notify_admins_on_start=True))
    bot = MagicMock(send_message=AsyncMock())
    await main_mod.notify_admins_on_start(bot, admin_ids=[10], allowed_ids=[10], warmup={})
    kb = bot.send_message.await_args.kwargs.get("reply_markup")
    assert kb is not None and [b.text for b in kb.keyboard[1]] == ["📥 Загрузки", "⋯ Ещё"]
