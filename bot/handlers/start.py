"""Start, help, and simple utility command handlers."""

from html import escape as html_escape

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from bot.db import Database
from bot.ui.formatters import Formatters
from bot.ui.keyboards import Keyboards

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


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """Handle /start command."""
    user = message.from_user
    name = user.first_name if user else ""
    safe_name = html_escape(name)

    welcome_text = (
        f"<b>Привет, {safe_name}! 👋</b>\n\n"
        "Я помогу найти и скачать фильмы и сериалы через Prowlarr/Radarr/Sonarr.\n\n"
        "<b>🚀 Быстрый старт:</b>\n"
        "• Просто напишите название\n"
        "• Или используйте 🎬 <b>Фильм</b> / 📺 <b>Сериал</b>\n"
        "• Настройки: ⚙️ <b>Настройки</b>\n\n"
        "Нажмите ❓ <b>Помощь</b> для списка команд."
    )

    await message.answer(welcome_text, parse_mode="HTML", reply_markup=Keyboards.main_menu())


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
async def cmd_cancel(message: Message, db: Database) -> None:
    """Handle /cancel command."""
    user_id = message.from_user.id if message.from_user else 0
    await db.delete_session(user_id)
    await message.answer("❌ Отменено. Напишите название для нового поиска.")
