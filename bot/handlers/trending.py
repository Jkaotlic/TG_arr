"""Trending/popular content handlers."""

import asyncio
import html

import structlog
from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from typing import Any

from bot.config import get_settings
from bot.clients.registry import get_qbittorrent, get_radarr, get_sonarr, get_tmdb
from bot.db import Database
from bot.handlers.common import accessible_message
from bot.handlers._cache import get_ttl, put_ttl
from bot.models import MovieInfo, SeriesInfo, User
from bot.services.add_service import AddService
from bot.services.search_service import SearchService
from bot.ui.callbacks import AddContentCB, TrendingItemCB
from bot.ui.formatters import Formatters
from bot.ui.keyboards import CallbackData, Keyboards
from bot.ui.menu import MENU_TRENDING

logger = structlog.get_logger()
router = Router()

# Cache for trending data to avoid re-fetching when viewing details.
# Keyed by tmdb_id for O(1) lookup; values are the plain item (kept as a bare
# value, not a (timestamp, item) tuple, so anything that still reads/writes
# the dict directly — including some pre-existing tests — keeps working).
# BUG-10/PERF-12: entries are TTL'd (6h) and evicted oldest-first on overflow
# instead of being clear()'d wholesale (which used to reset every other
# user's active list, not just stale ones).
_trending_movies_cache: dict[int, Any] = {}
_trending_series_cache: dict[int, Any] = {}

# Insertion timestamps live in a parallel side-table (same keys) so the main
# cache dicts stay plain `{key: value}` — direct pokes from other code/tests
# just won't get TTL tracking (treated as fresh) until they go through a
# _cache_put/_cache_get.
_trending_movies_inserted_at: dict[int, float] = {}
_trending_series_inserted_at: dict[int, float] = {}

_TIMESTAMPS = {
    id(_trending_movies_cache): _trending_movies_inserted_at,
    id(_trending_series_cache): _trending_series_inserted_at,
}

# Limit cache size to prevent unbounded growth
_MAX_CACHE_SIZE = 200

# PERF-12: trending items go stale — 6h TTL for a "yesterday's trending" click.
_CACHE_TTL_SECONDS = 6 * 60 * 60

# Lock for cache mutations (asyncio is single-threaded, but protects across awaits)
_cache_lock = asyncio.Lock()


def _cache_put(cache: dict[int, Any], key: int, value: Any) -> None:
    """Insert/refresh `key`, evicting the oldest entry when at capacity.

    Must be called while holding `_cache_lock`. Thin wrapper around the
    shared LRU+TTL cache helper (LOGIC-21) — kept as a module-level function
    (rather than inlining the call) because existing tests patch/call
    ``trending._cache_put`` directly.
    """
    timestamps = _TIMESTAMPS[id(cache)]
    put_ttl(cache, timestamps, key, value, _MAX_CACHE_SIZE)


def _cache_get(cache: dict[int, Any], key: int) -> Any | None:
    """Look up `key`, treating TTL-expired entries as a miss (and dropping them).

    Entries with no recorded timestamp (inserted via a direct `cache[key] =
    value`, bypassing `_cache_put`) are treated as always-fresh.
    """
    timestamps = _TIMESTAMPS[id(cache)]
    return get_ttl(cache, timestamps, key, _CACHE_TTL_SECONDS)


@router.message(F.text == MENU_TRENDING)
async def handle_trending_menu(message: Message) -> None:
    """Show trending/popular content selection menu."""
    settings = get_settings()

    show_music = settings.deezer_enabled and settings.lidarr_enabled
    if not settings.tmdb_enabled and not show_music:
        await message.answer(
            "❌ Топ контента недоступен.\n\n"
            "Для использования этой функции необходим TMDb API ключ.\n"
            "Получите бесплатный ключ на https://www.themoviedb.org/settings/api "
            "и добавьте его в переменную окружения TMDB_API_KEY."
        )
        return

    text = (
        "🔥 <b>Популярное сейчас</b>\n\n"
        "Выберите категорию для просмотра топа:"
    )

    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=Keyboards.trending_menu(show_music=show_music),
    )


@router.callback_query(F.data == CallbackData.TRENDING_BACK)
async def handle_trending_back(callback: CallbackQuery) -> None:
    """BUG-01: return to the trending menu.

    Trending lists previously used the shared CallbackData.BACK, which
    search.handle_back (registered first) swallowed with "Сессия истекла".
    A dedicated callback + handler re-renders the menu instead.
    """
    await callback.answer()
    message = accessible_message(callback)
    if message is None:
        return

    settings = get_settings()
    show_music = settings.deezer_enabled and settings.lidarr_enabled
    text = (
        "🔥 <b>Популярное сейчас</b>\n\n"
        "Выберите категорию для просмотра топа:"
    )
    await message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=Keyboards.trending_menu(show_music=show_music),
    )


