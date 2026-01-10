"""Calendar handler for viewing upcoming releases."""

from datetime import datetime, timedelta, timezone
from typing import Optional

import structlog
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot.clients.registry import get_radarr, get_sonarr
from bot.config import get_settings
from bot.db import Database
from bot.models import CalendarEvent, CalendarEventType, ContentType
from bot.ui.formatters import Formatters
from bot.ui.keyboards import CallbackData, Keyboards

logger = structlog.get_logger()
router = Router()

# Menu button text
MENU_CALENDAR = "📅 Календарь"

# Days to look ahead
CALENDAR_DAYS = 7


async def _fetch_calendar_events(
    content_filter: Optional[str] = None,
) -> list[CalendarEvent]:
    """
    Fetch calendar events from Radarr and Sonarr.

    Args:
        content_filter: "movie", "series", or None for all

    Returns:
        List of CalendarEvent objects sorted by release date
    """
    events: list[CalendarEvent] = []
    settings = get_settings()

    start_date = datetime.now(timezone.utc)
    end_date = start_date + timedelta(days=CALENDAR_DAYS)

    # Fetch movies from Radarr
    if content_filter in (None, "movie") and settings.radarr_enabled:
        radarr = get_radarr()
        if radarr:
            try:
                movies = await radarr.get_calendar(
                    start_date=start_date,
                    end_date=end_date,
                )
                for movie in movies:
                    # Parse date fields
                    release_date = None
                    for date_field in ["digitalRelease", "physicalRelease", "inCinemas"]:
                        if movie.get(date_field):
                            try:
                                release_date = datetime.fromisoformat(
                                    movie[date_field].replace("Z", "+00:00")
                                )
                                break
                            except (ValueError, AttributeError):
                                continue

                    if not release_date:
                        continue

                    events.append(CalendarEvent(
                        event_type=CalendarEventType.MOVIE,
                        title=movie.get("title", "Unknown"),
                        release_date=release_date,
                        overview=movie.get("overview"),
                        tmdb_id=movie.get("tmdbId"),
                        radarr_id=movie.get("id"),
                        year=movie.get("year"),
                        has_file=movie.get("hasFile", False),
                        is_available=movie.get("isAvailable", False),
                    ))
            except Exception as e:
                logger.warning("Failed to fetch Radarr calendar", error=str(e))

    # Fetch episodes from Sonarr
    if content_filter in (None, "series") and settings.sonarr_enabled:
        sonarr = get_sonarr()
        if sonarr:
            try:
                episodes = await sonarr.get_calendar(
                    start_date=start_date,
                    end_date=end_date,
                    include_series=True,
                )
                for ep in episodes:
                    # Parse air date
                    air_date_str = ep.get("airDateUtc") or ep.get("airDate")
                    if not air_date_str:
                        continue

                    try:
                        release_date = datetime.fromisoformat(
                            air_date_str.replace("Z", "+00:00")
                        )
                    except (ValueError, AttributeError):
                        continue

                    # Get series info
                    series = ep.get("series", {})

                    events.append(CalendarEvent(
                        event_type=CalendarEventType.EPISODE,
                        title=series.get("title", ep.get("title", "Unknown")),
                        release_date=release_date,
                        overview=ep.get("overview"),
                        tvdb_id=series.get("tvdbId"),
                        sonarr_id=ep.get("id"),
                        series_id=ep.get("seriesId"),
                        series_title=series.get("title"),
                        season_number=ep.get("seasonNumber", 0),
                        episode_number=ep.get("episodeNumber", 0),
                        episode_title=ep.get("title"),
                    ))
            except Exception as e:
                logger.warning("Failed to fetch Sonarr calendar", error=str(e))

    # Sort by release date
    events.sort(key=lambda e: e.release_date)

    return events


@router.message(F.text == MENU_CALENDAR)
@router.message(Command("calendar"))
async def cmd_calendar(message: Message) -> None:
    """Handle /calendar command and menu button."""
    await message.answer(
        "📅 <b>Календарь релизов</b>\n\n"
        "Выберите, какие релизы показать:",
        parse_mode="HTML",
        reply_markup=Keyboards.calendar_menu(),
    )


@router.callback_query(F.data == CallbackData.CALENDAR_MENU)
async def callback_calendar_menu(callback: CallbackQuery) -> None:
    """Show calendar menu."""
    if not callback.message:
        await callback.answer("Сообщение недоступно", show_alert=True)
        return

    await callback.message.edit_text(
        "📅 <b>Календарь релизов</b>\n\n"
        "Выберите, какие релизы показать:",
        parse_mode="HTML",
        reply_markup=Keyboards.calendar_menu(),
    )
    await callback.answer()


@router.callback_query(F.data == CallbackData.CALENDAR_ALL)
async def callback_calendar_all(callback: CallbackQuery) -> None:
    """Show all upcoming releases."""
    await callback.answer("Загрузка...")

    events = await _fetch_calendar_events(content_filter=None)
    text = Formatters.format_calendar_events(events, CALENDAR_DAYS, content_filter=None)

    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=Keyboards.calendar_events(events, content_filter=None),
    )


