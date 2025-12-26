"""History command handler."""

import structlog
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.config import get_settings
from bot.db import Database
from bot.models import User
from bot.ui.formatters import Formatters

logger = structlog.get_logger()
router = Router()

# Russian menu button text
MENU_HISTORY = "📋 История"


async def get_db() -> Database:
    """Get database instance."""
    settings = get_settings()
    db = Database(settings.database_path)
    await db.connect()
    return db


@router.message(F.text == MENU_HISTORY)
@router.message(Command("history"))
async def cmd_history(message: Message, db_user: User, is_admin: bool = False) -> None:
    """Handle /history command - show recent actions."""
    db = await get_db()

    try:
        user_id = db_user.tg_id

        # Admins see all actions, users see only their own
        if is_admin:
            actions = await db.get_all_actions(limit=20)
            header = "**📋 История всех пользователей**\n\n"
        else:
            actions = await db.get_user_actions(user_id, limit=20)
            header = "**📋 Ваши действия**\n\n"

        if not actions:
            await message.answer("📭 История пуста.")
            return

        text = header + Formatters.format_action_log(actions)
        await message.answer(text, parse_mode="Markdown")

    except Exception as e:
        logger.error("Failed to load history", error=str(e))
        await message.answer(Formatters.format_error(f"Не удалось загрузить историю: {str(e)}"))

    finally:
        await db.close()