@router.callback_query(F.data == CallbackData.TRENDING_MOVIES)
async def handle_trending_movies(callback: CallbackQuery) -> None:
    """Show trending/popular movies."""
    await callback.answer()
    message = accessible_message(callback)
    if message is None:
        return

    tmdb = await get_tmdb()
    if not tmdb:
        await message.edit_text(
            "❌ TMDb интеграция не настроена."
        )
        return

    # Show loading message
    await message.edit_text("⏳ Загружаю популярные фильмы...")

    try:
        # Get trending movies
        movies = await tmdb.get_trending_movies(time_window="week", page=1)

        if not movies:
            await message.edit_text(
                "😕 Не удалось загрузить популярные фильмы.\n"
                "Попробуйте позже."
            )
            return

        # Cache movies for detail views (merge into existing cache).
        # BUG-10/PERF-12: LRU-evict oldest instead of clear()'ing the whole
        # cache — an overflow no longer wipes other users' active lists.
        async with _cache_lock:
            for movie in movies:
                _cache_put(_trending_movies_cache, movie.tmdb_id, movie)

        # Format and send results
        text = Formatters.format_trending_movies(movies[:10])  # Top 10
        await message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=Keyboards.trending_movies(movies[:10]),
        )

    except Exception as e:
        logger.error("Failed to fetch trending movies", error=str(e), exc_info=True)
        await message.edit_text(
            Formatters.format_error("Не удалось загрузить популярные фильмы"),
            parse_mode="HTML",
        )


@router.callback_query(F.data == CallbackData.TRENDING_SERIES)
async def handle_trending_series(callback: CallbackQuery) -> None:
    """Show trending/popular TV series."""
    await callback.answer()
    message = accessible_message(callback)
    if message is None:
        return

    tmdb = await get_tmdb()
    if not tmdb:
        await message.edit_text(
            "❌ TMDb интеграция не настроена."
        )
        return

    # Show loading message
    await message.edit_text("⏳ Загружаю популярные сериалы...")

    try:
        # Get trending series
        series_list = await tmdb.get_trending_series(time_window="week", page=1)

        if not series_list:
            await message.edit_text(
                "😕 Не удалось загрузить популярные сериалы.\n"
                "Попробуйте позже."
            )
            return

        # Cache series for detail views (merge into existing cache).
        # BUG-10/PERF-12: LRU-evict oldest instead of clear()'ing the whole cache.
        async with _cache_lock:
            for series in series_list:
                _cache_put(_trending_series_cache, series.tmdb_id, series)

        # Format and send results
        text = Formatters.format_trending_series(series_list[:10])  # Top 10
        await message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=Keyboards.trending_series(series_list[:10]),
        )

    except Exception as e:
        logger.error("Failed to fetch trending series", error=str(e), exc_info=True)
        await message.edit_text(
            Formatters.format_error("Не удалось загрузить популярные сериалы"),
            parse_mode="HTML",
        )


@router.callback_query(TrendingItemCB.filter(F.kind == "movie"))
async def handle_movie_from_trending(callback: CallbackQuery, callback_data: TrendingItemCB) -> None:
    """Show movie details with poster when clicked from trending list."""
    await callback.answer()
    message = accessible_message(callback)
    if message is None:
        return

    try:
        tmdb_id = int(callback_data.item_id)
    except ValueError:
        await message.answer("❌ Неверный ID фильма")
        return

    # The trending list itself is the source of truth here: the item came
    # straight from TMDb, keyed by a TMDb id Radarr's own library lookup
    # doesn't take — so a cache miss means "the list is stale", not "look it
    # up elsewhere".
    movie = _cache_get(_trending_movies_cache, tmdb_id)

    if not movie:
        await message.answer(
            "❌ Фильм не найден в кэше.\n"
            "Попробуйте обновить список или используйте обычный поиск."
        )
        return

    # Send poster with movie details
    caption = Formatters.format_movie_with_poster(movie)

    if movie.poster_url:
        try:
            await message.answer_photo(
                photo=movie.poster_url,
                caption=caption,
                parse_mode="HTML",
                reply_markup=Keyboards.movie_details(movie),
            )
        except Exception as e:
            logger.error("Failed to send poster", error=str(e), exc_info=True)
            # Fallback to text only
            await message.answer(
                caption,
                parse_mode="HTML",
                reply_markup=Keyboards.movie_details(movie),
            )
    else:
        # No poster available
        await message.answer(
            caption,
            parse_mode="HTML",
            reply_markup=Keyboards.movie_details(movie),
        )


