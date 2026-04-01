"""History command handler."""

import structlog
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.db import Database
from bot.models import User
from bot.ui.formatters import Formatters

logger = structlog.get_logger()
router = Router()

# Russian menu button text
MENU_HISTORY = "📋 История"


@router.message(F.text == MENU_HISTORY)
@router.message(Command("history"))
async def cmd_history(message: Message, db_user: User, db: Database, is_admin: bool = False) -> None:
    """Handle /history command - show recent actions."""
    try:
        user_id = db_user.tg_id

        # Admins see all actions, users see only their own
        if is_admin:
            actions = await db.get_all_actions(limit=20)
            header = "<b>📋 История всех пользователей</b>\n\n"
        else:
            actions = await db.get_user_actions(user_id, limit=20)
            header = "<b>📋 Ваши действия</b>\n\n"

        if not actions:
            await message.answer("📭 История пуста.")
            return

        text = header + Formatters.format_action_log(actions)
        await message.answer(text, parse_mode="HTML")

    except Exception as e:
        logger.error("Failed to load history", error=str(e))
        await message.answer(Formatters.format_error("Не удалось загрузить историю"))
