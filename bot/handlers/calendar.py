"""Calendar/schedule handlers — upcoming episodes and movie releases."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Optional

import structlog
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot.clients.radarr import RadarrClient
from bot.clients.sonarr import SonarrClient
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


def _is_movie_calendar_item(item: dict[str, Any]) -> bool:
    """Tells a Radarr calendar row from a Sonarr one after they've been
    merged by `_collect_calendar`. Radarr's rows always carry `release_date`
    (`RadarrClient.get_calendar`), Sonarr's always carry `air_date`
    (`SonarrClient.get_calendar`) instead — the same distinction
    `_calendar_date_key` already relies on. Used to split the merged list
    back into the separate movies/episodes buckets
    `Formatters.format_calendar` renders.
    """
    return "release_date" in item


async def _collect_calendar(
    radarr: RadarrClient, sonarr: SonarrClient, days: int, *,
    errors: Optional[list[str]] = None,
) -> list[dict[str, Any]]:
    """Fetch Radarr's and Sonarr's calendars concurrently, merged and sorted
    by date into one list.

    A small, directly-testable step. Default (`errors=None`, the original
    contract this function shipped with): no per-source error tolerance — a
    failing client's exception propagates to the caller.

    Review fix round 1 (2026-08-10, task-13 re-review): `_fetch_and_send_calendar`
    used to reimplement an equivalent Radarr+Sonarr gather inline instead of
    calling this function — two implementations of the same fetch that could
    silently diverge on a future edit. It now calls this directly. To keep
    its per-source "⚠️ Radarr: ..." error reporting without a second,
    separately-maintained error-tolerant reimplementation, pass a mutable
    `errors` list: a failing source then contributes nothing (instead of
    raising) and an "<Source>: <message>" entry is appended to `errors`, so
    the caller learns which source failed without losing the other's data.
    """
    if errors is None:
        movies, episodes = await asyncio.gather(
            radarr.get_calendar(days=days),
            sonarr.get_calendar(days=days),
        )
    else:
        # SEC-21: text is sent with parse_mode=HTML — escape exception strings.
        import html as _html

        results = await asyncio.gather(
            radarr.get_calendar(days=days),
            sonarr.get_calendar(days=days),
            return_exceptions=True,
        )
        movies, episodes = [], []
        for source, result in zip(("Radarr", "Sonarr"), results, strict=True):
            if isinstance(result, BaseException):
                logger.error(
                    "calendar_fetch_failed", service=source, error=str(result), exc_info=result,
                )
                errors.append(f"{source}: {_html.escape(str(result))[:100]}")
            elif source == "Radarr":
                movies = result
            else:
                episodes = result

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

    # PERF-03/LOGIC-05: Radarr+Sonarr (via _collect_calendar, error-tolerant
    # when `errors=` is passed) and Lidarr run concurrently in one gather —
    # nesting _collect_calendar's own inner gather inside this one does not
    # serialize anything; all three sources still start together.
    # return_exceptions=True covers Lidarr's slot (_collect_calendar's own
    # slot never raises once `errors=` is set — it swallows its own
    # per-source failures internally, see its docstring).
    # Объявлены до ветвления: `combined` присваивается в обеих ветках, но с
    # разной формой (распаковка gather против прямого await), и без явного
    # объявления mypy не может определить тип `lidarr_result` вовсе.
    combined: Any
    lidarr_result: Any
    if lidarr is not None:
        combined, lidarr_result = await asyncio.gather(
            _collect_calendar(radarr, sonarr, days, errors=errors),
            lidarr.get_calendar(days=days),
            return_exceptions=True,
        )
        if isinstance(lidarr_result, BaseException):
            logger.error(
                "calendar_fetch_failed", service="Lidarr", error=str(lidarr_result), exc_info=lidarr_result,
            )
            errors.append(f"Lidarr: {_html.escape(str(lidarr_result))[:100]}")
            albums: list = []
        else:
            albums = list(lidarr_result)
    else:
        combined = await _collect_calendar(radarr, sonarr, days, errors=errors)
        albums = []

    # Split the merged, date-sorted list back into the separate
    # movies/episodes buckets Formatters.format_calendar renders.
    movies = [item for item in combined if _is_movie_calendar_item(item)]
    episodes = [item for item in combined if not _is_movie_calendar_item(item)]

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