@router.callback_query(TrendingItemCB.filter(F.kind == "series"))
async def handle_series_from_trending(callback: CallbackQuery, callback_data: TrendingItemCB) -> None:
    """Show series details with poster when clicked from trending list."""
    await callback.answer()
    message = accessible_message(callback)
    if message is None:
        return

    try:
        series_id = int(callback_data.item_id)
    except ValueError:
        await message.answer("❌ Неверный ID сериала")
        return

    # Try to get series from cache first (if from trending)
    series = _cache_get(_trending_series_cache, series_id)

    if not series:
        # series_id is a TMDb id from trending — not a Sonarr id
        await message.answer(
            "❌ Сериал не найден в кэше.\n"
            "Попробуйте обновить список или используйте обычный поиск."
        )
        return

    # Send poster with series details
    caption = Formatters.format_series_with_poster(series)

    if series.poster_url:
        try:
            await message.answer_photo(
                photo=series.poster_url,
                caption=caption,
                parse_mode="HTML",
                reply_markup=Keyboards.series_details(series),
            )
        except Exception as e:
            logger.error("Failed to send poster", error=str(e), exc_info=True)
            # Fallback to text only
            await message.answer(
                caption,
                parse_mode="HTML",
                reply_markup=Keyboards.series_details(series),
            )
    else:
        # No poster available
        await message.answer(
            caption,
            parse_mode="HTML",
            reply_markup=Keyboards.series_details(series),
        )


async def _resolve_series_for_add(search_service: SearchService, series: SeriesInfo) -> SeriesInfo | None:
    """TMDb trending carries no `tvdb_id` (see `TMDbClient.get_trending_series`
    — it stays 0, "resolved by name" was Scryer's job) but Sonarr's
    `add_series` needs a real one. Resolve it via Sonarr's own guarded
    lookup (`SearchService.lookup_series`, same semaphore/circuit-breaker
    path every other Sonarr lookup goes through) before adding.

    Returns `series` unchanged if it already carries a `tvdb_id`, the best
    title/year match from Sonarr's lookup otherwise, or `None` if Sonarr
    has nothing matching the title at all.
    """
    if series.tvdb_id:
        return series
    candidates = await search_service.lookup_series(series.title)
    if not candidates:
        return None
    if series.year:
        exact = next((c for c in candidates if c.year == series.year), None)
        if exact is not None:
            return exact
    return candidates[0]


async def _add_movie_from_trending(callback: CallbackQuery, movie: MovieInfo, db_user: User, db: Database) -> None:
    """Add a trending movie to Radarr, using the user's Radarr profile/folder
    preference (falling back to "first available" — see `AddService.resolve_profile`).
    """
    message = accessible_message(callback)
    if message is None:
        return
    status_msg = await message.answer("⏳ Добавляю фильм в Radarr...")

    try:
        radarr = await get_radarr()
        add_service = AddService(radarr, await get_sonarr(), qbittorrent=await get_qbittorrent())

        profiles, folders = await asyncio.gather(
            add_service.get_radarr_profiles(), add_service.get_radarr_root_folders(),
        )
        if not profiles or not folders:
            await status_msg.edit_text(
                Formatters.format_error("В Radarr не настроены профили качества или папки")
            )
            return

        prefs = db_user.preferences
        profile = AddService.resolve_profile(profiles, prefs.radarr_quality_profile_id)
        folder_path = AddService.resolve_root_folder(folders, prefs.radarr_root_folder_id)

        added, action = await add_service.add_movie(
            movie, quality_profile_id=profile.id, root_folder_path=folder_path,
        )
        action.user_id = db_user.tg_id
        await db.log_action(action)

        if added is not None:
            year_str = f" ({added.year})" if added.year else ""
            await status_msg.edit_text(
                f"✅ <b>{html.escape(added.title)}</b>{year_str} добавлен в Radarr — ищу релиз...",
                parse_mode="HTML",
            )
        else:
            # BUG-12b: error text can contain raw markup from the upstream
            # service — escape before interpolating into an HTML message.
            error_text = action.error_message or "Не удалось добавить фильм"
            await status_msg.edit_text(f"❌ {html.escape(error_text)[:200]}")

    except Exception as e:
        logger.error(
            "trending_add_movie_failed", title=movie.title, error=str(e), exc_info=True,
        )
        await status_msg.edit_text(
            Formatters.format_error("Не удалось добавить"), parse_mode="HTML",
        )


