"""Start, help, and simple utility command handlers."""

from html import escape as html_escape

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from bot.db import Database
from bot.ui.commands import render_help
from bot.ui.keyboards import Keyboards

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """Handle /start command."""
    user = message.from_user
    name = user.first_name if user else ""
    safe_name = html_escape(name)

    welcome_text = (
        f"<b>Привет, {safe_name}! 👋</b>\n\n"
        "Я помогу найти и скачать фильмы, сериалы, аниме и музыку через Scryer, Lidarr и Soulseek.\n\n"
        "<b>🚀 Быстрый старт:</b>\n"
        "• Просто напишите название — найду фильм или сериал\n"
        "• Или нажмите 🔍 <b>Поиск</b> / 🎵 <b>Музыка</b>\n\n"
        "Используйте /help для списка команд."
    )

    await message.answer(welcome_text, parse_mode="HTML", reply_markup=Keyboards.main_menu())


@router.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    """Show main menu keyboard."""
    await message.answer("📋 Меню:", reply_markup=Keyboards.main_menu())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """Handle /help command."""
    await message.answer(render_help(), parse_mode="HTML", reply_markup=Keyboards.main_menu())


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, db: Database) -> None:
    """Handle /cancel command."""
    user_id = message.from_user.id if message.from_user else 0
    await db.delete_session(user_id)
    await message.answer("❌ Отменено. Напишите название для нового поиска.")
