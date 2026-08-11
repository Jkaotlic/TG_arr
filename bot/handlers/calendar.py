"""Calendar/schedule handlers — upcoming episodes and movie releases."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot.clients.registry import get_lidarr, get_radarr, get_sonarr
from bot.handlers.common import accessible_message, swallow_not_modified
from bot.ui.callbacks import CalCB
from bot.ui.formatters import Formatters
from bot.ui.keyboards import CallbackData, Keyboards
from bot.ui.menu import MENU_CALENDAR

logger = structlog.get_logger()
router = Router()

# Store current period per-user so refresh keeps the same range
# Limited to prevent unbounded growth (whitelist bots have few users anyway)
_user_period: dict[int, int] = {}
_MAX_USER_PERIOD_ENTRIES = 100

# Lock for _user_period mutations (protects across awaits)
_period_lock = asyncio.Lock()


def _calendar_date_key(item: dict[str, Any]) -> str:
    """The date any calendar entry is sorted by.

    Radarr's movie dicts (`RadarrClient.get_calendar`) carry `release_date`
    (already resolved from digital/physical/cinema); Sonarr's episode dicts
    (`SonarrClient.get_calendar`) carry `air_date`. Checking both, in that
    order, lets one sort key work for a merged list of either shape.
    """
    return (
        item.get("release_date")
        or item.get("air_date")
        or item.get("digital_release")
        or item.get("physical_release")
        or item.get("in_cinemas")
        or ""
    )


async def _collect_calendar(radarr, sonarr, days: int) -> list[dict[str, Any]]:
    """Fetch Radarr's and Sonarr's calendars concurrently, merged and sorted
    by date into one list.

    A small, pure, directly-testable step — no per-source error tolerance
    here (a failing client's exception propagates to the caller); that
    tolerance lives in `_fetch_and_send_calendar`, which needs to know WHICH
    source failed so it can report it by name.
    """
    movies, episodes = await asyncio.gather(
        radarr.get_calendar(days=days),
        sonarr.get_calendar(days=days),
    )
    combined = list(movies) + list(episodes)
    return sorted(combined, key=_calendar_date_key)


async def _fetch_and_send_calendar(
    days: int,
    *,
    answer_func: Callable[..., Awaitable[Any]],
) -> None:
    """Fetch calendar data from Radarr/Sonarr (+ Lidarr for music) and send/edit."""
    radarr = await get_radarr()
    sonarr = await get_sonarr()
    lidarr = await get_lidarr()

    errors: list[str] = []

    # SEC-21: text is sent with parse_mode=HTML — escape exception strings.
    import html as _html

    # PERF-03/LOGIC-05: fetch the Radarr/Sonarr/Lidarr calendars concurrently.
    # return_exceptions=True keeps the per-source error tolerance: a failing
    # source contributes an empty list + a warning entry while the other
    # still renders.
    fetchers: list[tuple[str, Any]] = [
        ("Radarr", radarr.get_calendar(days=days)),
        ("Sonarr", sonarr.get_calendar(days=days)),
    ]
    if lidarr is not None:
        fetchers.append(("Lidarr", lidarr.get_calendar(days=days)))

    results = await asyncio.gather(
        *(coro for _, coro in fetchers),
        return_exceptions=True,
    )

    payloads: dict[str, list] = {}
    for (source, _), result in zip(fetchers, results, strict=True):
        if isinstance(result, BaseException):
            logger.error("calendar_fetch_failed", service=source, error=str(result), exc_info=result)
            errors.append(f"{source}: {_html.escape(str(result))[:100]}")
        else:
            payloads[source] = list(result)

    # Radarr's calendar rows are already movie-shaped, Sonarr's already
    # episode-shaped — no split step needed (that was Scryer's single
    # calendarEpisodes query covering every facet; each *arr client answers
    # its own facet directly).
    movies = payloads.get("Radarr", [])
    episodes = payloads.get("Sonarr", [])
    albums = payloads.get("Lidarr", [])

    text = Formatters.format_calendar(episodes, movies, days=days, albums=albums)
    if errors:
        text += "\n\n⚠️ " + " | ".join(errors)

    # BUG-17a: repeating the currently-active period (e.g. tapping "7 дней"
    # again) produces identical text/markup — Telegram rejects the edit.
    # swallow_not_modified only silences that specific, harmless case;
    # anything else re-raises.
    await swallow_not_modified(
        answer_func(
            text=text,
            parse_mode="HTML",
            reply_markup=Keyboards.calendar_controls(current_days=days),
        )
    )


@router.message(F.text == MENU_CALENDAR)
@router.message(Command("calendar"))
async def handle_calendar_menu(message: Message) -> None:
    """Show calendar for the next 7 days (default).

    Reachable both from the reply-keyboard button and as `/calendar`, which is
    what the published command menu offers.
    """
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


@router.callback_query(CalCB.filter())
async def handle_calendar_period(callback: CallbackQuery, callback_data: CalCB) -> None:
    """Switch calendar to the requested period (was ``cal_7``/``cal_14``/``cal_30``)."""
    await callback.answer()
    message = accessible_message(callback)
    if message is None:
        return
    days = callback_data.days
    user_id = callback.from_user.id
    async with _period_lock:
        _user_period[user_id] = days
    await _fetch_and_send_calendar(
        days,
        answer_func=message.edit_text,
    )


@router.callback_query(F.data.in_({"cal_7", "cal_14", "cal_30"}))
async def handle_legacy_calendar_period(callback: CallbackQuery) -> None:
    """r5: legacy ``cal_7``/``cal_14``/``cal_30`` string buttons from messages
    sent before the CalCB migration — surface an explicit alert instead of
    falling through unhandled.
    """
    await callback.answer("Кнопка устарела — откройте календарь заново", show_alert=True)


@router.callback_query(F.data == CallbackData.CALENDAR_REFRESH)
async def handle_calendar_refresh(callback: CallbackQuery) -> None:
    """Refresh calendar without changing period."""
    await callback.answer("🔄 Обновляю...")
    message = accessible_message(callback)
    if message is None:
        return
    user_id = callback.from_user.id
    days = _user_period.get(user_id, 7)
    await _fetch_and_send_calendar(
        days,
        answer_func=message.edit_text,
    )