async def _add_series_from_trending(callback: CallbackQuery, series: SeriesInfo, db_user: User, db: Database) -> None:
    """Add a trending series to Sonarr — first resolving a real `tvdb_id`
    (TMDb trending doesn't carry one), then the user's Sonarr profile/folder
    preference (falling back to "first available")."""
    message = accessible_message(callback)
    if message is None:
        return
    status_msg = await message.answer("⏳ Добавляю сериал в Sonarr...")

    try:
        sonarr = await get_sonarr()
        add_service = AddService(await get_radarr(), sonarr, qbittorrent=await get_qbittorrent())
        search_service = SearchService(add_service.radarr, sonarr)

        resolved = await _resolve_series_for_add(search_service, series)
        if resolved is None:
            await status_msg.edit_text(
                Formatters.format_warning(
                    f"Не удалось сопоставить <b>{html.escape(series.title)}</b> с Sonarr"
                ),
                parse_mode="HTML",
            )
            return

        profiles, folders = await asyncio.gather(
            add_service.get_sonarr_profiles(), add_service.get_sonarr_root_folders(),
        )
        if not profiles or not folders:
            await status_msg.edit_text(
                Formatters.format_error("В Sonarr не настроены профили качества или папки")
            )
            return

        prefs = db_user.preferences
        profile = AddService.resolve_profile(profiles, prefs.sonarr_quality_profile_id)
        folder_path = AddService.resolve_root_folder(folders, prefs.sonarr_root_folder_id)

        added, action = await add_service.add_series(
            resolved, quality_profile_id=profile.id, root_folder_path=folder_path,
        )
        action.user_id = db_user.tg_id
        await db.log_action(action)

        if added is not None:
            year_str = f" ({added.year})" if added.year else ""
            await status_msg.edit_text(
                f"✅ <b>{html.escape(added.title)}</b>{year_str} добавлен в Sonarr — ищу релиз...",
                parse_mode="HTML",
            )
        else:
            error_text = action.error_message or "Не удалось добавить сериал"
            await status_msg.edit_text(f"❌ {html.escape(error_text)[:200]}")

    except Exception as e:
        logger.error(
            "trending_add_series_failed", title=series.title, error=str(e), exc_info=True,
        )
        await status_msg.edit_text(
            Formatters.format_error("Не удалось добавить"), parse_mode="HTML",
        )


@router.callback_query(AddContentCB.filter(F.kind == "movie"))
async def handle_add_movie_from_trending(
    callback: CallbackQuery, callback_data: AddContentCB, db_user: User, db: Database
) -> None:
    """Add a movie to Radarr from the trending list."""
    await callback.answer()
    message = accessible_message(callback)
    if message is None:
        return

    tmdb_id = callback_data.tmdb_id
    movie = _cache_get(_trending_movies_cache, tmdb_id)

    if not movie:
        await message.answer(
            "❌ Фильм не найден в кэше.\n"
            "Попробуйте обновить список или используйте обычный поиск."
        )
        return

    await _add_movie_from_trending(callback, movie, db_user, db)


@router.callback_query(AddContentCB.filter(F.kind == "series"))
async def handle_add_series_from_trending(
    callback: CallbackQuery, callback_data: AddContentCB, db_user: User, db: Database
) -> None:
    """Add a series to Sonarr from the trending list."""
    await callback.answer()
    message = accessible_message(callback)
    if message is None:
        return

    tmdb_id = callback_data.tmdb_id
    series = _cache_get(_trending_series_cache, tmdb_id)

    if not series:
        await message.answer(
            "❌ Сериал не найден в кэше.\n"
            "Попробуйте обновить список или используйте обычный поиск."
        )
        return

    await _add_series_from_trending(callback, series, db_user, db)


# ---------------------------------------------------------------------------
# r5: legacy string-callback fallbacks — trend_m:/trend_s:/add_movie:/
# add_series: buttons from messages sent before the TrendingItemCB/
# AddContentCB migration. TRENDING_ARTIST's legacy fallback lives in
# music.py (it owns that handler family).
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith(CallbackData.TRENDING_MOVIE))
@router.callback_query(F.data.startswith(CallbackData.TRENDING_SERIES_ITEM))
@router.callback_query(F.data.startswith(CallbackData.ADD_MOVIE))
@router.callback_query(F.data.startswith(CallbackData.ADD_SERIES))
async def handle_legacy_trending_item(callback: CallbackQuery) -> None:
    """r5: legacy ``trend_m:``/``trend_s:``/``add_movie:``/``add_series:``
    string buttons — surface an explicit alert instead of falling through
    unhandled.
    """
    await callback.answer("Кнопка устарела — обновите список", show_alert=True)
