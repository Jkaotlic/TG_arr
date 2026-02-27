"""Trending/popular content handlers."""

import structlog
from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from bot.config import get_settings
from bot.clients.registry import get_tmdb, get_radarr, get_sonarr, get_qbittorrent, get_prowlarr
from bot.db import Database
from bot.models import User
from bot.services.add_service import AddService
from bot.ui.formatters import Formatters
from bot.ui.keyboards import CallbackData, Keyboards

logger = structlog.get_logger()
router = Router()

# Menu button text
MENU_TRENDING = "🔥 Топ"

# Cache for trending data to avoid re-fetching when viewing details
_trending_movies_cache = {}
_trending_series_cache = {}


@router.message(F.text == MENU_TRENDING)
async def handle_trending_menu(message: Message) -> None:
    """Show trending/popular content selection menu."""
    settings = get_settings()

    if not settings.tmdb_enabled:
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
        reply_markup=Keyboards.trending_menu(),
    )


@router.callback_query(F.data == CallbackData.TRENDING_MOVIES)
async def handle_trending_movies(callback: CallbackQuery) -> None:
    """Show trending/popular movies."""
    await callback.answer()
    if not callback.message:
        return

    tmdb = get_tmdb()
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

        # Cache movies for detail views
        global _trending_movies_cache
        _trending_movies_cache = {movie.tmdb_id: movie for movie in movies}

        # Format and send results
        text = Formatters.format_trending_movies(movies[:10])  # Top 10
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=Keyboards.trending_movies(movies[:10]),
        )

    except Exception as e:
        logger.error("Failed to fetch trending movies", error=str(e))
        await callback.message.edit_text(
            f"❌ Ошибка при загрузке популярных фильмов:\n{str(e)}"
        )


@router.callback_query(F.data == CallbackData.TRENDING_SERIES)
async def handle_trending_series(callback: CallbackQuery) -> None:
    """Show trending/popular TV series."""
    await callback.answer()
    if not callback.message:
        return

    tmdb = get_tmdb()
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

        # Cache series for detail views
        global _trending_series_cache
        _trending_series_cache = {series.tmdb_id: series for series in series_list}

        # Format and send results
        text = Formatters.format_trending_series(series_list[:10])  # Top 10
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=Keyboards.trending_series(series_list[:10]),
        )

    except Exception as e:
        logger.error("Failed to fetch trending series", error=str(e))
        await callback.message.edit_text(
            f"❌ Ошибка при загрузке популярных сериалов:\n{str(e)}"
        )


@router.callback_query(F.data.startswith(CallbackData.TRENDING_MOVIE))
async def handle_movie_from_trending(callback: CallbackQuery) -> None:
    """Show movie details with poster when clicked from trending list."""
    await callback.answer()
    if not callback.message:
        return

    # Extract TMDB ID from callback data
    tmdb_id_str = callback.data.replace(CallbackData.TRENDING_MOVIE, "")
    try:
        tmdb_id = int(tmdb_id_str)
    except ValueError:
        await callback.message.answer("❌ Неверный ID фильма")
        return

    # Try to get movie from cache first
    movie = _trending_movies_cache.get(tmdb_id)

    if not movie:
        # If not in cache, fetch from Radarr
        radarr = get_radarr()
        try:
            movie = await radarr.lookup_movie_by_tmdb(tmdb_id)
        except Exception as e:
            logger.error("Failed to lookup movie", tmdb_id=tmdb_id, error=str(e))
            await callback.message.answer(f"❌ Ошибка: {str(e)}")
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
            logger.error("Failed to send poster", error=str(e))
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


@router.callback_query(F.data.startswith(CallbackData.TRENDING_SERIES_ITEM))
async def handle_series_from_trending(callback: CallbackQuery) -> None:
    """Show series details with poster when clicked from trending list."""
    await callback.answer()
    if not callback.message:
        return

    # Extract TMDB ID from callback data
    series_id_str = callback.data.replace(CallbackData.TRENDING_SERIES_ITEM, "")
    try:
        series_id = int(series_id_str)
    except ValueError:
        await callback.message.answer("❌ Неверный ID сериала")
        return

    # Try to get series from cache first (if from trending)
    series = _trending_series_cache.get(series_id)

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
            logger.error("Failed to send poster", error=str(e))
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


