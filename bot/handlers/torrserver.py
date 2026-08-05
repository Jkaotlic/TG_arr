"""TorrServer section — the "watch it now" contour.

Kept apart from the Scryer search flow on purpose: Scryer answers "I want to
own this", TorrServer answers "I want to watch this tonight". They share no
state.
"""

import structlog
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from bot.clients.registry import get_torrserver, get_torrserver_service  # noqa: F401 -- get_torrserver_service is wired in by Task 10/11 (search/add, list/delete); unused until then
from bot.clients.torrserver import TorrServerError
from bot.handlers.common import accessible_message, safe_edit
from bot.models import TorrServerRelease
from bot.ui.formatters import Formatters
from bot.ui.keyboards import CallbackData, Keyboards
from bot.ui.menu import MENU_TORRSERVER

logger = structlog.get_logger()
router = Router(name="torrserver")

#: Per-user search hits. They are far too large for callback_data, so buttons
#: carry only an index into this list — the same trick the Soulseek flow uses.
_results: dict[int, list[TorrServerRelease]] = {}

#: Cap on remembered result sets, evicting the oldest — never clear() the whole
#: dict, that would wipe every other user's in-flight selection.
_MAX_CACHED_USERS = 50

_NOT_CONFIGURED = (
    "❌ TorrServer не настроен. Добавьте <code>TORRSERVER_URL</code>, "
    "<code>TORRSERVER_USERNAME</code> и <code>TORRSERVER_PASSWORD</code> в конфигурацию."
)


async def render_panel() -> tuple[str, InlineKeyboardMarkup | None]:
    """Build the panel card without touching Telegram, so both the command and
    the refresh button can decide themselves how to deliver it."""
    client = await get_torrserver()
    if not client:
        return _NOT_CONFIGURED, None

    try:
        stats = await client.get_stats()
    except TorrServerError as e:
        return Formatters.format_error(str(e)), Keyboards.torrserver_panel()
    except Exception as e:
        logger.error("torrserver_panel_failed", error=str(e), exc_info=True)
        return Formatters.format_error("Не удалось получить статус TorrServer"), Keyboards.torrserver_panel()

    return Formatters.format_torrserver_status(stats), Keyboards.torrserver_panel()


@router.message(F.text == MENU_TORRSERVER)
@router.message(Command("ts"))
async def cmd_torrserver(message: Message) -> None:
    """Open the TorrServer panel."""
    text, keyboard = await render_panel()
    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data == CallbackData.TS_REFRESH)
@router.callback_query(F.data == CallbackData.TS_BACK)
async def handle_panel_refresh(callback: CallbackQuery) -> None:
    """Re-render the panel in place (exactly one callback.answer())."""
    text, keyboard = await render_panel()
    if (message := accessible_message(callback)) is not None:
        await safe_edit(message, text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == CallbackData.TS_CLOSE)
async def handle_close(callback: CallbackQuery) -> None:
    """Close the panel message."""
    if (message := accessible_message(callback)) is not None:
        await message.delete()
    await callback.answer()
