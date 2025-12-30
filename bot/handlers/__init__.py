"""Telegram bot handlers."""

from aiogram import Router

from bot.handlers.downloads import router as downloads_router
from bot.handlers.emby import router as emby_router
from bot.handlers.history import router as history_router
from bot.handlers.search import router as search_router
from bot.handlers.settings import router as settings_router
from bot.handlers.start import router as start_router
from bot.handlers.status import router as status_router
from bot.handlers.trending import router as trending_router


def setup_routers() -> Router:
    """Setup and return the main router with all handlers."""
    main_router = Router()

    # Include all sub-routers
    main_router.include_router(start_router)
    main_router.include_router(search_router)
    main_router.include_router(settings_router)
    main_router.include_router(status_router)
    main_router.include_router(history_router)
    main_router.include_router(downloads_router)
    main_router.include_router(emby_router)
    main_router.include_router(trending_router)

    return main_router
