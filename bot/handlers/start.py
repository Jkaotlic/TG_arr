"""Start, help, and simple utility command handlers."""

from html import escape as html_escape

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from bot.ui.formatters import Formatters
from bot.ui.keyboards import Keyboards

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """Handle /start command."""
    user = message.from_user
    name = user.first_name if user else "there"
    safe_name = html_escape(name)

    welcome_text = (
        f"<b>Welcome, {safe_name}!</b>\n\n"
        "TG_arr helps you search and grab releases via Prowlarr/Radarr/Sonarr from Telegram.\n\n"
        "<b>Quick start</b>\n"
        "- Send any movie/series title to search\n"
        "- Or use <code>/movie</code>, <code>/series</code>, <code>/search</code>\n"
        "- Configure defaults with <code>/settings</code>\n\n"
        "Use <code>/help</code> for all commands."
    )

    await message.answer(welcome_text, parse_mode="HTML", reply_markup=Keyboards.main_menu())


@router.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    """Show main menu keyboard."""
    await message.answer("Menu:", reply_markup=Keyboards.main_menu())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """Handle /help command."""
    help_text = (
        "<b>TG_arr Bot Help</b>\n\n"
        "<b>Main</b>\n"
        "<code>/start</code> - start\n"
        "<code>/menu</code> - show menu buttons\n"
        "<code>/help</code> - this message\n\n"
        "<b>Search</b>\n"
        "<code>/search &lt;query&gt;</code> - auto-detect movie/series\n"
        "<code>/movie &lt;query&gt;</code> - movie search\n"
        "<code>/series &lt;query&gt;</code> - series search\n\n"
        "<b>Settings & Status</b>\n"
        "<code>/settings</code> - preferences\n"
        "<code>/status</code> - check Prowlarr/Radarr/Sonarr\n"
        "<code>/history</code> - recent actions\n"
        "<code>/cancel</code> - cancel current operation\n\n"
        "<b>Downloads (qBittorrent)</b>\n"
        "<code>/downloads</code> or <code>/dl</code> - list torrents\n"
        "<code>/qstatus</code> - qBittorrent overview\n"
        "<code>/pause</code> - pause all (or by hash)\n"
        "<code>/resume</code> - resume all (or by hash)\n\n"
        "Tip: you can also just send any title as a search query."
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
        await message.answer("Operation cancelled. Send a new search query to start over.")
    finally:
        await db.close()
