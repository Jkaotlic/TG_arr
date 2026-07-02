"""Search-flow keyboards: content-type picker, paginated results, release
details/actions, and the season-monitoring preset picker.

Also hosts ``_external_links`` (feature #5), a helper that builds a row of
external-metadata URL buttons (TMDB/IMDb/TVDB). It's used both here (by
``release_details``) and by the trending-domain ``movie_details``/
``series_details`` (bot/ui/keyboards/trending.py) — kept here since it's the
release/content-details concept, and reached from the composed ``Keyboards``
class via normal attribute lookup either way.
"""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.models import ContentType, SearchResult, SeriesInfo
from bot.ui.callbacks import PageCB, ReleaseCB, SeasonPresetCB
from bot.ui.keyboards._constants import CallbackData


class _SearchKeyboards:
    """Search-result / release-details keyboard mixin."""

    @staticmethod
    def content_type_selection(show_music: bool = False) -> InlineKeyboardMarkup:
        """Create keyboard for selecting content type (movie/series/music)."""
        first_row = [
            InlineKeyboardButton(text="🎬 Фильм", callback_data=CallbackData.TYPE_MOVIE),
            InlineKeyboardButton(text="📺 Сериал", callback_data=CallbackData.TYPE_SERIES),
        ]
        rows = [first_row]
        if show_music:
            rows.append([InlineKeyboardButton(text="🎵 Музыка", callback_data=CallbackData.TYPE_MUSIC)])
        rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data=CallbackData.CANCEL)])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @staticmethod
    def search_results(
        results: list[SearchResult],
        current_page: int,
        total_pages: int,
        per_page: int = 5,
        show_grab_best: bool = False,
        best_score: int = 0,
    ) -> InlineKeyboardMarkup:
        """
        Create keyboard for search results with pagination.

        Args:
            results: List of results for current page
            current_page: Current page number (0-indexed)
            total_pages: Total number of pages
            per_page: Results per page
            show_grab_best: Whether to show "Grab Best" button
            best_score: Score of the best result
        """
        keyboard = []

        # Result buttons (numbered)
        start_idx = current_page * per_page
        for i, result in enumerate(results):
            idx = start_idx + i
            # Show basic info in button
            quality = result.quality.resolution or "?"
            seeders = f"S:{result.seeders}" if result.seeders else ""
            size = result.size_formatted[:6] if result.size > 0 else ""

            label = f"{idx + 1}. {quality} {seeders} {size}".strip()
            if len(label) > 40:
                label = label[:37] + "..."

            keyboard.append([
                InlineKeyboardButton(
                    text=label,
                    callback_data=ReleaseCB(idx=idx).pack(),
                )
            ])

        # Grab best button
        if show_grab_best and results:
            keyboard.append([
                InlineKeyboardButton(
                    text=f"⚡ Лучший (оценка: {best_score})",
                    callback_data=CallbackData.GRAB_BEST,
                )
            ])

        # Pagination row — #1: typed PageCB(scope="search") instead of "page:" string
        nav_buttons = []
        if current_page > 0:
            nav_buttons.append(
                InlineKeyboardButton(text="◀️", callback_data=PageCB(scope="search", page=current_page - 1).pack())
            )

        nav_buttons.append(
            InlineKeyboardButton(text=f"{current_page + 1}/{total_pages}", callback_data="noop")
        )

        if current_page < total_pages - 1:
            nav_buttons.append(
                InlineKeyboardButton(text="▶️", callback_data=PageCB(scope="search", page=current_page + 1).pack())
            )

        if nav_buttons:
            keyboard.append(nav_buttons)

        # Cancel button
        keyboard.append([
            InlineKeyboardButton(text="❌ Отмена", callback_data=CallbackData.CANCEL),
        ])

        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def release_details(
        result: SearchResult,
        content_type: ContentType,
        can_grab: bool = True,
        show_force_grab: bool = False,
        content: object = None,
    ) -> InlineKeyboardMarkup:
        """Create keyboard for release details view.

        When the resolved movie/series (``content``) is available, prepend a row
        of external-metadata link buttons (feature #5).
        """
        keyboard = []

        if content is not None:
            links = _SearchKeyboards._external_links(content)
            if links:
                keyboard.append(links)

        if can_grab:
            keyboard.append([
                InlineKeyboardButton(text="✅ Скачать", callback_data=CallbackData.CONFIRM_GRAB),
            ])

        if show_force_grab:
            keyboard.append([
                InlineKeyboardButton(text="⚡ Принудительно (qBit)", callback_data=CallbackData.FORCE_GRAB),
            ])

        # #2: let the user choose which seasons Sonarr monitors (series only).
        if content_type == ContentType.SERIES:
            keyboard.append([
                InlineKeyboardButton(text="📺 Мониторинг сезонов", callback_data=CallbackData.SEASON_MENU),
            ])

        keyboard.append([
            InlineKeyboardButton(text="◀️ Назад", callback_data=CallbackData.BACK),
            InlineKeyboardButton(text="❌ Отмена", callback_data=CallbackData.CANCEL),
        ])

        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def season_presets() -> InlineKeyboardMarkup:
        """Feature #2: Sonarr season-monitoring preset picker.

        BUG-16: "Назад" uses the dedicated SEASON_BACK callback, not the
        generic BACK — the latter clears the release selection and returns to
        the results list, which is not what a user expects from a submenu.
        """
        rows = [
            [InlineKeyboardButton(text="📺 Все сезоны", callback_data=SeasonPresetCB(preset="all").pack())],
            [InlineKeyboardButton(text="🔮 Только будущие", callback_data=SeasonPresetCB(preset="future").pack())],
            [InlineKeyboardButton(text="1️⃣ Первый сезон", callback_data=SeasonPresetCB(preset="firstSeason").pack())],
            [InlineKeyboardButton(text="🔚 Последний сезон", callback_data=SeasonPresetCB(preset="latestSeason").pack())],
            [InlineKeyboardButton(text="◀️ Назад", callback_data=CallbackData.SEASON_BACK)],
        ]
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @staticmethod
    def _external_links(content: object) -> list[InlineKeyboardButton]:
        """Feature #5: URL buttons opening the title in external metadata sites.

        Zero-backend (Telegram URL buttons). TVDB is series-only; TMDB uses the
        tv/movie path depending on the content model.
        """
        buttons: list[InlineKeyboardButton] = []
        is_series = isinstance(content, SeriesInfo)
        tmdb_id = getattr(content, "tmdb_id", None)
        imdb_id = getattr(content, "imdb_id", None)
        tvdb_id = getattr(content, "tvdb_id", None)
        if tmdb_id:
            kind = "tv" if is_series else "movie"
            buttons.append(InlineKeyboardButton(text="🎬 TMDB", url=f"https://www.themoviedb.org/{kind}/{tmdb_id}"))
        if imdb_id:
            buttons.append(InlineKeyboardButton(text="🎞 IMDb", url=f"https://www.imdb.com/title/{imdb_id}/"))
        if is_series and tvdb_id:
            buttons.append(InlineKeyboardButton(text="📺 TVDB", url=f"https://thetvdb.com/dereferrer/series/{tvdb_id}"))
        return buttons
