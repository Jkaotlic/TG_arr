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

from bot.models import ContentType, MovieInfo, SearchResult, SeriesInfo
from bot.ui.callbacks import (
    AutoSearchCB,
    FindGrabCB,
    FindPageCB,
    FindReleaseCB,
    ForceGrabCB,
    GrabBestCB,
    GrabCB,
    PageCB,
    ReleaseCB,
    SeasonPresetCB,
)
from bot.ui.formatters._common import release_button_label
from bot.ui.keyboards._constants import BTN_BACK, BTN_CLOSE, CallbackData


class _SearchKeyboards:
    """Search-result / release-details keyboard mixin."""

    @staticmethod
    def content_type_selection(show_music: bool = False) -> InlineKeyboardMarkup:
        """Create keyboard for selecting content type (movie/series/anime/music)."""
        first_row = [
            InlineKeyboardButton(text="🎬 Фильм", callback_data=CallbackData.TYPE_MOVIE),
            InlineKeyboardButton(text="📺 Сериал", callback_data=CallbackData.TYPE_SERIES),
        ]
        rows = [first_row, [
            InlineKeyboardButton(text="🎌 Аниме", callback_data=CallbackData.TYPE_ANIME),
        ]]
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
        list_token: str = "",
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
            list_token: token of this result list (bot/ui/list_token.py)
        """
        keyboard = []

        # Result buttons (numbered)
        start_idx = current_page * per_page
        for i, result in enumerate(results):
            idx = start_idx + i
            # Audit GRAB-10/11: full size with unit, source, languages, ⛔.
            label = release_button_label(result, idx)

            keyboard.append([
                InlineKeyboardButton(
                    text=label,
                    callback_data=ReleaseCB(tok=list_token, idx=idx).pack(),
                )
            ])

        # Grab best button
        if show_grab_best and results:
            keyboard.append([
                InlineKeyboardButton(
                    text=f"⚡ Лучший (оценка: {best_score})",
                    callback_data=GrabBestCB(tok=list_token).pack(),
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
        list_token: str = "",
        idx: int = 0,
        season_menu: bool = True,
    ) -> InlineKeyboardMarkup:
        """Create keyboard for release details view.

        ``season_menu=False`` hides the season-monitoring picker: the series
        was already in the library before this search, and its monitoring is
        not the grab's to rewrite (audit GRAB-05).

        When the resolved movie/series (``content``) is available, prepend a row
        of external-metadata link buttons (feature #5).

        ``list_token``/``idx`` name the list and the release this card shows,
        so its grab buttons act on exactly that release (audit GRAB-03).
        """
        keyboard = []

        if content is not None:
            links = _SearchKeyboards._external_links(content)
            if links:
                keyboard.append(links)

        if can_grab:
            keyboard.append([
                InlineKeyboardButton(
                    text="✅ Скачать", callback_data=GrabCB(tok=list_token, idx=idx).pack(),
                ),
            ])

        if show_force_grab:
            keyboard.append([
                InlineKeyboardButton(
                    text="⚡ Принудительно (qBit)",
                    callback_data=ForceGrabCB(tok=list_token, idx=idx).pack(),
                ),
            ])

        # #2: let the user choose which seasons are monitored (series only).
        if content_type == ContentType.SERIES and season_menu:
            keyboard.append([
                InlineKeyboardButton(text="📺 Мониторинг сезонов", callback_data=CallbackData.SEASON_MENU),
            ])

        keyboard.append([
            InlineKeyboardButton(text="◀️ Назад", callback_data=CallbackData.BACK),
            InlineKeyboardButton(text="❌ Отмена", callback_data=CallbackData.CANCEL),
        ])

        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def rejected_confirm(list_token: str, idx: int) -> InlineKeyboardMarkup:
        """Audit 2026-09-23 GRAB-02: a release *arr rejected is taken only
        after an explicit second tap."""
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="⚠️ Всё равно скачать",
                callback_data=GrabCB(tok=list_token, idx=idx, ok=1).pack(),
            )],
            [InlineKeyboardButton(
                text="◀️ Назад", callback_data=ReleaseCB(tok=list_token, idx=idx).pack(),
            )],
        ])

    @staticmethod
    def grab_failed(
        list_token: str, idx: int, arr_name: str, offer_auto_search: bool,
    ) -> InlineKeyboardMarkup:
        """After a grab that did not happen: *arr's own search as an explicit,
        labelled choice (GRAB-08), and the way back to the release card."""
        rows = []
        if offer_auto_search:
            rows.append([InlineKeyboardButton(
                text=f"🔍 Пусть {arr_name} ищет сам",
                callback_data=AutoSearchCB(tok=list_token, idx=idx).pack(),
            )])
        rows.append([
            InlineKeyboardButton(
                text="◀️ Назад", callback_data=ReleaseCB(tok=list_token, idx=idx).pack(),
            ),
            InlineKeyboardButton(text="❌ Отмена", callback_data=CallbackData.CANCEL),
        ])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @staticmethod
    def free_grab_failed(list_token: str, idx: int, offer_qbit: bool, page: int = 0) -> InlineKeyboardMarkup:
        """Free search after *arr refused/never saw the push: qBittorrent as an
        explicit choice, never a silent fallback (GRAB-08)."""
        rows = []
        if offer_qbit:
            rows.append([InlineKeyboardButton(
                text="⚡ Скачать напрямую (qBit)",
                callback_data=FindGrabCB(tok=list_token, idx=idx, q=1).pack(),
            )])
        rows.append([
            InlineKeyboardButton(text=BTN_BACK, callback_data=FindPageCB(page=page).pack()),
            InlineKeyboardButton(text="❌ Отмена", callback_data=CallbackData.CANCEL),
        ])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @staticmethod
    def free_search_offer() -> InlineKeyboardMarkup:
        """Offered when the catalogue-backed search has nothing to show.

        The catalogue can only answer for titles TMDb/TVDB carry; a dead end
        there is not a dead end for the indexers, and this button is the only
        place the user learns that.
        """
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔎 Искать по названию", callback_data=CallbackData.FREE_SEARCH)],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=CallbackData.CANCEL)],
        ])

    @staticmethod
    def free_results(
        results: list[SearchResult], page: int, total_pages: int, offset: int = 0,
        list_token: str = "",
    ) -> InlineKeyboardMarkup:
        """Free-search result list — same shape as `search_results`, different
        callbacks.

        No "⚡ Лучший": that button auto-grabs on a score gated against
        `auto_grab_score_threshold`, and a free-text hit never went through
        *arr's profile at all, so there is no verdict to auto-act on.
        """
        keyboard = []
        for i, result in enumerate(results):
            idx = offset + i
            label = release_button_label(result, idx)
            keyboard.append([
                InlineKeyboardButton(text=label, callback_data=FindReleaseCB(tok=list_token, idx=idx).pack()),
            ])

        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="◀️", callback_data=FindPageCB(page=page - 1).pack()))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(text="▶️", callback_data=FindPageCB(page=page + 1).pack()))
        keyboard.append(nav)

        keyboard.append([InlineKeyboardButton(text="❌ Отмена", callback_data=CallbackData.CANCEL)])
        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def free_release(idx: int, list_token: str = "", page: int = 0) -> InlineKeyboardMarkup:
        """One free-search hit. No season-monitoring button: there is no series
        in Sonarr to monitor. ``page`` — the list page the hit was opened from
        (audit UI-09: "Назад" used to jump to page 1)."""
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Скачать", callback_data=FindGrabCB(tok=list_token, idx=idx).pack())],
            [
                InlineKeyboardButton(text=BTN_BACK, callback_data=FindPageCB(page=page).pack()),
                InlineKeyboardButton(text="❌ Отмена", callback_data=CallbackData.CANCEL),
            ],
        ])

    @staticmethod
    def season_presets() -> InlineKeyboardMarkup:
        """Feature #2: season-monitoring preset picker.

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

    @staticmethod
    def title_candidates(candidates: list) -> InlineKeyboardMarkup:
        """Ask which title the user meant (see `needs_title_confirmation`)."""
        from bot.ui.callbacks import TitleCB

        keyboard = []
        for idx, candidate in enumerate(candidates[:5]):
            year = f" ({candidate.year})" if getattr(candidate, "year", None) else ""
            label = f"{idx + 1}. {candidate.title}{year}"
            if len(label) > 60:
                label = label[:57] + "..."
            keyboard.append([
                InlineKeyboardButton(text=label, callback_data=TitleCB(idx=idx).pack())
            ])

        keyboard.append([
            InlineKeyboardButton(text="❌ Отмена", callback_data=CallbackData.CANCEL),
        ])
        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def _title_action_ref(title) -> tuple[str, str]:
        """(kind, resource_id) for `TitleActionCB` — "movie"/"series" and the
        Radarr/Sonarr id as a string. Two separate fields, not one
        colon-joined "movie:15" — see `TitleActionCB`'s docstring for why
        (aiogram's CallbackData uses ':' as its own field separator).
        """
        if isinstance(title, MovieInfo):
            return "movie", str(title.radarr_id)
        return "series", str(title.sonarr_id)

    @staticmethod
    def title_actions(title) -> InlineKeyboardMarkup:
        """Manage a library title — monitoring and removal (2026-07-29).

        The monitoring button is a toggle: it shows the action, not the state,
        so the user never has to work out what pressing it will do.
        """
        from bot.ui.callbacks import TitleActionCB

        kind, title_id = _SearchKeyboards._title_action_ref(title)
        monitored = bool(getattr(title, "monitored", False))
        toggle = (
            InlineKeyboardButton(
                text="🔕 Снять мониторинг",
                callback_data=TitleActionCB(action="unmon", kind=kind, title_id=title_id).pack(),
            )
            if monitored
            else InlineKeyboardButton(
                text="🔔 Включить мониторинг",
                callback_data=TitleActionCB(action="mon", kind=kind, title_id=title_id).pack(),
            )
        )
        return InlineKeyboardMarkup(inline_keyboard=[
            [toggle],
            [InlineKeyboardButton(
                text="🗑 Удалить из каталога",
                callback_data=TitleActionCB(action="delete", kind=kind, title_id=title_id).pack(),
            )],
            [InlineKeyboardButton(text=BTN_CLOSE, callback_data=CallbackData.CLOSE)],
        ])

    @staticmethod
    def title_choices(titles: list, limit: int = 5) -> InlineKeyboardMarkup:
        """Ask which library entry was meant (audit 2026-07-30, BUG-05).

        `/title Frozen` matches Frozen, Frozen Fever and Frozen II. Picking the
        first silently is how "Холодное сердце" once became "Heart of Stone" —
        and here the card that follows carries a delete button, so the guess is
        far more expensive.
        """
        from bot.ui.callbacks import TitleActionCB

        rows = []
        for title in titles[:limit]:
            year = getattr(title, "year", None)
            label = f"{getattr(title, 'title', '?')}{f' ({year})' if year else ''}"
            kind, title_id = _SearchKeyboards._title_action_ref(title)
            rows.append([InlineKeyboardButton(
                text=label[:60],
                callback_data=TitleActionCB(action="pick", kind=kind, title_id=title_id).pack(),
            )])
        rows.append([InlineKeyboardButton(text=BTN_CLOSE, callback_data=CallbackData.CLOSE)])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @staticmethod
    def confirm_title_delete(kind: str, title_id: str) -> InlineKeyboardMarkup:
        """Second step of a destructive action — never one tap away."""
        from bot.ui.callbacks import TitleActionCB

        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="🗑 Да, удалить",
                callback_data=TitleActionCB(action="delconf", kind=kind, title_id=title_id).pack(),
            )],
            # Audit UI-07: "not now" returns to the title card, it does not
            # abort a search (there is none here).
            [InlineKeyboardButton(
                text=BTN_BACK,
                callback_data=TitleActionCB(action="pick", kind=kind, title_id=title_id).pack(),
            )],
        ])

    #: Season buttons per picker page (audit UI-17: the rest used to be cut).
    SEASONS_PER_PAGE = 12

    @staticmethod
    def season_scope(
        seasons: list, title_id: str, per_row: int = 4, page: int = 0,
    ) -> InlineKeyboardMarkup:
        """Pick what to fetch for a series: the whole show or one season.

        Searching a 20-season show as one query returns season packs the user
        may not want, and burns indexer quota on episodes they already have.
        Newest seasons first, 12 per page; older ones are a ▶️ away (audit
        UI-17: they used to be unreachable).
        """
        from bot.ui.callbacks import SeasonPageCB, SeasonScopeCB

        rows = [[InlineKeyboardButton(
            text="📺 Весь сериал",
            callback_data=SeasonScopeCB(season=0, title_id=title_id).pack(),
        )]]

        per_page = _SearchKeyboards.SEASONS_PER_PAGE
        ordered = sorted(seasons, reverse=True)
        total_pages = max(1, (len(ordered) + per_page - 1) // per_page)
        page = max(0, min(page, total_pages - 1))
        shown = ordered[page * per_page:(page + 1) * per_page]
        for i in range(0, len(shown), per_row):
            rows.append([
                InlineKeyboardButton(
                    text=f"S{season:02d}",
                    callback_data=SeasonScopeCB(season=season, title_id=title_id).pack(),
                )
                for season in shown[i:i + per_row]
            ])

        if total_pages > 1:
            nav = []
            if page > 0:
                nav.append(InlineKeyboardButton(
                    text="◀️", callback_data=SeasonPageCB(page=page - 1, title_id=title_id).pack(),
                ))
            nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
            if page < total_pages - 1:
                nav.append(InlineKeyboardButton(
                    text="▶️", callback_data=SeasonPageCB(page=page + 1, title_id=title_id).pack(),
                ))
            rows.append(nav)

        rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data=CallbackData.CANCEL)])
        return InlineKeyboardMarkup(inline_keyboard=rows)
