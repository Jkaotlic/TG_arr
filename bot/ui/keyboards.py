"""Inline keyboard builders for Telegram bot."""


from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from bot.ui.callbacks import PageCB, TorrentPageCB
from bot.ui.menu import (
    MENU_CALENDAR,
    MENU_DOWNLOADS,
    MENU_EMBY,
    MENU_HISTORY,
    MENU_MUSIC,
    MENU_QSTATUS,
    MENU_SEARCH,
    MENU_SETTINGS,
    MENU_STATUS,
    MENU_TRENDING,
)
from bot.models import (
    ArtistInfo,
    ContentType,
    MetadataProfile,
    MovieInfo,
    QualityProfile,
    RootFolder,
    SearchResult,
    SeriesInfo,
    TorrentFilter,
    TorrentInfo,
)


class CallbackData:
    """Callback data prefixes for parsing."""

    # Content type selection
    TYPE_MOVIE = "type:movie"
    TYPE_SERIES = "type:series"
    TYPE_MUSIC = "type:music"

    # Pagination
    # #1: search pagination migrated to the typed PageCB factory
    # (bot/ui/callbacks.py) — the old "page:" string prefix collided with music's
    # "art_page:" (LOGIC-14). Music pagination below is not yet migrated.
    ARTIST_PAGE = "art_page:"  # art_page:N
    MUSIC_BACK = "music_back"  # LOGIC-24: music-aware back button
    BACK = "back"
    CANCEL = "cancel"

    # Release selection
    RELEASE = "rel:"  # rel:0 (index in results)
    GRAB_BEST = "grab_best"

    # Content selection (for add)
    ARTIST = "artist:"  # artist:idx (session results index)
    ADD_MOVIE = "add_movie:"  # add_movie:tmdb_id
    ADD_SERIES = "add_series:"  # add_series:tmdb_id

    # Confirmation
    CONFIRM_GRAB = "confirm_grab"
    FORCE_GRAB = "force_grab"  # Force download via qBittorrent

    # Season monitoring (#2)
    SEASON_MENU = "season_menu"     # open the season-monitoring preset picker
    SEASON_PRESET = "season_set:"   # season_set:all / future / firstSeason / latestSeason
    # BUG-16: dedicated back button from the season-preset picker — re-renders
    # the release card (like handle_season_preset does) instead of falling
    # through to the generic CallbackData.BACK, which clears selected_result/
    # selected_content and returns to the results list.
    SEASON_BACK = "season_back"

    # Settings
    SETTINGS = "settings"
    SET_RADARR_PROFILE = "set:rp:"  # set:rp:1
    SET_RADARR_FOLDER = "set:rf:"  # set:rf:1
    SET_SONARR_PROFILE = "set:sp:"  # set:sp:1
    SET_SONARR_FOLDER = "set:sf:"  # set:sf:1
    SET_LIDARR_PROFILE = "set:lp:"  # set:lp:1
    SET_LIDARR_META = "set:lm:"  # set:lm:1 (Lidarr metadata profile)
    SET_LIDARR_FOLDER = "set:lf:"  # set:lf:1
    SET_RESOLUTION = "set:res:"  # set:res:1080p
    SET_AUTO_GRAB = "set:ag:"  # set:ag:1 or set:ag:0

    # qBittorrent / Downloads
    TORRENT = "t:"  # t:hash - select torrent
    TORRENT_PAUSE = "t_pause:"  # t_pause:hash
    TORRENT_RESUME = "t_resume:"  # t_resume:hash
    TORRENT_DELETE = "t_delete:"  # t_delete:hash
    TORRENT_DELETE_FILES = "t_delf:"  # t_delf:hash (delete with files) — shows confirm
    TORRENT_DELETE_FILES_CONFIRM = "t_delfc:"  # t_delfc:hash — confirmed, actually deletes
    TORRENT_REFRESH = "t_refresh"  # Refresh torrent list
    TORRENT_FILTER = "t_filter:"  # t_filter:downloading
    TORRENT_PAGE = "t_page:"  # t_page:2
    TORRENT_BACK = "t_back"  # Back to torrent list
    TORRENT_PAUSE_ALL = "t_pause_all"
    TORRENT_RESUME_ALL = "t_resume_all"
    TORRENT_CLOSE = "t_close"  # Close torrent list message
    SPEED_LIMIT = "speed:"  # speed:1024 (KB/s)
    SPEED_MENU = "speed_menu"

    # Emby
    EMBY_REFRESH = "emby_refresh"  # Refresh status
    EMBY_SCAN_ALL = "emby_scan_all"  # Scan all libraries
    EMBY_SCAN_MOVIES = "emby_scan_movies"  # Scan movies library
    EMBY_SCAN_SERIES = "emby_scan_series"  # Scan series library
    EMBY_RESTART = "emby_restart"  # Restart server
    EMBY_RESTART_CONFIRM = "emby_restart_confirm"  # Confirm restart
    EMBY_UPDATE = "emby_update"  # Check/install update
    EMBY_UPDATE_CONFIRM = "emby_update_confirm"  # Confirm update
    EMBY_CLOSE = "emby_close"  # Close Emby message

    # Trending/Popular
    TRENDING_MOVIES = "trending_movies"  # Show trending movies
    TRENDING_SERIES = "trending_series"  # Show trending series
    TRENDING_MUSIC = "trending_music"  # Show trending music (artists)
    TRENDING_MOVIE = "trend_m:"  # trend_m:tmdb_id - view movie details
    TRENDING_SERIES_ITEM = "trend_s:"  # trend_s:tmdb_id - view series details
    TRENDING_ARTIST = "trend_a:"  # trend_a:idx - view trending artist from Deezer list
    TRENDING_BACK = "trending_back"  # BUG-01: back to trending menu (not search's BACK)

    # Calendar
    CALENDAR_7 = "cal_7"  # 7 days
    CALENDAR_14 = "cal_14"  # 14 days
    CALENDAR_30 = "cal_30"  # 30 days
    CALENDAR_REFRESH = "cal_refresh"  # Refresh current view


