"""Calendar/schedule handlers — upcoming episodes and movie releases."""

import structlog
from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from bot.clients.registry import get_radarr, get_sonarr
from bot.ui.formatters import Formatters
from bot.ui.keyboards import CallbackData, Keyboards

logger = structlog.get_logger()
router = Router()

# Menu button text
MENU_CALENDAR = "📅 Календарь"

# Store current period per-user so refresh keeps the same range
_user_period: dict[int, int] = {}


async def _fetch_and_send_calendar(
    days: int,
    *,
    answer_func,
    edit: bool = False,
) -> None:
    """Fetch calendar data from Sonarr & Radarr and send/edit the message."""
    sonarr = get_sonarr()
    radarr = get_radarr()

    episodes: list[dict] = []
    movies: list[dict] = []
    errors: list[str] = []

    try:
        episodes = await sonarr.get_calendar(days=days)
    except Exception as e:
        logger.error("Sonarr calendar error", error=str(e))
        errors.append(f"Sonarr: {e}")

    try:
        movies = await radarr.get_calendar(days=days)
    except Exception as e:
        logger.error("Radarr calendar error", error=str(e))
        errors.append(f"Radarr: {e}")

    text = Formatters.format_calendar(episodes, movies, days=days)
    if errors:
        text += "\n\n⚠️ " + " | ".join(errors)

    kwargs = dict(
        text=text,
        parse_mode="HTML",
        reply_markup=Keyboards.calendar_controls(current_days=days),
    )

    if edit:
        await answer_func(**kwargs)
    else:
        await answer_func(**kwargs)


@router.message(F.text == MENU_CALENDAR)
async def handle_calendar_menu(message: Message) -> None:
    """Show calendar for the next 7 days (default)."""
    user_id = message.from_user.id if message.from_user else 0
    days = _user_period.get(user_id, 7)
    _user_period[user_id] = days

    await _fetch_and_send_calendar(
        days,
        answer_func=message.answer,
        edit=False,
    )


@router.callback_query(F.data == CallbackData.CALENDAR_7)
async def handle_calendar_7(callback: CallbackQuery) -> None:
    """Switch calendar to 7 days."""
    await callback.answer()
    user_id = callback.from_user.id
    _user_period[user_id] = 7
    await _fetch_and_send_calendar(
        7,
        answer_func=callback.message.edit_text,
        edit=True,
    )


@router.callback_query(F.data == CallbackData.CALENDAR_14)
async def handle_calendar_14(callback: CallbackQuery) -> None:
    """Switch calendar to 14 days."""
    await callback.answer()
    user_id = callback.from_user.id
    _user_period[user_id] = 14
    await _fetch_and_send_calendar(
        14,
        answer_func=callback.message.edit_text,
        edit=True,
    )


@router.callback_query(F.data == CallbackData.CALENDAR_30)
async def handle_calendar_30(callback: CallbackQuery) -> None:
    """Switch calendar to 30 days."""
    await callback.answer()
    user_id = callback.from_user.id
    _user_period[user_id] = 30
    await _fetch_and_send_calendar(
        30,
        answer_func=callback.message.edit_text,
        edit=True,
    )


@router.callback_query(F.data == CallbackData.CALENDAR_REFRESH)
async def handle_calendar_refresh(callback: CallbackQuery) -> None:
    """Refresh calendar without changing period."""
    await callback.answer("🔄 Обновляю...")
    user_id = callback.from_user.id
    days = _user_period.get(user_id, 7)
    await _fetch_and_send_calendar(
        days,
        answer_func=callback.message.edit_text,
        edit=True,
    )
