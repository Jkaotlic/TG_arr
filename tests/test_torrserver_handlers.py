"""Хендлеры раздела TorrServer: панель и её деградация."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import ForceReply

from bot.handlers import torrserver as ts_handlers
from bot.models import SyncHookResult, TorrServerFile, TorrServerRelease, TorrServerStats, TorrServerTorrent
from bot.services.torrserver_service import AddResult
from bot.ui.menu import TORRSERVER_PROMPT


@pytest.mark.asyncio
async def test_panel_says_how_to_configure_when_disabled():
    with patch.object(ts_handlers, "get_torrserver", new_callable=AsyncMock, return_value=None):
        text, keyboard = await ts_handlers.render_panel()

    assert "TORRSERVER_URL" in text
    assert keyboard is None


@pytest.mark.asyncio
async def test_panel_renders_stats():
    client = MagicMock()
    client.get_stats = AsyncMock(return_value=TorrServerStats(
        version="MatriX.142.2", torrent_count=6, total_size=1024, cache_size=1024,
        use_disk=False, source_count=6,
    ))
    with patch.object(ts_handlers, "get_torrserver", new_callable=AsyncMock, return_value=client):
        text, keyboard = await ts_handlers.render_panel()

    assert "MatriX.142.2" in text
    assert keyboard is not None


@pytest.mark.asyncio
async def test_panel_survives_a_dead_server():
    client = MagicMock()
    client.get_stats = AsyncMock(side_effect=ts_handlers.TorrServerError("недоступен"))
    with patch.object(ts_handlers, "get_torrserver", new_callable=AsyncMock, return_value=client):
        text, keyboard = await ts_handlers.render_panel()

    assert "недоступен" in text
    assert keyboard is not None  # кнопка «Обновить» должна остаться


@pytest.mark.asyncio
async def test_panel_survives_an_unexpected_error():
    """A non-TorrServerError failure (network blip, bug, whatever) out of
    get_stats() must still degrade to a card with the retry keyboard, not an
    unhandled exception reaching the aiogram dispatcher."""
    client = MagicMock()
    client.get_stats = AsyncMock(side_effect=RuntimeError("boom"))
    with patch.object(ts_handlers, "get_torrserver", new_callable=AsyncMock, return_value=client):
        text, keyboard = await ts_handlers.render_panel()

    assert "❌" in text
    assert keyboard is not None  # кнопка «Обновить» должна остаться


def test_router_is_registered_before_search_router():
    """handle_text_search ловит любой текст, а aiogram не каскадирует
    обработчики после совпадения — наш роутер обязан идти раньше.

    Существующие роутеры (кроме torrserver) создаются без ``name=`` (см.
    bot/handlers/search/services.py, emby.py, ...), поэтому сравнение по
    ``.name`` неприменимо в общем случае — сверяем позицию по идентичности
    объекта роутера.
    """
    from bot.handlers import setup_routers
    from bot.handlers import search as search_module
    from bot.handlers import torrserver as ts_module

    subs = setup_routers().sub_routers
    assert subs.index(ts_module.router) < subs.index(search_module.router)


HIT = TorrServerRelease(title="Dune 2021 BDRemux", size=1024, seeders=10,
                        link="http://p:9696/2/download?link=a", tracker="RuTracker.org")


def _message(text="Dune"):
    message = MagicMock()
    message.text = text
    message.answer = AsyncMock()
    message.from_user = MagicMock(id=7)
    return message


@pytest.mark.asyncio
async def test_search_prompt_uses_force_reply_with_the_exact_marker():
    callback = MagicMock()
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.answer = AsyncMock()

    with patch.object(ts_handlers, "accessible_message", return_value=callback.message):
        await ts_handlers.handle_search_prompt(callback)

    text = callback.message.answer.await_args.args[0]
    markup = callback.message.answer.await_args.kwargs["reply_markup"]
    assert text == TORRSERVER_PROMPT
    assert isinstance(markup, ForceReply)


@pytest.mark.asyncio
async def test_reply_runs_a_search_and_caches_hits():
    client = MagicMock()
    client.search = AsyncMock(return_value=[HIT])
    status = MagicMock()
    status.edit_text = AsyncMock()
    message = _message("Dune 2021")
    message.answer = AsyncMock(return_value=status)

    with patch.object(ts_handlers, "get_torrserver", new_callable=AsyncMock, return_value=client):
        await ts_handlers.handle_search_reply(message)

    client.search.assert_awaited_once()
    assert ts_handlers._results[7][0].title == "Dune 2021 BDRemux"
    assert "Найдено" in status.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_reply_rejects_a_too_short_query():
    message = _message("a")
    with patch.object(ts_handlers, "get_torrserver", new_callable=AsyncMock) as client_getter:
        await ts_handlers.handle_search_reply(message)

    client_getter.assert_not_awaited()
    assert "коротк" in message.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_empty_search_result_is_reported():
    client = MagicMock()
    client.search = AsyncMock(return_value=[])
    status = MagicMock()
    status.edit_text = AsyncMock()
    message = _message("асдфасдф")
    message.answer = AsyncMock(return_value=status)

    with patch.object(ts_handlers, "get_torrserver", new_callable=AsyncMock, return_value=client):
        await ts_handlers.handle_search_reply(message)

    assert "не найдено" in status.edit_text.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_add_publishes_and_answers_with_the_stream_link():
    ts_handlers._results[7] = [HIT]
    torrent = TorrServerTorrent(hash="abc", title="Dune 2021", stat=3,
                                stat_string="Torrent working",
                                files=[TorrServerFile(id=1, path="Dune/Dune.mkv", length=10)])
    service = MagicMock()
    service.add_and_publish = AsyncMock(return_value=AddResult(
        torrent=torrent, metadata_ready=True,
        sync=SyncHookResult(status="ok", duration_s=1.0),
        stream_url="http://ts:8090/stream/Dune.mkv?link=abc&index=1&play",
    ))

    callback = MagicMock()
    callback.answer = AsyncMock()
    callback.from_user = MagicMock(id=7)
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()

    from bot.ui.callbacks import TsAddCB

    with patch.object(ts_handlers, "get_torrserver_service", new_callable=AsyncMock,
                      return_value=service), \
         patch.object(ts_handlers, "accessible_message", return_value=callback.message):
        await ts_handlers.handle_add(callback, TsAddCB(idx=0))

    service.add_and_publish.assert_awaited_once_with(HIT.link, HIT.title, "")
    assert "Emby" in callback.message.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_add_after_cache_expiry_asks_to_search_again():
    ts_handlers._results.pop(7, None)
    callback = MagicMock()
    callback.answer = AsyncMock()
    callback.from_user = MagicMock(id=7)
    callback.message = MagicMock()

    from bot.ui.callbacks import TsAddCB

    with patch.object(ts_handlers, "accessible_message", return_value=callback.message):
        await ts_handlers.handle_add(callback, TsAddCB(idx=0))

    assert callback.answer.await_args.kwargs.get("show_alert") is True
