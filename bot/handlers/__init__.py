"""Telegram bot handlers."""

from aiogram import Router

from bot.handlers.calendar import router as calendar_router
from bot.handlers.downloads import router as downloads_router
from bot.handlers.emby import router as emby_router
from bot.handlers.history import router as history_router
from bot.handlers.music import router as music_router
from bot.handlers.search import router as search_router
from bot.handlers.settings import router as settings_router
from bot.handlers.start import router as start_router
from bot.handlers.status import router as status_router
from bot.handlers.trending import router as trending_router


def setup_routers() -> Router:
    """Setup and return the main router with all handlers."""
    main_router = Router()

    # search_router owns CONFIRM_GRAB and dispatches to music/movie/series by
    # session.selected_content type (BUG-27). music_router is only for /music,
    # artist selection, and TRENDING_MUSIC callbacks.
    main_router.include_router(start_router)
    main_router.include_router(search_router)
    main_router.include_router(music_router)
    main_router.include_router(settings_router)
    main_router.include_router(status_router)
    main_router.include_router(history_router)
    main_router.include_router(downloads_router)
    main_router.include_router(emby_router)
    main_router.include_router(trending_router)
    main_router.include_router(calendar_router)

    return main_router
