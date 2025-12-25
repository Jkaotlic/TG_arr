"""Start and help command handlers."""

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from bot.ui.formatters import Formatters

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """Handle /start command."""
    user = message.from_user
    name = user.first_name if user else "there"

    welcome_text = f"""👋 **Welcome, {name}!**

I'm TG_arr bot - your gateway to managing Prowlarr, Radarr, and Sonarr right from Telegram.

**Quick Start:**
• Send me a movie/series name to search
• Use `/movie` or `/series` for specific searches
• Configure defaults with `/settings`

Use `/help` for full command list."""

    await message.answer(welcome_text, parse_mode="Markdown")


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """Handle /help command."""
    await message.answer(Formatters.format_help(), parse_mode="Markdown")


@router.message(Command("cancel"))
async def cmd_cancel(message: Message) -> None:
    """Handle /cancel command."""
    # Import here to avoid circular imports
    from bot.db import Database
    from bot.config import get_settings

    settings = get_settings()
    db = Database(settings.database_path)
    await db.connect()

    try:
        user_id = message.from_user.id if message.from_user else 0
        await db.delete_session(user_id)
        await message.answer("Operation cancelled. Send a new search query to start over.")
    finally:
        await db.close()
