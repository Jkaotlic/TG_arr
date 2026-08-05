"""Хендлеры раздела TorrServer: панель и её деградация."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.handlers import torrserver as ts_handlers
from bot.models import TorrServerStats


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