@router.callback_query(F.data == CallbackData.CALENDAR_MOVIES)
async def callback_calendar_movies(callback: CallbackQuery) -> None:
    """Show only movie releases."""
    await callback.answer("Загрузка...")

    events = await _fetch_calendar_events(content_filter="movie")
    text = Formatters.format_calendar_events(events, CALENDAR_DAYS, content_filter="movie")

    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=Keyboards.calendar_events(events, content_filter="movie"),
    )


@router.callback_query(F.data == CallbackData.CALENDAR_SERIES)
async def callback_calendar_series(callback: CallbackQuery) -> None:
    """Show only series releases."""
    await callback.answer("Загрузка...")

    events = await _fetch_calendar_events(content_filter="series")
    text = Formatters.format_calendar_events(events, CALENDAR_DAYS, content_filter="series")

    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=Keyboards.calendar_events(events, content_filter="series"),
    )


@router.callback_query(F.data == CallbackData.CALENDAR_REFRESH)
async def callback_calendar_refresh(callback: CallbackQuery) -> None:
    """Refresh calendar events."""
    await callback.answer("Обновляю...")

    # Try to determine current filter from message
    msg_text = callback.message.text or ""
    content_filter = None
    if "(фильмы)" in msg_text:
        content_filter = "movie"
    elif "(сериалы)" in msg_text:
        content_filter = "series"

    events = await _fetch_calendar_events(content_filter=content_filter)
    text = Formatters.format_calendar_events(events, CALENDAR_DAYS, content_filter=content_filter)

    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=Keyboards.calendar_events(events, content_filter=content_filter),
    )


@router.callback_query(F.data == CallbackData.CALENDAR_SUBSCRIBE)
async def callback_calendar_subscribe(callback: CallbackQuery) -> None:
    """Show subscription settings."""
    user_id = callback.from_user.id if callback.from_user else 0

    settings = get_settings()
    db = Database(settings.database_path)
    await db.connect()

    try:
        subscription = await db.get_calendar_subscription(user_id)

        if subscription and subscription.enabled:
            content_type_str = None
            if subscription.content_type:
                content_type_str = subscription.content_type.value

            await callback.message.edit_text(
                "🔔 <b>Уведомления о релизах</b>\n\n"
                "Вы получаете уведомления о предстоящих релизах "
                "за 1 день до выхода.",
                parse_mode="HTML",
                reply_markup=Keyboards.calendar_subscription(
                    is_subscribed=True,
                    content_type=content_type_str,
                ),
            )
        else:
            await callback.message.edit_text(
                "🔔 <b>Уведомления о релизах</b>\n\n"
                "Подпишитесь, чтобы получать уведомления "
                "о предстоящих релизах за 1 день до выхода.",
                parse_mode="HTML",
                reply_markup=Keyboards.calendar_subscription(is_subscribed=False),
            )
    finally:
        await db.close()

    await callback.answer()


@router.callback_query(F.data.startswith(CallbackData.CALENDAR_SUB_TOGGLE))
async def callback_calendar_toggle_subscription(callback: CallbackQuery) -> None:
    """Toggle calendar subscription."""
    user_id = callback.from_user.id if callback.from_user else 0
    sub_type = callback.data.split(":")[-1]  # "all", "movie", or "series"

    # Determine content type
    content_type: Optional[ContentType] = None
    if sub_type == "movie":
        content_type = ContentType.MOVIE
    elif sub_type == "series":
        content_type = ContentType.SERIES

    settings = get_settings()
    db = Database(settings.database_path)
    await db.connect()

    try:
        await db.create_calendar_subscription(
            user_id=user_id,
            content_type=content_type,
            notify_days_before=1,
        )

        type_text = "все релизы"
        if content_type == ContentType.MOVIE:
            type_text = "фильмы"
        elif content_type == ContentType.SERIES:
            type_text = "сериалы"

        await callback.answer(f"✅ Подписка на {type_text} оформлена!")

        await callback.message.edit_text(
            "🔔 <b>Уведомления о релизах</b>\n\n"
            f"✅ Вы подписались на {type_text}.\n"
            "Уведомления будут приходить за 1 день до выхода.",
            parse_mode="HTML",
            reply_markup=Keyboards.calendar_subscription(
                is_subscribed=True,
                content_type=content_type.value if content_type else None,
            ),
        )
    finally:
        await db.close()


@router.callback_query(F.data == CallbackData.CALENDAR_UNSUBSCRIBE)
async def callback_calendar_unsubscribe(callback: CallbackQuery) -> None:
    """Unsubscribe from calendar notifications."""
    user_id = callback.from_user.id if callback.from_user else 0

    settings = get_settings()
    db = Database(settings.database_path)
    await db.connect()

    try:
        await db.delete_calendar_subscription(user_id)
        await callback.answer("❌ Подписка отменена")

        await callback.message.edit_text(
            "🔔 <b>Уведомления о релизах</b>\n\n"
            "❌ Вы отписались от уведомлений о релизах.",
            parse_mode="HTML",
            reply_markup=Keyboards.calendar_subscription(is_subscribed=False),
        )
    finally:
        await db.close()
