"""Audit 2026-09-23, Task C2: stale/legacy callbacks and the music entry.

UI-19 «часики» на сообщениях >48 ч; `t_refresh` без обработчика.
UI-13 «🎵 Музыка» → следующий текст уходил в автоопределение, не в поиск артиста.
UI-14 музыка оставляла висящие «Ищу…» сообщения и живые клавиатуры.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import CallbackQuery, Chat, ForceReply, InaccessibleMessage, Message
from aiogram.types import User as TgUser

from bot.models import ContentType, SearchSession, User


def _all_roots():
    import bot.handlers as hp

    # Same order as setup_routers() (which can attach the routers only once).
    return [
        hp.start_router, hp.torrserver_router, hp.music_prompt_router, hp.search_router,
        hp.music_router, hp.settings_router, hp.users_router, hp.status_router,
        hp.history_router, hp.downloads_router, hp.emby_router, hp.trending_router,
        hp.calendar_router, hp.titles_router, hp.fallback_router,
    ]


async def _route_callback(data: str):
    query = CallbackQuery(
        id="1", from_user=TgUser(id=42, is_bot=False, first_name="u"),
        chat_instance="c", data=data,
    )
    stack = _all_roots()
    while stack:
        router = stack.pop(0)
        for h in router.callback_query.handlers:
            ok, kw = await h.check(query)
            if ok:
                return h, kw
        stack = list(router.sub_routers) + stack
    return None, {}


def _cb() -> MagicMock:
    cb = MagicMock()
    cb.from_user = MagicMock(id=42)
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    return cb


# ---------------------------------------------------------------- InaccessibleMessage
@pytest.mark.asyncio
async def test_callback_on_inaccessible_message_is_answered_and_not_handled():
    from bot.middleware.auth import StaleMessageMiddleware

    msg = InaccessibleMessage(chat=Chat(id=1, type="private"), message_id=1, date=0)
    event = MagicMock(spec=CallbackQuery)
    event.message = msg
    event.answer = AsyncMock()
    handler = AsyncMock()

    await StaleMessageMiddleware()(handler, event, {})

    handler.assert_not_called()
    event.answer.assert_awaited_once()
    assert "Сообщение устарело" in event.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_callback_on_live_message_passes_through():
    from bot.middleware.auth import StaleMessageMiddleware

    event = MagicMock(spec=CallbackQuery)
    event.message = MagicMock(spec=Message)
    event.answer = AsyncMock()
    handler = AsyncMock(return_value="ok")

    assert await StaleMessageMiddleware()(handler, event, {}) == "ok"
    event.answer.assert_not_called()


def test_dispatcher_registers_stale_message_middleware():
    source = (Path(__file__).resolve().parent.parent / "bot" / "main.py").read_text(encoding="utf-8")
    assert "dp.callback_query.middleware(StaleMessageMiddleware())" in source


# ---------------------------------------------------------------- legacy / unknown data
@pytest.mark.asyncio
async def test_unknown_callback_data_is_answered_by_the_fallback():
    handler, _ = await _route_callback("definitely_not_a_button:1")
    assert handler is not None, "unknown data must still reach a handler (no spinner)"
    cb = _cb()
    await handler.callback(cb)
    cb.answer.assert_awaited_once()
    assert cb.answer.await_args.kwargs.get("show_alert")


@pytest.mark.asyncio
async def test_t_refresh_is_handled():
    from bot.ui.keyboards import CallbackData

    handler, _ = await _route_callback(CallbackData.TORRENT_REFRESH)
    assert handler is not None
    import bot.handlers as hp

    assert handler not in hp.fallback_router.callback_query.handlers, "t_refresh redraws the list"


def test_fallback_router_is_last_in_setup_routers():
    source = (Path(__file__).resolve().parent.parent / "bot" / "handlers" / "__init__.py").read_text(
        encoding="utf-8",
    )
    includes = [line.strip() for line in source.splitlines() if "include_router(" in line]
    assert includes[-1] == "main_router.include_router(fallback_router)"
    assert includes.index("main_router.include_router(music_prompt_router)") < includes.index(
        "main_router.include_router(search_router)",
    )


# ---------------------------------------------------------------- music entry (UI-13)
@pytest.mark.asyncio
async def test_music_menu_asks_with_force_reply(monkeypatch):
    from bot.handlers import music
    from bot.ui.menu import MUSIC_PROMPT

    monkeypatch.setenv("LIDARR_URL", "http://lidarr.example.com")
    monkeypatch.setenv("LIDARR_API_KEY", "k")
    msg = MagicMock()
    msg.answer = AsyncMock()
    with patch.object(music, "get_settings", return_value=MagicMock(music_enabled=True)):
        await music.handle_menu_music(msg)
    assert msg.answer.await_args.args[0] == MUSIC_PROMPT
    assert isinstance(msg.answer.await_args.kwargs.get("reply_markup"), ForceReply)


@pytest.mark.asyncio
async def test_reply_to_music_prompt_searches_artists():
    from bot.handlers import music
    from bot.ui.menu import MUSIC_PROMPT

    msg = MagicMock()
    msg.text = "Metallica"
    msg.reply_to_message = MagicMock(text=MUSIC_PROMPT)
    with patch.object(music, "process_music_search", AsyncMock()) as search:
        await music.handle_music_prompt_reply(msg, User(tg_id=42), MagicMock())
    assert search.await_args.args[1] == "Metallica"


@pytest.mark.asyncio
async def test_music_prompt_reply_is_routed_before_text_search():
    from bot.ui.menu import MUSIC_PROMPT

    message = Message(
        message_id=2, date=0, chat=Chat(id=42, type="private"),
        from_user=TgUser(id=42, is_bot=False, first_name="u"), text="Metallica",
        reply_to_message=Message(
            message_id=1, date=0, chat=Chat(id=42, type="private"), text=MUSIC_PROMPT,
        ),
    )
    from bot.handlers import music

    bot = MagicMock()
    bot.me = AsyncMock(return_value=MagicMock(username="bot"))
    stack = _all_roots()
    found = None
    while stack and found is None:
        router = stack.pop(0)
        for h in router.message.handlers:
            ok, _ = await h.check(message, bot=bot)
            if ok:
                found = h
                break
        stack = list(router.sub_routers) + stack
    assert found is not None and found.callback is music.handle_music_prompt_reply


# ---------------------------------------------------------------- UI-14
@pytest.mark.asyncio
async def test_music_search_edits_given_status_message_instead_of_new_one():
    from bot.handlers import music

    msg = MagicMock()
    msg.answer = AsyncMock()
    status = MagicMock()
    status.edit_text = AsyncMock()
    search_service = MagicMock()
    search_service.lookup_artist = AsyncMock(return_value=[])
    db = MagicMock(delete_session=AsyncMock())
    with patch.object(music, "_get_music_services", AsyncMock(return_value=(search_service, MagicMock()))):
        await music.process_music_search(msg, "Metallica", User(tg_id=42), db, status_msg=status)
    msg.answer.assert_not_called()
    status.edit_text.assert_awaited()


@pytest.mark.asyncio
async def test_type_choice_music_reuses_the_question_message():
    from bot.handlers.search import results as results_mod
    from bot.ui.callbacks import ContentTypeCB

    session = SearchSession(user_id=42, query="Metallica", content_type=ContentType.UNKNOWN, list_token="abc123")
    db = MagicMock(get_session=AsyncMock(return_value=session), delete_session=AsyncMock())
    cb = _cb()
    with patch("bot.handlers.music.process_music_search", AsyncMock()) as search:
        await results_mod.handle_type_selection(cb, ContentTypeCB(kind="music", tok="abc123"), User(tg_id=42), db)
    assert search.await_args.kwargs.get("status_msg") is cb.message


@pytest.mark.asyncio
async def test_trending_artist_click_edits_the_same_message():
    from bot.handlers import music
    from bot.ui.callbacks import TrendingArtistCB

    music._trending_artists_cache[42] = [{"name": "Artist"}]
    token = music._trending_artist_tokens.issue(42)
    cb = _cb()
    try:
        with patch.object(music, "process_music_search", AsyncMock()) as search:
            await music.handle_trending_artist_click(
                cb, TrendingArtistCB(tok=token, idx=0), User(tg_id=42), MagicMock(),
            )
    finally:
        music._trending_artists_cache.pop(42, None)
    assert search.await_args.kwargs.get("status_msg") is cb.message
