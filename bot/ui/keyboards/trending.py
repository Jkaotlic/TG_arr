"""Trending/popular-content keyboards (TMDB movies/series, Deezer artists).

``movie_details``/``series_details`` reuse ``_external_links`` (feature #5),
which lives on the search-domain mixin (bot/ui/keyboards/search.py) since it's
shared with ``release_details``. Calling ``_SearchKeyboards._external_links``
directly (rather than through the composed ``Keyboards`` class) avoids a
circular import between this module and the package ``__init__.py``.
"""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.models import MovieInfo, SeriesInfo
from bot.ui.callbacks import AddContentCB, TrendingItemCB
from bot.ui.keyboards._constants import CallbackData
from bot.ui.keyboards.search import _SearchKeyboards


class _TrendingKeyboards:
    """Trending/popular keyboard mixin."""

    @staticmethod
    def trending_menu(show_music: bool = False) -> InlineKeyboardMarkup:
        """Create trending/popular content selection menu."""
        rows = [
            [InlineKeyboardButton(text="🎬 Популярные фильмы", callback_data=CallbackData.TRENDING_MOVIES)],
            [InlineKeyboardButton(text="📺 Популярные сериалы", callback_data=CallbackData.TRENDING_SERIES)],
        ]
        if show_music:
            rows.append([
                InlineKeyboardButton(text="🎵 Популярные артисты", callback_data=CallbackData.TRENDING_MUSIC),
            ])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @staticmethod
    def trending_artists(artists: list[dict]) -> InlineKeyboardMarkup:
        """Create keyboard for trending artists (Deezer chart)."""
        keyboard = []
        for i, a in enumerate(artists[:10]):
            name = a.get("name", "Unknown")
            label = f"{i + 1}. {name}"
            if len(label) > 40:
                label = label[:37] + "..."
            keyboard.append([
                InlineKeyboardButton(
                    text=label,
                    callback_data=TrendingItemCB(kind="artist", item_id=str(i)).pack(),
                )
            ])
        keyboard.append([
            InlineKeyboardButton(text="◀️ Назад", callback_data=CallbackData.TRENDING_BACK),
        ])
        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def trending_movies(movies: list[MovieInfo]) -> InlineKeyboardMarkup:
        """Create keyboard for trending movies list."""
        keyboard = []

        for i, movie in enumerate(movies[:10], 1):
            title = f"{i}. {movie.title}"
            if movie.year:
                title += f" ({movie.year})"
            keyboard.append([
                InlineKeyboardButton(
                    text=title,
                    callback_data=TrendingItemCB(kind="movie", item_id=str(movie.tmdb_id)).pack(),
                )
            ])

        keyboard.append([
            InlineKeyboardButton(text="◀️ Назад", callback_data=CallbackData.TRENDING_BACK),
        ])

        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def trending_series(series_list: list[SeriesInfo]) -> InlineKeyboardMarkup:
        """Create keyboard for trending series list."""
        keyboard = []

        for i, series in enumerate(series_list[:10], 1):
            title = f"{i}. {series.title}"
            if series.year:
                title += f" ({series.year})"
            keyboard.append([
                InlineKeyboardButton(
                    text=title,
                    callback_data=TrendingItemCB(kind="series", item_id=str(series.tmdb_id)).pack(),
                )
            ])

        keyboard.append([
            InlineKeyboardButton(text="◀️ Назад", callback_data=CallbackData.TRENDING_BACK),
        ])

        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def movie_details(movie: MovieInfo) -> InlineKeyboardMarkup:
        """Create keyboard for movie details from trending."""
        keyboard = []
        links = _SearchKeyboards._external_links(movie)
        if links:
            keyboard.append(links)
        keyboard.append([
            InlineKeyboardButton(
                text="➕ Добавить в Radarr",
                callback_data=AddContentCB(kind="movie", tmdb_id=movie.tmdb_id).pack(),
            )
        ])
        keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data=CallbackData.TRENDING_MOVIES)])
        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def series_details(series: SeriesInfo) -> InlineKeyboardMarkup:
        """Create keyboard for series details from trending."""
        keyboard = []
        links = _SearchKeyboards._external_links(series)
        if links:
            keyboard.append(links)
        keyboard.append([
            InlineKeyboardButton(
                text="➕ Добавить в Sonarr",
                callback_data=AddContentCB(kind="series", tmdb_id=series.tmdb_id).pack(),
            )
        ])
        keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data=CallbackData.TRENDING_SERIES)])
        return InlineKeyboardMarkup(inline_keyboard=keyboard)
