"""Trending/popular content handlers."""

import structlog
from aiogram import F, Router
from aiogram.types import CallbackQuery, Message, BufferedInputFile, FSInputFile, URLInputFile

from bot.config import get_settings
from bot.clients.registry import get_tmdb, get_radarr, get_sonarr
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


@router.callback_query(F.data.startswith(CallbackData.MOVIE))
async def handle_movie_from_trending(callback: CallbackQuery) -> None:
    """Show movie details with poster when clicked from trending list."""
    await callback.answer()

    # Extract TMDB ID from callback data
    tmdb_id_str = callback.data.replace(CallbackData.MOVIE, "")
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
                photo=URLInputFile(movie.poster_url),
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


@router.callback_query(F.data.startswith(CallbackData.SERIES))
async def handle_series_from_trending(callback: CallbackQuery) -> None:
    """Show series details with poster when clicked from trending list."""
    await callback.answer()

    # Extract TMDB ID from callback data (or TVDB ID for regular series search)
    series_id_str = callback.data.replace(CallbackData.SERIES, "")
    try:
        series_id = int(series_id_str)
    except ValueError:
        await callback.message.answer("❌ Неверный ID сериала")
        return

    # Try to get series from cache first (if from trending)
    series = _trending_series_cache.get(series_id)

    if not series:
        # If not in cache, need to determine if it's TMDB or TVDB ID
        # For now, assume it's from regular search (TVDB ID)
        sonarr = get_sonarr()
        try:
            series = await sonarr.lookup_series_by_tvdb(series_id)
        except Exception as e:
            logger.error("Failed to lookup series", series_id=series_id, error=str(e))
            await callback.message.answer(f"❌ Ошибка: {str(e)}")
            return

    if not series:
        await callback.message.answer("❌ Сериал не найден")
        return

    # Send poster with series details
    caption = Formatters.format_series_with_poster(series)

    if series.poster_url:
        try:
            await callback.message.answer_photo(
                photo=URLInputFile(series.poster_url),
                caption=caption,
                parse_mode="HTML",
                reply_markup=Keyboards.series_selection(series),
            )
        except Exception as e:
            logger.error("Failed to send poster", error=str(e))
            # Fallback to text only
            await callback.message.answer(
                caption,
                parse_mode="HTML",
                reply_markup=Keyboards.series_selection(series),
            )
    else:
        # No poster available
        await callback.message.answer(
            caption,
            parse_mode="HTML",
            reply_markup=Keyboards.series_selection(series),
        )
