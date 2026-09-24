"""Start, help, and simple utility command handlers."""

import importlib
import inspect
from html import escape as html_escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message
from aiogram.types import User as TgUser
import structlog

from bot.db import Database
from bot.handlers.common import accessible_message
from bot.models import User
from bot.ui.callbacks import MoreCB
from bot.ui.commands import render_help
from bot.ui.keyboards import CallbackData, Keyboards
from bot.ui.menu import MENU_MORE

logger = structlog.get_logger()
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


# ---------------------------------------------------------------------------
# "⋯ Ещё" panel (main-menu declutter, user request 2026-09-24)
# ---------------------------------------------------------------------------

#: MoreCB key → (module, handler) of the reply button it replaces. Resolved at
#: call time, so the panel runs exactly the handler the old button ran.
_MORE_TARGETS: dict[str, tuple[str, str]] = {
    "trending": ("bot.handlers.trending", "handle_trending_menu"),
    "calendar": ("bot.handlers.calendar", "handle_calendar_menu"),
    "watch": ("bot.handlers.torrserver", "cmd_torrserver"),
    "emby": ("bot.handlers.emby", "cmd_emby"),
    "history": ("bot.handlers.history", "cmd_history"),
    "status": ("bot.handlers.status", "cmd_status"),
    "settings": ("bot.handlers.settings", "cmd_settings"),
}


@router.message(F.text == MENU_MORE)
async def handle_menu_more(message: Message) -> None:
    """"⋯ Ещё": the sections that left the reply keyboard."""
    await message.answer("Ещё:", reply_markup=Keyboards.more_menu())


def _as_user_message(message: Message, user: TgUser) -> Message:
    """The panel message as if the tapping user had sent it.

    The panel is the bot's own message; handlers written for a reply-button
    press read ``message.from_user`` (calendar keeps a per-user period), which
    here would be the bot.
    """
    try:
        return message.model_copy(update={"from_user": user})
    except Exception:  # a test double without pydantic's model_copy
        return message


@router.callback_query(MoreCB.filter())
async def handle_more_item(
    callback: CallbackQuery,
    callback_data: MoreCB,
    db_user: User,
    db: Database,
    is_admin: bool = False,
) -> None:
    """Run the handler of the chosen section, then drop the panel."""
    target = _MORE_TARGETS.get(callback_data.key)
    message = accessible_message(callback)
    if target is None or message is None:
        await callback.answer("Раздел недоступен — откройте «⋯ Ещё» заново", show_alert=True)
        return

    await callback.answer()
    # No leftover panel: the section arrives as its own message below.
    try:
        await message.delete()
    except TelegramBadRequest:
        try:
            await message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass

    module_name, func_name = target
    handler = getattr(importlib.import_module(module_name), func_name)
    available = {"db_user": db_user, "db": db, "is_admin": is_admin}
    params = inspect.signature(handler).parameters
    kwargs = {name: value for name, value in available.items() if name in params}
    logger.info("more_menu_item", key=callback_data.key)
    await handler(_as_user_message(message, callback.from_user), **kwargs)
