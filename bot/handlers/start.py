"""Start, help, and simple utility command handlers."""

from html import escape as html_escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from bot.db import Database
from bot.handlers.common import accessible_message
from bot.ui.commands import render_help
from bot.ui.keyboards import CallbackData, Keyboards

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """Handle /start command."""
    user = message.from_user
    name = user.first_name if user else ""
    safe_name = html_escape(name)

    welcome_text = (
        f"<b>Привет, {safe_name}! 👋</b>\n\n"
        "Я помогу найти и скачать фильмы, сериалы, аниме и музыку через Radarr, Sonarr, Lidarr и Soulseek.\n\n"
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


@router.callback_query(F.data == CallbackData.CLOSE)
async def handle_close(callback: CallbackQuery) -> None:
    """«✖️ Закрыть»: remove this message and nothing else (audit UI-07).

    Unlike CANCEL (search flow only), it never touches the search session —
    closing /settings used to drop a search the user was still in.
    """
    if (message := accessible_message(callback)) is not None:
        try:
            await message.delete()
        except TelegramBadRequest:
            # Too old to delete (>48 h) — at least take the buttons away.
            try:
                await message.edit_reply_markup(reply_markup=None)
            except TelegramBadRequest:
                pass
    await callback.answer()
