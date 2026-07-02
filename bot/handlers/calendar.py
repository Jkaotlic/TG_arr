"""Calendar/schedule handlers — upcoming episodes and movie releases."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message

from bot.clients.registry import get_lidarr, get_radarr, get_sonarr
from bot.ui.formatters import Formatters
from bot.ui.keyboards import CallbackData, Keyboards

logger = structlog.get_logger()
router = Router()

# Menu button text
MENU_CALENDAR = "📅 Календарь"

# Store current period per-user so refresh keeps the same range
# Limited to prevent unbounded growth (whitelist bots have few users anyway)
_user_period: dict[int, int] = {}
_MAX_USER_PERIOD_ENTRIES = 100

# Lock for _user_period mutations (protects across awaits)
_period_lock = asyncio.Lock()


async def _fetch_and_send_calendar(
    days: int,
    *,
    answer_func: Callable[..., Awaitable[Any]],
) -> None:
    """Fetch calendar data from Sonarr & Radarr and send/edit the message."""
    sonarr = await get_sonarr()
    radarr = await get_radarr()
    lidarr = await get_lidarr()

    episodes: list[dict] = []
    movies: list[dict] = []
    albums: list[dict] = []
    errors: list[str] = []

    # SEC-21: text is sent with parse_mode=HTML — escape exception strings.
    import html as _html

    # PERF-03/LOGIC-05: fetch the Sonarr/Radarr/Lidarr calendars concurrently.
    # return_exceptions=True keeps the same per-source error tolerance: a
    # failing source contributes an empty list + a warning entry while the
    # others still render.
    fetchers: list[tuple[str, Any]] = [
        ("Sonarr", sonarr.get_calendar(days=days)),
        ("Radarr", radarr.get_calendar(days=days)),
    ]
    if lidarr is not None:
        fetchers.append(("Lidarr", lidarr.get_calendar(days=days)))

    results = await asyncio.gather(
        *(coro for _, coro in fetchers),
        return_exceptions=True,
    )

    payloads: dict[str, list[dict]] = {}
    for (source, _), result in zip(fetchers, results, strict=True):
        if isinstance(result, Exception):
            logger.error("calendar_fetch_failed", service=source, error=str(result), exc_info=result)
            errors.append(f"{source}: {_html.escape(str(result))[:100]}")
        else:
            payloads[source] = result

    episodes = payloads.get("Sonarr", [])
    movies = payloads.get("Radarr", [])
    albums = payloads.get("Lidarr", [])

    text = Formatters.format_calendar(episodes, movies, days=days, albums=albums)
    if errors:
        text += "\n\n⚠️ " + " | ".join(errors)

    try:
        await answer_func(
            text=text,
            parse_mode="HTML",
            reply_markup=Keyboards.calendar_controls(current_days=days),
        )
    except TelegramBadRequest as e:
        # BUG-17a: repeating the currently-active period (e.g. tapping "7 дней"
        # again) produces identical text/markup — Telegram rejects the edit.
        # Swallow only that specific, harmless case; anything else re-raises.
        if "message is not modified" not in str(e):
            raise


@router.message(F.text == MENU_CALENDAR)
async def handle_calendar_menu(message: Message) -> None:
    """Show calendar for the next 7 days (default)."""
    user_id = message.from_user.id if message.from_user else 0
    days = _user_period.get(user_id, 7)
    async with _period_lock:
        if len(_user_period) >= _MAX_USER_PERIOD_ENTRIES:
            _user_period.clear()
        _user_period[user_id] = days

    await _fetch_and_send_calendar(
        days,
        answer_func=message.answer,
    )


@router.callback_query(F.data == CallbackData.CALENDAR_7)
async def handle_calendar_7(callback: CallbackQuery) -> None:
    """Switch calendar to 7 days."""
    await callback.answer()
    if not callback.message:
        return
    user_id = callback.from_user.id
    async with _period_lock:
        _user_period[user_id] = 7
    await _fetch_and_send_calendar(
        7,
        answer_func=callback.message.edit_text,
    )


@router.callback_query(F.data == CallbackData.CALENDAR_14)
async def handle_calendar_14(callback: CallbackQuery) -> None:
    """Switch calendar to 14 days."""
    await callback.answer()
    if not callback.message:
        return
    user_id = callback.from_user.id
    async with _period_lock:
        _user_period[user_id] = 14
    await _fetch_and_send_calendar(
        14,
        answer_func=callback.message.edit_text,
    )


@router.callback_query(F.data == CallbackData.CALENDAR_30)
async def handle_calendar_30(callback: CallbackQuery) -> None:
    """Switch calendar to 30 days."""
    await callback.answer()
    if not callback.message:
        return
    user_id = callback.from_user.id
    async with _period_lock:
        _user_period[user_id] = 30
    await _fetch_and_send_calendar(
        30,
        answer_func=callback.message.edit_text,
    )


@router.callback_query(F.data == CallbackData.CALENDAR_REFRESH)
async def handle_calendar_refresh(callback: CallbackQuery) -> None:
    """Refresh calendar without changing period."""
    await callback.answer("🔄 Обновляю...")
    if not callback.message:
        return
    user_id = callback.from_user.id
    days = _user_period.get(user_id, 7)
    await _fetch_and_send_calendar(
        days,
        answer_func=callback.message.edit_text,
    )
