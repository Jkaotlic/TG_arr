"""Start, help, and simple utility command handlers."""

import asyncio
from html import escape as html_escape
from typing import Optional

import structlog
from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from bot.config import get_settings
from bot.db import Database
from bot.ui.formatters import Formatters
from bot.ui.keyboards import CallbackData, Keyboards

logger = structlog.get_logger()
router = Router()

# Russian menu button texts
MENU_SEARCH = "🔍 Поиск"
MENU_MOVIE = "🎬 Фильм"
MENU_SERIES = "📺 Сериал"
MENU_DOWNLOADS = "📥 Загрузки"
MENU_QSTATUS = "📊 qBit"
MENU_STATUS = "🔌 Статус"
MENU_SETTINGS = "⚙️ Настройки"
MENU_HISTORY = "📋 История"
MENU_HELP = "❓ Помощь"


async def _get_download_stats() -> Optional[tuple[int, int]]:
    """Get download statistics from qBittorrent (active, total)."""
    settings = get_settings()
    if not settings.qbittorrent_enabled:
        return None

    try:
        from bot.clients.registry import get_qbittorrent

        qbt = get_qbittorrent()
        if qbt:
            # Add timeout to prevent blocking for too long
            torrents = await asyncio.wait_for(qbt.get_torrents(), timeout=3.0)
            active = sum(1 for t in torrents if t.download_speed > 0 or t.upload_speed > 0)
            return active, len(torrents)
    except asyncio.TimeoutError:
        logger.debug("qBittorrent stats request timed out")
    except Exception as e:
        logger.debug("Failed to get download stats", error=str(e))

    return None


async def _build_home_screen(user_id: int, user_name: str) -> tuple[str, Optional[InlineKeyboardMarkup]]:
    """Build home screen text and keyboard."""
    settings = get_settings()
    db = Database(settings.database_path)
    await db.connect()

    try:
        # Get user stats and recent searches in parallel with overall timeout
        stats_task = db.get_user_stats(user_id)
        searches_task = db.get_recent_searches(user_id, limit=4)
        download_task = _get_download_stats()

        try:
            stats, recent_searches, download_stats = await asyncio.wait_for(
                asyncio.gather(
                    stats_task, searches_task, download_task, return_exceptions=True
                ),
                timeout=5.0  # Overall timeout for all tasks
            )
        except asyncio.TimeoutError:
            logger.warning("Home screen data fetch timed out")
            stats = {"total_searches": 0, "total_grabs": 0, "movies_added": 0, "series_added": 0}
            recent_searches = []
            download_stats = None

        # Handle exceptions
        if isinstance(stats, Exception):
            stats = {"total_searches": 0, "total_grabs": 0, "movies_added": 0, "series_added": 0}
        if isinstance(recent_searches, Exception):
            recent_searches = []
        if isinstance(download_stats, Exception):
            download_stats = None

        # Build welcome text
        safe_name = html_escape(user_name)
        lines = [f"<b>Привет, {safe_name}! 👋</b>\n"]

        # Add download status if available
        if download_stats:
            active, total = download_stats
            if total > 0:
                lines.append(f"📥 <b>Загрузки:</b> {active} активных / {total} всего")

        # Add user stats
        if stats["total_searches"] > 0 or stats["total_grabs"] > 0:
            stats_parts = []
            if stats["total_searches"] > 0:
                stats_parts.append(f"🔍 {stats['total_searches']} поисков")
            if stats["total_grabs"] > 0:
                stats_parts.append(f"📥 {stats['total_grabs']} скачано")
            if stats["movies_added"] > 0:
                stats_parts.append(f"🎬 {stats['movies_added']} фильмов")
            if stats["series_added"] > 0:
                stats_parts.append(f"📺 {stats['series_added']} сериалов")
            lines.append("📊 <b>Ваша статистика:</b> " + " • ".join(stats_parts))

        lines.append("")
        lines.append("<b>🚀 Быстрый старт:</b>")
        lines.append("• Просто напишите название")
        lines.append("• Или используйте 🎬 <b>Фильм</b> / 📺 <b>Сериал</b>")

        text = "\n".join(lines)

        # Build quick actions keyboard
        quick_kb = Keyboards.quick_actions(
            recent_searches=recent_searches,
            show_trending=settings.tmdb_enabled,
        )

        return text, quick_kb

    finally:
        await db.close()


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """Handle /start command."""
    user = message.from_user
    user_id = user.id if user else 0
    name = user.first_name if user else ""

    text, quick_kb = await _build_home_screen(user_id, name)

    await message.answer(text, parse_mode="HTML", reply_markup=Keyboards.main_menu())

    # Send quick actions as separate inline message if available
    if quick_kb:
        await message.answer("⚡ <b>Быстрые действия:</b>", parse_mode="HTML", reply_markup=quick_kb)


@router.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    """Show main menu keyboard."""
    await message.answer("📋 Меню:", reply_markup=Keyboards.main_menu())


@router.message(F.text == MENU_HELP)
@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """Handle /help command."""
    help_text = (
        "<b>🤖 TG_arr — Справка</b>\n\n"
        "<b>🔍 Поиск:</b>\n"
        "<code>/search</code> — фильмы и сериалы\n"
        "<code>/movie</code> — только фильмы\n"
        "<code>/series</code> — только сериалы\n\n"
        "<b>📥 Загрузки:</b>\n"
        "<code>/downloads</code> — список торрентов\n"
        "<code>/qstatus</code> — статус qBittorrent\n"
        "<code>/pause</code> — пауза всех\n"
        "<code>/resume</code> — продолжить все\n\n"
        "<b>⚙️ Другое:</b>\n"
        "<code>/settings</code> — настройки\n"
        "<code>/status</code> — статус сервисов\n"
        "<code>/history</code> — история\n"
        "<code>/cancel</code> — отмена\n\n"
        "💡 Просто напишите название для поиска!"
    )
    await message.answer(help_text, parse_mode="HTML", reply_markup=Keyboards.main_menu())


@router.message(Command("cancel"))
async def cmd_cancel(message: Message) -> None:
    """Handle /cancel command."""
    # Import here to avoid circular imports
    from bot.config import get_settings
    from bot.db import Database

    settings = get_settings()
    db = Database(settings.database_path)
    await db.connect()

    try:
        user_id = message.from_user.id if message.from_user else 0
        await db.delete_session(user_id)
        await message.answer("❌ Отменено. Напишите название для нового поиска.")
    finally:
        await db.close()
