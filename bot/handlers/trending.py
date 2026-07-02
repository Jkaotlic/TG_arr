"""Trending/popular content handlers."""

import asyncio
import html
import time

import structlog
from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from typing import Any

from bot.config import get_settings
from bot.clients.registry import get_tmdb, get_radarr, get_sonarr, get_qbittorrent, get_prowlarr
from bot.db import Database
from bot.models import User
from bot.services.add_service import AddService
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

    Must be called while holding `_cache_lock`. Pops-then-reinserts so a
    refreshed key also becomes the freshest for eviction ordering (dict
    iteration order = insertion order in CPython).
    """
    timestamps = _TIMESTAMPS[id(cache)]
    cache.pop(key, None)
    timestamps.pop(key, None)
    while len(cache) >= _MAX_CACHE_SIZE:
        oldest = next(iter(cache))
        cache.pop(oldest, None)
        timestamps.pop(oldest, None)
    cache[key] = value
    timestamps[key] = time.monotonic()


def _cache_get(cache: dict[int, Any], key: int) -> Any | None:
    """Look up `key`, treating TTL-expired entries as a miss (and dropping them).

    Entries with no recorded timestamp (inserted via a direct `cache[key] =
    value`, bypassing `_cache_put`) are treated as always-fresh.
    """
    if key not in cache:
        return None
    timestamps = _TIMESTAMPS[id(cache)]
    inserted_at = timestamps.get(key)
    if inserted_at is not None and time.monotonic() - inserted_at > _CACHE_TTL_SECONDS:
        cache.pop(key, None)
        timestamps.pop(key, None)
        return None
    return cache[key]


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
    if not callback.message:
        return

    settings = get_settings()
    show_music = settings.deezer_enabled and settings.lidarr_enabled
    text = (
        "🔥 <b>Популярное сейчас</b>\n\n"
        "Выберите категорию для просмотра топа:"
    )
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=Keyboards.trending_menu(show_music=show_music),
    )


@router.callback_query(F.data == CallbackData.TRENDING_MOVIES)
async def handle_trending_movies(callback: CallbackQuery) -> None:
    """Show trending/popular movies."""
    await callback.answer()
    if not callback.message:
        return

    tmdb = await get_tmdb()
    if not tmdb:
        await callback.message.edit_text(
            "❌ TMDb интеграция не настроена."
        )
        return

    # Show loading message
    await callback.message.edit_text("⏳ Загружаю популярные фильмы...")

    try:
        # Get trending movies
        movies = await tmdb.get_trending_movies(time_window="week", page=1)

        if not movies:
            await callback.message.edit_text(
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
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=Keyboards.trending_movies(movies[:10]),
        )

    except Exception as e:
        logger.error("Failed to fetch trending movies", error=str(e), exc_info=True)
        await callback.message.edit_text(
            Formatters.format_error("Не удалось загрузить популярные фильмы"),
            parse_mode="HTML",
        )


@router.callback_query(F.data == CallbackData.TRENDING_SERIES)
async def handle_trending_series(callback: CallbackQuery) -> None:
    """Show trending/popular TV series."""
    await callback.answer()
    if not callback.message:
        return

    tmdb = await get_tmdb()
    if not tmdb:
        await callback.message.edit_text(
            "❌ TMDb интеграция не настроена."
        )
        return

    # Show loading message
    await callback.message.edit_text("⏳ Загружаю популярные сериалы...")

    try:
        # Get trending series
        series_list = await tmdb.get_trending_series(time_window="week", page=1)

        if not series_list:
            await callback.message.edit_text(
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
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=Keyboards.trending_series(series_list[:10]),
        )

    except Exception as e:
        logger.error("Failed to fetch trending series", error=str(e), exc_info=True)
        await callback.message.edit_text(
            Formatters.format_error("Не удалось загрузить популярные сериалы"),
            parse_mode="HTML",
        )


@router.callback_query(TrendingItemCB.filter(F.kind == "movie"))
async def handle_movie_from_trending(callback: CallbackQuery, callback_data: TrendingItemCB) -> None:
    """Show movie details with poster when clicked from trending list."""
    await callback.answer()
    if not callback.message:
        return

    try:
        tmdb_id = int(callback_data.item_id)
    except ValueError:
        await callback.message.answer("❌ Неверный ID фильма")
        return

    # Try to get movie from cache first
    movie = _cache_get(_trending_movies_cache, tmdb_id)

    if not movie:
        # If not in cache, fetch from Radarr
        radarr = await get_radarr()
        try:
            movie = await radarr.lookup_movie_by_tmdb(tmdb_id)
        except Exception as e:
            logger.error("Failed to lookup movie", tmdb_id=tmdb_id, error=str(e), exc_info=True)
            await callback.message.answer(
                Formatters.format_error("Не удалось найти фильм"),
                parse_mode="HTML",
            )
            return

    if not movie:
        await callback.message.answer("❌ Фильм не найден")
        return

    # Send poster with movie details
    caption = Formatters.format_movie_with_poster(movie)

    if movie.poster_url:
        try:
            await callback.message.answer_photo(
                photo=movie.poster_url,
                caption=caption,
                parse_mode="HTML",
                reply_markup=Keyboards.movie_details(movie),
            )
        except Exception as e:
            logger.error("Failed to send poster", error=str(e), exc_info=True)
            # Fallback to text only
            await callback.message.answer(
                caption,
                parse_mode="HTML",
                reply_markup=Keyboards.movie_details(movie),
            )
    else:
        # No poster available
        await callback.message.answer(
            caption,
            parse_mode="HTML",
            reply_markup=Keyboards.movie_details(movie),
        )


@router.callback_query(TrendingItemCB.filter(F.kind == "series"))
async def handle_series_from_trending(callback: CallbackQuery, callback_data: TrendingItemCB) -> None:
    """Show series details with poster when clicked from trending list."""
    await callback.answer()
    if not callback.message:
        return

    try:
        series_id = int(callback_data.item_id)
    except ValueError:
        await callback.message.answer("❌ Неверный ID сериала")
        return

    # Try to get series from cache first (if from trending)
    series = _cache_get(_trending_series_cache, series_id)

    if not series:
        # series_id is a TMDb ID from trending — cannot use as TVDB ID for Sonarr lookup
        await callback.message.answer(
            "❌ Сериал не найден в кэше.\n"
            "Попробуйте обновить список или используйте обычный поиск."
        )
        return

    # Send poster with series details
    caption = Formatters.format_series_with_poster(series)

    if series.poster_url:
        try:
            await callback.message.answer_photo(
                photo=series.poster_url,
                caption=caption,
                parse_mode="HTML",
                reply_markup=Keyboards.series_details(series),
            )
        except Exception as e:
            logger.error("Failed to send poster", error=str(e), exc_info=True)
            # Fallback to text only
            await callback.message.answer(
                caption,
                parse_mode="HTML",
                reply_markup=Keyboards.series_details(series),
            )
    else:
        # No poster available
        await callback.message.answer(
            caption,
            parse_mode="HTML",
            reply_markup=Keyboards.series_details(series),
        )


@router.callback_query(AddContentCB.filter(F.kind == "movie"))
async def handle_add_movie_from_trending(
    callback: CallbackQuery, callback_data: AddContentCB, db_user: User, db: Database
) -> None:
    """Add movie to Radarr from trending list."""
    await callback.answer()
    if not callback.message:
        return

    tmdb_id = callback_data.tmdb_id

    # Try to get movie from cache first
    movie = _cache_get(_trending_movies_cache, tmdb_id)

    if not movie:
        # If not in cache, fetch from Radarr
        radarr = await get_radarr()
        try:
            movie = await radarr.lookup_movie_by_tmdb(tmdb_id)
        except Exception as e:
            logger.error("Failed to lookup movie", tmdb_id=tmdb_id, error=str(e), exc_info=True)
            await callback.message.answer(
                Formatters.format_error("Не удалось найти фильм"),
                parse_mode="HTML",
            )
            return

    if not movie:
        await callback.message.answer("❌ Фильм не найден")
        return

    # Show loading message
    status_msg = await callback.message.answer("⏳ Добавляю фильм в Radarr...")

    try:
        # Get services
        prowlarr = await get_prowlarr()
        radarr = await get_radarr()
        sonarr = await get_sonarr()
        qbittorrent = await get_qbittorrent()
        add_service = AddService(prowlarr, radarr, sonarr, qbittorrent)

        # Get user preferences
        # PERF-07a: 2 independent RTTs → 1 wall-clock RTT.
        profiles, folders = await asyncio.gather(
            add_service.get_radarr_profiles(),
            add_service.get_radarr_root_folders(),
        )
        prefs = db_user.preferences

        if not profiles or not folders:
            await status_msg.edit_text("❌ Нет профилей качества или папок в Radarr")
            return

        profile_id = prefs.radarr_quality_profile_id or profiles[0].id
        folder_path = None
        if prefs.radarr_root_folder_id:
            folder = next((f for f in folders if f.id == prefs.radarr_root_folder_id), None)
            folder_path = folder.path if folder else folders[0].path
        else:
            folder_path = folders[0].path

        # Add movie to Radarr
        added_movie, action = await add_service.add_movie(
            movie=movie,
            quality_profile_id=profile_id,
            root_folder_path=folder_path,
            search_for_movie=True,
        )

        action.user_id = db_user.tg_id
        await db.log_action(action)

        if action.success and added_movie:
            await status_msg.edit_text(
                f"✅ <b>{html.escape(added_movie.title)}</b> ({added_movie.year})\n\n"
                f"Фильм добавлен в Radarr. Начат поиск релизов.",
                parse_mode="HTML",
            )
        else:
            # BUG-12b: action.error_message can contain raw *arr error text
            # (e.g. an unescaped "<" breaks HTML parsing under default parse_mode).
            error_msg = html.escape(action.error_message or "Неизвестная ошибка")
            await status_msg.edit_text(f"❌ Ошибка: {error_msg}")

    except Exception as e:
        logger.error("Failed to add movie from trending", tmdb_id=tmdb_id, error=str(e), exc_info=True)
        await status_msg.edit_text(
            Formatters.format_error("Не удалось добавить фильм"),
            parse_mode="HTML",
        )


@router.callback_query(AddContentCB.filter(F.kind == "series"))
async def handle_add_series_from_trending(
    callback: CallbackQuery, callback_data: AddContentCB, db_user: User, db: Database
) -> None:
    """Add series to Sonarr from trending list."""
    await callback.answer()
    if not callback.message:
        return

    tmdb_id = callback_data.tmdb_id

    # Try to get series from cache first
    series = _cache_get(_trending_series_cache, tmdb_id)

    if not series:
        if not callback.message:
            return
        await callback.message.answer(
            "❌ Сериал не найден в кэше.\n"
            "Попробуйте обновить список или используйте обычный поиск."
        )
        return

    if not callback.message:
        return

    # Show loading message
    status_msg = await callback.message.answer("⏳ Добавляю сериал в Sonarr...")

    try:
        # Get services
        prowlarr = await get_prowlarr()
        radarr = await get_radarr()
        sonarr = await get_sonarr()
        qbittorrent = await get_qbittorrent()
        add_service = AddService(prowlarr, radarr, sonarr, qbittorrent)

        # Get user preferences
        # PERF-07a: 2 independent RTTs → 1 wall-clock RTT.
        profiles, folders = await asyncio.gather(
            add_service.get_sonarr_profiles(),
            add_service.get_sonarr_root_folders(),
        )
        prefs = db_user.preferences

        if not profiles or not folders:
            await status_msg.edit_text("❌ Нет профилей качества или папок в Sonarr")
            return

        profile_id = prefs.sonarr_quality_profile_id or profiles[0].id
        folder_path = None
        if prefs.sonarr_root_folder_id:
            folder = next((f for f in folders if f.id == prefs.sonarr_root_folder_id), None)
            folder_path = folder.path if folder else folders[0].path
        else:
            folder_path = folders[0].path

        # Resolve TVDB ID if missing (TMDb trending returns tvdb_id=0)
        if not series.tvdb_id:
            # LOGIC-22: reuse the `sonarr` client already fetched above instead
            # of a redundant await get_sonarr().
            lookup_results = await sonarr.lookup_series(series.title)
            matched = None
            for lr in lookup_results:
                if lr.tmdb_id == series.tmdb_id:
                    matched = lr
                    break
            if not matched and lookup_results:
                matched = lookup_results[0]
            if matched and matched.tvdb_id:
                series = matched
                # PERF-07: write the resolved series (now carrying a real
                # tvdb_id) back into the cache so a subsequent add/detail view
                # reuses it instead of re-running the Sonarr lookup.
                async with _cache_lock:
                    _cache_put(_trending_series_cache, tmdb_id, series)
            else:
                await status_msg.edit_text(
                    "❌ Не удалось определить TVDB ID для сериала.\n"
                    "Попробуйте добавить через обычный поиск."
                )
                return

        # Add series to Sonarr
        added_series, action = await add_service.add_series(
            series=series,
            quality_profile_id=profile_id,
            root_folder_path=folder_path,
            monitor_type="all",
            search_for_missing=True,
        )

        action.user_id = db_user.tg_id
        await db.log_action(action)

        if action.success and added_series:
            year_str = f" ({added_series.year})" if added_series.year else ""
            await status_msg.edit_text(
                f"✅ <b>{html.escape(added_series.title)}</b>{year_str}\n\n"
                f"Сериал добавлен в Sonarr. Начат поиск релизов.",
                parse_mode="HTML",
            )
        else:
            # BUG-12b: escape *arr error text before interpolating into HTML.
            error_msg = html.escape(action.error_message or "Неизвестная ошибка")
            await status_msg.edit_text(f"❌ Ошибка: {error_msg}")

    except Exception as e:
        logger.error("Failed to add series from trending", tmdb_id=tmdb_id, error=str(e), exc_info=True)
        await status_msg.edit_text(
            Formatters.format_error("Не удалось добавить сериал"),
            parse_mode="HTML",
        )


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