class Keyboards:
    """Inline keyboard builders."""

    @staticmethod
    def main_menu() -> ReplyKeyboardMarkup:
        """Create main (reply) menu keyboard with the most used commands."""
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text=MENU_SEARCH), KeyboardButton(text=MENU_MUSIC), KeyboardButton(text=MENU_TRENDING)],
                [KeyboardButton(text=MENU_CALENDAR), KeyboardButton(text=MENU_DOWNLOADS), KeyboardButton(text=MENU_QSTATUS)],
                [KeyboardButton(text=MENU_EMBY), KeyboardButton(text=MENU_STATUS), KeyboardButton(text=MENU_SETTINGS)],
                [KeyboardButton(text=MENU_HISTORY)],
            ],
            resize_keyboard=True,
            input_field_placeholder="Введите название для поиска...",
        )

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
                    callback_data=f"{CallbackData.RELEASE}{idx}",
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
            links = Keyboards._external_links(content)
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
            [InlineKeyboardButton(text="📺 Все сезоны", callback_data=f"{CallbackData.SEASON_PRESET}all")],
            [InlineKeyboardButton(text="🔮 Только будущие", callback_data=f"{CallbackData.SEASON_PRESET}future")],
            [InlineKeyboardButton(text="1️⃣ Первый сезон", callback_data=f"{CallbackData.SEASON_PRESET}firstSeason")],
            [InlineKeyboardButton(text="🔚 Последний сезон", callback_data=f"{CallbackData.SEASON_PRESET}latestSeason")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data=CallbackData.SEASON_BACK)],
        ]
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @staticmethod
    def quality_profiles(profiles: list[QualityProfile], prefix: str) -> InlineKeyboardMarkup:
        """Create keyboard for selecting quality profile."""
        keyboard = []

        for profile in profiles:
            keyboard.append([
                InlineKeyboardButton(
                    text=profile.name,
                    callback_data=f"{prefix}{profile.id}",
                )
            ])

        keyboard.append([
            InlineKeyboardButton(text="◀️ Назад", callback_data=CallbackData.SETTINGS),
        ])

        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def root_folders(folders: list[RootFolder], prefix: str) -> InlineKeyboardMarkup:
        """Create keyboard for selecting root folder."""
        keyboard = []

        for folder in folders:
            label = f"{folder.path} ({folder.free_space_formatted})"
            if len(label) > 40:
                label = folder.path[:37] + "..."

            keyboard.append([
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"{prefix}{folder.id}",
                )
            ])

        keyboard.append([
            InlineKeyboardButton(text="◀️ Назад", callback_data=CallbackData.SETTINGS),
        ])

        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def settings_menu(lidarr_enabled: bool = False) -> InlineKeyboardMarkup:
        """Create main settings menu keyboard."""
        rows = [
            [
                InlineKeyboardButton(text="🎬 Профиль Radarr", callback_data="settings:radarr_profile"),
                InlineKeyboardButton(text="📁 Папка Radarr", callback_data="settings:radarr_folder"),
            ],
            [
                InlineKeyboardButton(text="📺 Профиль Sonarr", callback_data="settings:sonarr_profile"),
                InlineKeyboardButton(text="📁 Папка Sonarr", callback_data="settings:sonarr_folder"),
            ],
        ]
        if lidarr_enabled:
            rows.append([
                InlineKeyboardButton(text="🎵 Профиль Lidarr", callback_data="settings:lidarr_profile"),
                InlineKeyboardButton(text="📁 Папка Lidarr", callback_data="settings:lidarr_folder"),
            ])
            rows.append([
                InlineKeyboardButton(text="🎧 Lidarr metadata", callback_data="settings:lidarr_meta"),
            ])
        rows.append([InlineKeyboardButton(text="🎯 Качество", callback_data="settings:resolution")])
        rows.append([InlineKeyboardButton(text="⚡ Авто-граб", callback_data="settings:auto_grab")])
        rows.append([InlineKeyboardButton(text="❌ Закрыть", callback_data=CallbackData.CANCEL)])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @staticmethod
    def metadata_profiles(profiles: list[MetadataProfile], prefix: str) -> InlineKeyboardMarkup:
        """Create keyboard for selecting Lidarr metadata profile."""
        keyboard = []
        for profile in profiles:
            keyboard.append([
                InlineKeyboardButton(
                    text=profile.name,
                    callback_data=f"{prefix}{profile.id}",
                )
            ])
        keyboard.append([
            InlineKeyboardButton(text="◀️ Назад", callback_data=CallbackData.SETTINGS),
        ])
        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def artist_list(
        artists: list[ArtistInfo],
        current_page: int = 0,
        per_page: int = 5,
    ) -> InlineKeyboardMarkup:
        """Create keyboard for artist selection from lookup results."""
        total_pages = max(1, (len(artists) + per_page - 1) // per_page)
        start_idx = current_page * per_page
        page_artists = artists[start_idx:start_idx + per_page]

        keyboard = []
        for i, a in enumerate(page_artists):
            idx = start_idx + i
            disamb = f" [{a.disambiguation}]" if a.disambiguation else ""
            label = f"{a.name}{disamb}"
            if len(label) > 40:
                label = label[:37] + "..."
            keyboard.append([
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"{CallbackData.ARTIST}{idx}",
                )
            ])

        if total_pages > 1:
            nav_buttons = []
            if current_page > 0:
                nav_buttons.append(
                    InlineKeyboardButton(text="◀️", callback_data=f"{CallbackData.ARTIST_PAGE}{current_page - 1}")
                )
            nav_buttons.append(
                InlineKeyboardButton(text=f"{current_page + 1}/{total_pages}", callback_data="noop")
            )
            if current_page < total_pages - 1:
                nav_buttons.append(
                    InlineKeyboardButton(text="▶️", callback_data=f"{CallbackData.ARTIST_PAGE}{current_page + 1}")
                )
            keyboard.append(nav_buttons)

        keyboard.append([
            InlineKeyboardButton(text="❌ Отмена", callback_data=CallbackData.CANCEL),
        ])
        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def artist_details(artist: ArtistInfo, already_in_library: bool = False) -> InlineKeyboardMarkup:
        """Create keyboard for artist details (add/search)."""
        keyboard = []
        if already_in_library:
            keyboard.append([
                InlineKeyboardButton(text="🔍 Запустить поиск", callback_data=CallbackData.CONFIRM_GRAB),
            ])
        else:
            keyboard.append([
                InlineKeyboardButton(text="➕ Добавить и искать", callback_data=CallbackData.CONFIRM_GRAB),
            ])
        keyboard.append([
            # LOGIC-24: dedicated music-back so search.handle_back doesn't reply
            # "сессия истекла" on a music session (which has no .results).
            InlineKeyboardButton(text="◀️ Назад", callback_data=CallbackData.MUSIC_BACK),
            InlineKeyboardButton(text="❌ Отмена", callback_data=CallbackData.CANCEL),
        ])
        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def resolution_selection() -> InlineKeyboardMarkup:
        """Create keyboard for selecting preferred resolution."""
        resolutions = [("2160p", "2160p"), ("1080p", "1080p"), ("720p", "720p"), ("Любое", "any")]
        keyboard = []

        for i in range(0, len(resolutions), 2):
            row = []
            for label, value in resolutions[i:i + 2]:
                callback = f"{CallbackData.SET_RESOLUTION}{value}"
                row.append(InlineKeyboardButton(text=label, callback_data=callback))
            keyboard.append(row)

        keyboard.append([
            InlineKeyboardButton(text="◀️ Назад", callback_data=CallbackData.SETTINGS),
        ])

        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def auto_grab_toggle(current: bool) -> InlineKeyboardMarkup:
        """Create keyboard for toggling auto-grab."""
        current_text = "ВКЛ ✓" if current else "ВЫКЛ"
        new_value = 0 if current else 1

        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"Авто-граб: {current_text}",
                        callback_data=f"{CallbackData.SET_AUTO_GRAB}{new_value}",
                    ),
                ],
                [
                    InlineKeyboardButton(text="◀️ Назад", callback_data=CallbackData.SETTINGS),
                ],
            ]
        )

    # =========================================================================
    # qBittorrent / Download Keyboards
    # =========================================================================

    @staticmethod
    def torrent_list(
        torrents: list[TorrentInfo],
        current_page: int = 0,
        total_pages: int = 1,
        current_filter: TorrentFilter = TorrentFilter.ALL,
    ) -> InlineKeyboardMarkup:
        """Create keyboard for torrent list with pagination.

        LOGIC-01: pagination buttons carry ``current_filter`` via the typed
        ``TorrentPageCB`` so paging through a filtered list doesn't silently
        fall back to the unfiltered "all" view.
        """
        keyboard = []

        # Torrent buttons — torrents is already the page slice
        for torrent in torrents:
            # Format: emoji progress% name (speed)
            progress = f"{torrent.progress_percent}%"
            speed = ""
            if torrent.download_speed > 0:
                speed = f" ⬇{torrent.download_speed_formatted}"
            elif torrent.upload_speed > 0:
                speed = f" ⬆{torrent.upload_speed_formatted}"

            name = torrent.name[:25] + "..." if len(torrent.name) > 28 else torrent.name
            label = f"{torrent.state_emoji} {progress} {name}{speed}"
            if len(label) > 50:
                label = label[:47] + "..."

            keyboard.append([
                InlineKeyboardButton(
                    text=label,
                    # PERF-05: full 40-hex hash fits comfortably under the 64-byte
                    # callback_data limit ("t:" + 40 hex = 42 bytes), so lookups
                    # can use the targeted get_torrent(hash) instead of scanning
                    # the whole list for a short-hash prefix match.
                    callback_data=f"{CallbackData.TORRENT}{torrent.hash}",
                )
            ])

        # Pagination row
        if total_pages > 1:
            nav_buttons = []
            if current_page > 0:
                nav_buttons.append(
                    InlineKeyboardButton(
                        text="◀️",
                        callback_data=TorrentPageCB(page=current_page - 1, flt=current_filter.value).pack(),
                    )
                )
            nav_buttons.append(
                InlineKeyboardButton(text=f"{current_page + 1}/{total_pages}", callback_data="noop")
            )
            if current_page < total_pages - 1:
                nav_buttons.append(
                    InlineKeyboardButton(
                        text="▶️",
                        callback_data=TorrentPageCB(page=current_page + 1, flt=current_filter.value).pack(),
                    )
                )
            keyboard.append(nav_buttons)

        # Filter and action buttons
        keyboard.append([
            InlineKeyboardButton(
                text="🔄 Обновить",
                callback_data=TorrentPageCB(page=current_page, flt=current_filter.value).pack(),
            ),
            InlineKeyboardButton(text="🔍 Фильтр", callback_data=f"{CallbackData.TORRENT_FILTER}menu"),
        ])

        keyboard.append([
            InlineKeyboardButton(text="⏸ Пауза всех", callback_data=CallbackData.TORRENT_PAUSE_ALL),
            InlineKeyboardButton(text="▶️ Возобновить", callback_data=CallbackData.TORRENT_RESUME_ALL),
        ])

        keyboard.append([
            InlineKeyboardButton(text="🚀 Лимиты скорости", callback_data=CallbackData.SPEED_MENU),
        ])

        keyboard.append([
            InlineKeyboardButton(text="❌ Закрыть", callback_data=CallbackData.TORRENT_CLOSE),
        ])

        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def torrent_details(
        torrent: TorrentInfo,
        current_filter: TorrentFilter = TorrentFilter.ALL,
    ) -> InlineKeyboardMarkup:
        """Create keyboard for torrent details view.

        LOGIC-01: ``current_filter`` (defaults to ALL when the caller doesn't
        know it — e.g. reached via a legacy ``t_page:`` callback) is threaded
        into the "back to list" button so returning from details doesn't
        silently drop the user's active filter.
        """
        keyboard = []
        # PERF-05: full hash — see torrent_list for the byte-budget rationale.
        full_hash = torrent.hash

        # Pause/Resume based on state
        from bot.models import TorrentState
        if torrent.state in (TorrentState.PAUSED, TorrentState.QUEUED):
            keyboard.append([
                InlineKeyboardButton(text="▶️ Возобновить", callback_data=f"{CallbackData.TORRENT_RESUME}{full_hash}"),
            ])
        else:
            keyboard.append([
                InlineKeyboardButton(text="⏸ Пауза", callback_data=f"{CallbackData.TORRENT_PAUSE}{full_hash}"),
            ])

        # Delete options
        keyboard.append([
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"{CallbackData.TORRENT_DELETE}{full_hash}"),
            InlineKeyboardButton(text="🗑 + Файлы", callback_data=f"{CallbackData.TORRENT_DELETE_FILES}{full_hash}"),
        ])

        # Back button — carries the filter so the list redraw stays filtered.
        keyboard.append([
            InlineKeyboardButton(
                text="◀️ К списку",
                callback_data=TorrentPageCB(page=0, flt=current_filter.value).pack(),
            ),
        ])

        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def torrent_filters(current_filter: TorrentFilter = TorrentFilter.ALL) -> InlineKeyboardMarkup:
        """Create keyboard for selecting torrent filter."""
        filters = [
            (TorrentFilter.ALL, "📋 Все"),
            (TorrentFilter.DOWNLOADING, "⬇️ Загрузка"),
            (TorrentFilter.SEEDING, "⬆️ Раздача"),
            (TorrentFilter.COMPLETED, "✅ Готово"),
            (TorrentFilter.PAUSED, "⏸ Пауза"),
            (TorrentFilter.ACTIVE, "🔥 Активные"),
            (TorrentFilter.STALLED, "⚠️ Застряли"),
            (TorrentFilter.ERRORED, "❌ Ошибки"),
        ]

        keyboard = []
        row = []

        for filter_type, label in filters:
            # Mark current filter
            display_label = f"• {label}" if filter_type == current_filter else label
            row.append(
                InlineKeyboardButton(
                    text=display_label,
                    callback_data=f"{CallbackData.TORRENT_FILTER}{filter_type.value}",
                )
            )
            if len(row) == 2:
                keyboard.append(row)
                row = []

        if row:
            keyboard.append(row)

        keyboard.append([
            InlineKeyboardButton(text="◀️ Назад", callback_data=CallbackData.TORRENT_BACK),
        ])

        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def _speed_preset_row(
        presets: list[tuple[int, str]],
        direction: str,
        current_limit: int,
    ) -> list[InlineKeyboardButton]:
        """Build one row of speed-limit preset buttons (LOGIC-03 helper).

        Collapses what used to be 4 near-identical copies of this loop (two
        rows each for download/upload) into a single implementation. The
        "✓ " marker is honest: it compares against the caller-supplied
        ``current_limit`` (bytes/s) instead of always defaulting to 0.
        """
        row = []
        for speed_kb, label in presets:
            marker = "✓ " if current_limit == speed_kb * 1024 else ""
            row.append(
                InlineKeyboardButton(
                    text=f"{marker}{label}",
                    callback_data=f"{CallbackData.SPEED_LIMIT}{direction}:{speed_kb}",
                )
            )
        return row

    @staticmethod
    def speed_limits_menu(
        current_dl_limit: int = 0,
        current_ul_limit: int = 0,
    ) -> InlineKeyboardMarkup:
        """Create keyboard for speed limit presets.

        LOGIC-03: callers must pass the qBittorrent-reported current limits
        (bytes/s) so the "✓" marker reflects reality — previously this was
        always called with the 0/0 defaults, which happened to always match
        the "unlimited" preset regardless of the real setting.
        """
        # Presets in KB/s (0 = unlimited)
        presets = [
            (0, "∞ Без лимита"),
            (512, "512 КБ/с"),
            (1024, "1 МБ/с"),
            (2048, "2 МБ/с"),
            (5120, "5 МБ/с"),
            (10240, "10 МБ/с"),
        ]

        keyboard = [
            [InlineKeyboardButton(text="⬇️ Лимит загрузки:", callback_data="noop")],
            Keyboards._speed_preset_row(presets[:3], "dl", current_dl_limit),
            Keyboards._speed_preset_row(presets[3:], "dl", current_dl_limit),
            [InlineKeyboardButton(text="⬆️ Лимит отдачи:", callback_data="noop")],
            Keyboards._speed_preset_row(presets[:3], "ul", current_ul_limit),
            Keyboards._speed_preset_row(presets[3:], "ul", current_ul_limit),
            [InlineKeyboardButton(text="◀️ Назад", callback_data=CallbackData.TORRENT_BACK)],
        ]

        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def confirm_delete_torrent(torrent_hash: str, with_files: bool = False) -> InlineKeyboardMarkup:
        """Create confirmation keyboard for deleting a torrent.

        BUG-14/DEAD-03: previously generated ``t_delete:confirm:<hash16>`` /
        ``t_delf:confirm:<hash16>``, which the handlers would have parsed as
        a (nonexistent) short-hash "confirm" — this keyboard was unreachable
        AND broken. Now wired for the "delete with files" flow only (the
        irreversible one): confirm uses the dedicated ``t_delfc:`` prefix,
        cancel goes back to the torrent card via ``t:<hash>``. Plain delete
        (keep files, reversible) still fires immediately without this step.
        """
        full_hash = torrent_hash
        if with_files:
            confirm_callback = f"{CallbackData.TORRENT_DELETE_FILES_CONFIRM}{full_hash}"
            text = "⚠️ Да, удалить с файлами"
        else:
            confirm_callback = f"{CallbackData.TORRENT_DELETE}{full_hash}"
            text = "Да, удалить торрент"

        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text=text, callback_data=confirm_callback),
                ],
                [
                    InlineKeyboardButton(
                        text="❌ Отмена", callback_data=f"{CallbackData.TORRENT}{full_hash}"
                    ),
                ],
            ]
        )

    # =========================================================================
    # Emby Keyboards
    # =========================================================================

    @staticmethod
    def emby_main(
        has_update: bool = False,
        can_restart: bool = True,
        can_update: bool = True,
    ) -> InlineKeyboardMarkup:
        """Create main Emby control keyboard."""
        keyboard = []

        # Library scan buttons
        keyboard.append([
            InlineKeyboardButton(text="🔄 Обновить статус", callback_data=CallbackData.EMBY_REFRESH),
        ])

        keyboard.append([
            InlineKeyboardButton(text="📚 Сканировать всё", callback_data=CallbackData.EMBY_SCAN_ALL),
        ])

        keyboard.append([
            InlineKeyboardButton(text="🎬 Фильмы", callback_data=CallbackData.EMBY_SCAN_MOVIES),
            InlineKeyboardButton(text="📺 Сериалы", callback_data=CallbackData.EMBY_SCAN_SERIES),
        ])

        # Server control buttons
        if can_restart:
            keyboard.append([
                InlineKeyboardButton(text="🔁 Перезагрузить", callback_data=CallbackData.EMBY_RESTART),
            ])

        if can_update and has_update:
            keyboard.append([
                InlineKeyboardButton(text="⬆️ Установить обновление", callback_data=CallbackData.EMBY_UPDATE),
            ])

        keyboard.append([
            InlineKeyboardButton(text="❌ Закрыть", callback_data=CallbackData.EMBY_CLOSE),
        ])

        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def emby_confirm_restart() -> InlineKeyboardMarkup:
        """Create confirmation keyboard for server restart."""
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⚠️ Да, перезагрузить",
                        callback_data=CallbackData.EMBY_RESTART_CONFIRM,
                    ),
                ],
                [
                    InlineKeyboardButton(text="❌ Отмена", callback_data=CallbackData.EMBY_REFRESH),
                ],
            ]
        )

    @staticmethod
    def emby_confirm_update() -> InlineKeyboardMarkup:
        """Create confirmation keyboard for server update."""
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⚠️ Да, обновить",
                        callback_data=CallbackData.EMBY_UPDATE_CONFIRM,
                    ),
                ],
                [
                    InlineKeyboardButton(text="❌ Отмена", callback_data=CallbackData.EMBY_REFRESH),
                ],
            ]
        )

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
                    callback_data=f"{CallbackData.TRENDING_ARTIST}{i}",
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
                    callback_data=f"{CallbackData.TRENDING_MOVIE}{movie.tmdb_id}",
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
                    callback_data=f"{CallbackData.TRENDING_SERIES_ITEM}{series.tmdb_id}",
                )
            ])

        keyboard.append([
            InlineKeyboardButton(text="◀️ Назад", callback_data=CallbackData.TRENDING_BACK),
        ])

        return InlineKeyboardMarkup(inline_keyboard=keyboard)

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
    def movie_details(movie: MovieInfo) -> InlineKeyboardMarkup:
        """Create keyboard for movie details from trending."""
        keyboard = []
        links = Keyboards._external_links(movie)
        if links:
            keyboard.append(links)
        keyboard.append([InlineKeyboardButton(text="➕ Добавить в Radarr", callback_data=f"{CallbackData.ADD_MOVIE}{movie.tmdb_id}")])
        keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data=CallbackData.TRENDING_MOVIES)])
        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def series_details(series: SeriesInfo) -> InlineKeyboardMarkup:
        """Create keyboard for series details from trending."""
        keyboard = []
        links = Keyboards._external_links(series)
        if links:
            keyboard.append(links)
        keyboard.append([InlineKeyboardButton(text="➕ Добавить в Sonarr", callback_data=f"{CallbackData.ADD_SERIES}{series.tmdb_id}")])
        keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data=CallbackData.TRENDING_SERIES)])
        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    # =========================================================================
    # Calendar Keyboards
    # =========================================================================

    @staticmethod
    def calendar_controls(current_days: int = 7) -> InlineKeyboardMarkup:
        """Create keyboard for calendar period selection."""
        periods = [
            ("7 дней", CallbackData.CALENDAR_7, 7),
            ("14 дней", CallbackData.CALENDAR_14, 14),
            ("30 дней", CallbackData.CALENDAR_30, 30),
        ]
        buttons = []
        for label, callback, days in periods:
            text = f"• {label} •" if days == current_days else label
            buttons.append(InlineKeyboardButton(text=text, callback_data=callback))

        return InlineKeyboardMarkup(
            inline_keyboard=[
                buttons,
                [InlineKeyboardButton(text="🔄 Обновить", callback_data=CallbackData.CALENDAR_REFRESH)],
            ]
        )