@router.callback_query(F.data.startswith(CallbackData.ADD_MOVIE))
async def handle_add_movie_from_trending(callback: CallbackQuery, db_user: User, db: Database) -> None:
    """Add movie to Radarr from trending list."""
    await callback.answer()
    if not callback.message:
        return

    # Extract TMDB ID from callback data
    tmdb_id_str = callback.data.replace(CallbackData.ADD_MOVIE, "")
    try:
        tmdb_id = int(tmdb_id_str)
    except ValueError:
        await callback.message.answer("❌ Неверный ID фильма")
        return

    # Try to get movie from cache first
    movie = _trending_movies_cache.get(tmdb_id)

    if not movie:
        # If not in cache, fetch from Radarr
        radarr = get_radarr()
        try:
            movie = await radarr.lookup_movie_by_tmdb(tmdb_id)
        except Exception as e:
            logger.error("Failed to lookup movie", tmdb_id=tmdb_id, error=str(e))
            await callback.message.answer(f"❌ Ошибка: {str(e)}")
            return

    if not movie:
        await callback.message.answer("❌ Фильм не найден")
        return

    # Show loading message
    status_msg = await callback.message.answer("⏳ Добавляю фильм в Radarr...")

    try:
        # Get services
        prowlarr = get_prowlarr()
        radarr = get_radarr()
        sonarr = get_sonarr()
        qbittorrent = get_qbittorrent()
        add_service = AddService(prowlarr, radarr, sonarr, qbittorrent)

        # Get user preferences
        prefs = db_user.preferences
        profiles = await add_service.get_radarr_profiles()
        folders = await add_service.get_radarr_root_folders()

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

        if action.success:
            await status_msg.edit_text(
                f"✅ <b>{added_movie.title}</b> ({added_movie.year})\n\n"
                f"Фильм добавлен в Radarr. Начат поиск релизов.",
                parse_mode="HTML",
            )
        else:
            error_msg = action.error_message or "Неизвестная ошибка"
            await status_msg.edit_text(f"❌ Ошибка: {error_msg}")

    except Exception as e:
        logger.error("Failed to add movie from trending", tmdb_id=tmdb_id, error=str(e))
        await status_msg.edit_text(f"❌ Ошибка при добавлении: {str(e)}")


@router.callback_query(F.data.startswith(CallbackData.ADD_SERIES))
async def handle_add_series_from_trending(callback: CallbackQuery, db_user: User, db: Database) -> None:
    """Add series to Sonarr from trending list."""
    await callback.answer()
    if not callback.message:
        return

    # Extract TMDB ID from callback data
    tmdb_id_str = callback.data.replace(CallbackData.ADD_SERIES, "")
    try:
        tmdb_id = int(tmdb_id_str)
    except ValueError:
        await callback.message.answer("❌ Неверный ID сериала")
        return

    # Try to get series from cache first
    series = _trending_series_cache.get(tmdb_id)

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
        prowlarr = get_prowlarr()
        radarr = get_radarr()
        sonarr = get_sonarr()
        qbittorrent = get_qbittorrent()
        add_service = AddService(prowlarr, radarr, sonarr, qbittorrent)

        # Get user preferences
        prefs = db_user.preferences
        profiles = await add_service.get_sonarr_profiles()
        folders = await add_service.get_sonarr_root_folders()

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
            sonarr_client = get_sonarr()
            lookup_results = await sonarr_client.lookup_series(series.title)
            matched = None
            for lr in lookup_results:
                if lr.tmdb_id == series.tmdb_id:
                    matched = lr
                    break
            if not matched and lookup_results:
                matched = lookup_results[0]
            if matched and matched.tvdb_id:
                series = matched
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

        if action.success:
            year_str = f" ({added_series.year})" if added_series.year else ""
            await status_msg.edit_text(
                f"✅ <b>{added_series.title}</b>{year_str}\n\n"
                f"Сериал добавлен в Sonarr. Начат поиск релизов.",
                parse_mode="HTML",
            )
        else:
            error_msg = action.error_message or "Неизвестная ошибка"
            await status_msg.edit_text(f"❌ Ошибка: {error_msg}")

    except Exception as e:
        logger.error("Failed to add series from trending", tmdb_id=tmdb_id, error=str(e))
        await status_msg.edit_text(f"❌ Ошибка при добавлении: {str(e)}")
