"""Inline keyboard builders for Telegram bot."""

from typing import Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from bot.models import (
    ContentType,
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

    # Pagination
    PAGE = "page:"  # page:5
    BACK = "back"
    CANCEL = "cancel"

    # Release selection
    RELEASE = "rel:"  # rel:0 (index in results)
    GRAB_BEST = "grab_best"

    # Content selection (for add)
    MOVIE = "movie:"  # movie:tmdb_id
    SERIES = "series:"  # series:tvdb_id
    ADD_MOVIE = "add_movie:"  # add_movie:tmdb_id
    ADD_SERIES = "add_series:"  # add_series:tmdb_id

    # Confirmation
    CONFIRM_ADD = "confirm_add"
    CONFIRM_GRAB = "confirm_grab"
    FORCE_GRAB = "force_grab"  # Force download via qBittorrent

    # Settings
    SETTINGS = "settings"
    SET_RADARR_PROFILE = "set:rp:"  # set:rp:1
    SET_RADARR_FOLDER = "set:rf:"  # set:rf:1
    SET_SONARR_PROFILE = "set:sp:"  # set:sp:1
    SET_SONARR_FOLDER = "set:sf:"  # set:sf:1
    SET_RESOLUTION = "set:res:"  # set:res:1080p
    SET_AUTO_GRAB = "set:ag:"  # set:ag:1 or set:ag:0

    # Monitor type for series
    MONITOR = "mon:"  # mon:all, mon:future, etc.

    # Season selection
    SEASON = "season:"  # season:1

    # qBittorrent / Downloads
    TORRENT = "t:"  # t:hash - select torrent
    TORRENT_PAUSE = "t_pause:"  # t_pause:hash
    TORRENT_RESUME = "t_resume:"  # t_resume:hash
    TORRENT_DELETE = "t_del:"  # t_del:hash
    TORRENT_DELETE_FILES = "t_delf:"  # t_delf:hash (delete with files)
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
    TRENDING_MOVIE = "trend_m:"  # trend_m:tmdb_id - view movie details
    TRENDING_SERIES_ITEM = "trend_s:"  # trend_s:tmdb_id - view series details

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
                [KeyboardButton(text="🔍 Поиск"), KeyboardButton(text="🎬 Фильм"), KeyboardButton(text="📺 Сериал")],
                [KeyboardButton(text="📥 Загрузки"), KeyboardButton(text="📊 qBit"), KeyboardButton(text="🔥 Топ")],
                [KeyboardButton(text="📺 Emby"), KeyboardButton(text="� Календарь"), KeyboardButton(text="🔌 Статус")],
                [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text="📋 История"), KeyboardButton(text="❓ Помощь")],
            ],
            resize_keyboard=True,
            input_field_placeholder="Введите название для поиска...",
        )

    @staticmethod
    def content_type_selection() -> InlineKeyboardMarkup:
        """Create keyboard for selecting content type (movie/series)."""
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🎬 Фильм", callback_data=CallbackData.TYPE_MOVIE),
                    InlineKeyboardButton(text="📺 Сериал", callback_data=CallbackData.TYPE_SERIES),
                ],
                [
                    InlineKeyboardButton(text="❌ Отмена", callback_data=CallbackData.CANCEL),
                ],
            ]
        )

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

        # Pagination row
        nav_buttons = []
        if current_page > 0:
            nav_buttons.append(
                InlineKeyboardButton(text="◀️", callback_data=f"{CallbackData.PAGE}{current_page - 1}")
            )

        nav_buttons.append(
            InlineKeyboardButton(text=f"{current_page + 1}/{total_pages}", callback_data="noop")
        )

        if current_page < total_pages - 1:
            nav_buttons.append(
                InlineKeyboardButton(text="▶️", callback_data=f"{CallbackData.PAGE}{current_page + 1}")
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
    ) -> InlineKeyboardMarkup:
        """Create keyboard for release details view."""
        keyboard = []

        if can_grab:
            keyboard.append([
                InlineKeyboardButton(text="✅ Скачать", callback_data=CallbackData.CONFIRM_GRAB),
            ])

        if show_force_grab:
            keyboard.append([
                InlineKeyboardButton(text="⚡ Принудительно (qBit)", callback_data=CallbackData.FORCE_GRAB),
            ])

        keyboard.append([
            InlineKeyboardButton(text="◀️ Назад", callback_data=CallbackData.BACK),
            InlineKeyboardButton(text="❌ Отмена", callback_data=CallbackData.CANCEL),
        ])

        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def movie_list(
        movies: list[MovieInfo],
        current_page: int = 0,
        per_page: int = 5,
    ) -> InlineKeyboardMarkup:
        """Create keyboard for movie selection from lookup results."""
        total_pages = max(1, (len(movies) + per_page - 1) // per_page)
        start_idx = current_page * per_page
        page_movies = movies[start_idx:start_idx + per_page]

        keyboard = []

        for movie in page_movies:
            label = f"{movie.title} ({movie.year})"
            if len(label) > 40:
                label = label[:37] + "..."

            keyboard.append([
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"{CallbackData.MOVIE}{movie.tmdb_id}",
                )
            ])

        # Pagination
        if total_pages > 1:
            nav_buttons = []
            if current_page > 0:
                nav_buttons.append(
                    InlineKeyboardButton(text="◀️", callback_data=f"{CallbackData.PAGE}{current_page - 1}")
                )
            nav_buttons.append(
                InlineKeyboardButton(text=f"{current_page + 1}/{total_pages}", callback_data="noop")
            )
            if current_page < total_pages - 1:
                nav_buttons.append(
                    InlineKeyboardButton(text="▶️", callback_data=f"{CallbackData.PAGE}{current_page + 1}")
                )
            keyboard.append(nav_buttons)

        keyboard.append([
            InlineKeyboardButton(text="❌ Отмена", callback_data=CallbackData.CANCEL),
        ])

        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def series_list(
        series: list[SeriesInfo],
        current_page: int = 0,
        per_page: int = 5,
    ) -> InlineKeyboardMarkup:
        """Create keyboard for series selection from lookup results."""
        total_pages = max(1, (len(series) + per_page - 1) // per_page)
        start_idx = current_page * per_page
        page_series = series[start_idx:start_idx + per_page]

        keyboard = []

        for s in page_series:
            year_str = f" ({s.year})" if s.year else ""
            label = f"{s.title}{year_str}"
            if len(label) > 40:
                label = label[:37] + "..."

            keyboard.append([
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"{CallbackData.SERIES}{s.tvdb_id}",
                )
            ])

        # Pagination
        if total_pages > 1:
            nav_buttons = []
            if current_page > 0:
                nav_buttons.append(
                    InlineKeyboardButton(text="◀️", callback_data=f"{CallbackData.PAGE}{current_page - 1}")
                )
            nav_buttons.append(
                InlineKeyboardButton(text=f"{current_page + 1}/{total_pages}", callback_data="noop")
            )
            if current_page < total_pages - 1:
                nav_buttons.append(
                    InlineKeyboardButton(text="▶️", callback_data=f"{CallbackData.PAGE}{current_page + 1}")
                )
            keyboard.append(nav_buttons)

        keyboard.append([
            InlineKeyboardButton(text="❌ Отмена", callback_data=CallbackData.CANCEL),
        ])

        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def confirm_add(content_type: ContentType, has_release: bool = False) -> InlineKeyboardMarkup:
        """Create confirmation keyboard for adding content."""
        keyboard = []

        if has_release:
            keyboard.append([
                InlineKeyboardButton(text="✅ Добавить и скачать", callback_data=CallbackData.CONFIRM_GRAB),
            ])
        else:
            keyboard.append([
                InlineKeyboardButton(text="✅ Добавить", callback_data=CallbackData.CONFIRM_ADD),
            ])

        keyboard.append([
            InlineKeyboardButton(text="◀️ Назад", callback_data=CallbackData.BACK),
            InlineKeyboardButton(text="❌ Отмена", callback_data=CallbackData.CANCEL),
        ])

        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def monitor_type_selection() -> InlineKeyboardMarkup:
        """Create keyboard for selecting monitor type (series)."""
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Все серии", callback_data=f"{CallbackData.MONITOR}all"),
                    InlineKeyboardButton(text="Только будущие", callback_data=f"{CallbackData.MONITOR}future"),
                ],
                [
                    InlineKeyboardButton(text="Пропущенные", callback_data=f"{CallbackData.MONITOR}missing"),
                    InlineKeyboardButton(text="Первый сезон", callback_data=f"{CallbackData.MONITOR}firstSeason"),
                ],
                [
                    InlineKeyboardButton(text="Последний сезон", callback_data=f"{CallbackData.MONITOR}latestSeason"),
                    InlineKeyboardButton(text="Только пилот", callback_data=f"{CallbackData.MONITOR}pilot"),
                ],
                [
                    InlineKeyboardButton(text="Ничего", callback_data=f"{CallbackData.MONITOR}none"),
                ],
                [
                    InlineKeyboardButton(text="❌ Отмена", callback_data=CallbackData.CANCEL),
                ],
            ]
        )

    @staticmethod
    def season_selection(seasons: list[dict], include_all: bool = True) -> InlineKeyboardMarkup:
        """Create keyboard for selecting season(s)."""
        keyboard = []

        if include_all:
            keyboard.append([
                InlineKeyboardButton(text="📦 Все сезоны", callback_data=f"{CallbackData.SEASON}all"),
            ])

        # Group seasons in rows of 4
        season_buttons = []
        for s in seasons:
            season_num = s.get("seasonNumber", 0)
            if season_num == 0:
                continue  # Skip specials for now
            season_buttons.append(
                InlineKeyboardButton(
                    text=f"S{season_num:02d}",
                    callback_data=f"{CallbackData.SEASON}{season_num}",
                )
            )

        # Arrange in rows of 4
        for i in range(0, len(season_buttons), 4):
            keyboard.append(season_buttons[i:i + 4])

        keyboard.append([
            InlineKeyboardButton(text="❌ Отмена", callback_data=CallbackData.CANCEL),
        ])

        return InlineKeyboardMarkup(inline_keyboard=keyboard)

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
    def settings_menu() -> InlineKeyboardMarkup:
        """Create main settings menu keyboard."""
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🎬 Профиль Radarr", callback_data="settings:radarr_profile"),
                    InlineKeyboardButton(text="📁 Папка Radarr", callback_data="settings:radarr_folder"),
                ],
                [
                    InlineKeyboardButton(text="📺 Профиль Sonarr", callback_data="settings:sonarr_profile"),
                    InlineKeyboardButton(text="📁 Папка Sonarr", callback_data="settings:sonarr_folder"),
                ],
                [
                    InlineKeyboardButton(text="🎯 Качество", callback_data="settings:resolution"),
                ],
                [
                    InlineKeyboardButton(text="⚡ Авто-граб", callback_data="settings:auto_grab"),
                ],
                [
                    InlineKeyboardButton(text="❌ Закрыть", callback_data=CallbackData.CANCEL),
                ],
            ]
        )

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

    @staticmethod
    def simple_back_cancel() -> InlineKeyboardMarkup:
        """Simple keyboard with back and cancel buttons."""
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="◀️ Назад", callback_data=CallbackData.BACK),
                    InlineKeyboardButton(text="❌ Отмена", callback_data=CallbackData.CANCEL),
                ],
            ]
        )

    @staticmethod
    def cancel_only() -> InlineKeyboardMarkup:
        """Simple keyboard with only cancel button."""
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="❌ Отмена", callback_data=CallbackData.CANCEL),
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
        """Create keyboard for torrent list with pagination."""
        # torrents is already a page slice, use passed total_pages
        page_torrents = torrents

        keyboard = []

        # Torrent buttons
        for torrent in page_torrents:
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
                    callback_data=f"{CallbackData.TORRENT}{torrent.hash[:16]}",
                )
            ])

        # Pagination row
        if total_pages > 1:
            nav_buttons = []
            if current_page > 0:
                nav_buttons.append(
                    InlineKeyboardButton(text="◀️", callback_data=f"{CallbackData.TORRENT_PAGE}{current_page - 1}")
                )
            nav_buttons.append(
                InlineKeyboardButton(text=f"{current_page + 1}/{total_pages}", callback_data="noop")
            )
            if current_page < total_pages - 1:
                nav_buttons.append(
                    InlineKeyboardButton(text="▶️", callback_data=f"{CallbackData.TORRENT_PAGE}{current_page + 1}")
                )
            keyboard.append(nav_buttons)

        # Filter and action buttons
        keyboard.append([
            InlineKeyboardButton(text="🔄 Обновить", callback_data=CallbackData.TORRENT_REFRESH),
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
    def torrent_details(torrent: TorrentInfo) -> InlineKeyboardMarkup:
        """Create keyboard for torrent details view."""
        keyboard = []
        hash_short = torrent.hash[:16]

        # Pause/Resume based on state
        from bot.models import TorrentState
        if torrent.state in (TorrentState.PAUSED, TorrentState.QUEUED):
            keyboard.append([
                InlineKeyboardButton(text="▶️ Возобновить", callback_data=f"{CallbackData.TORRENT_RESUME}{hash_short}"),
            ])
        else:
            keyboard.append([
                InlineKeyboardButton(text="⏸ Пауза", callback_data=f"{CallbackData.TORRENT_PAUSE}{hash_short}"),
            ])

        # Delete options
        keyboard.append([
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"{CallbackData.TORRENT_DELETE}{hash_short}"),
            InlineKeyboardButton(text="🗑 + Файлы", callback_data=f"{CallbackData.TORRENT_DELETE_FILES}{hash_short}"),
        ])

        # Back button
        keyboard.append([
            InlineKeyboardButton(text="◀️ К списку", callback_data=CallbackData.TORRENT_BACK),
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
    def speed_limits_menu(
        current_dl_limit: int = 0,
        current_ul_limit: int = 0,
    ) -> InlineKeyboardMarkup:
        """Create keyboard for speed limit presets."""
        # Presets in KB/s (0 = unlimited)
        presets = [
            (0, "∞ Без лимита"),
            (512, "512 КБ/с"),
            (1024, "1 МБ/с"),
            (2048, "2 МБ/с"),
            (5120, "5 МБ/с"),
            (10240, "10 МБ/с"),
        ]

        keyboard = []

        # Download limits
        keyboard.append([
            InlineKeyboardButton(text="⬇️ Лимит загрузки:", callback_data="noop"),
        ])

        dl_row = []
        for speed_kb, label in presets[:3]:
            marker = "✓ " if current_dl_limit == speed_kb * 1024 else ""
            dl_row.append(
                InlineKeyboardButton(
                    text=f"{marker}{label}",
                    callback_data=f"{CallbackData.SPEED_LIMIT}dl:{speed_kb}",
                )
            )
        keyboard.append(dl_row)

        dl_row2 = []
        for speed_kb, label in presets[3:]:
            marker = "✓ " if current_dl_limit == speed_kb * 1024 else ""
            dl_row2.append(
                InlineKeyboardButton(
                    text=f"{marker}{label}",
                    callback_data=f"{CallbackData.SPEED_LIMIT}dl:{speed_kb}",
                )
            )
        keyboard.append(dl_row2)

        # Upload limits
        keyboard.append([
            InlineKeyboardButton(text="⬆️ Лимит отдачи:", callback_data="noop"),
        ])

        ul_row = []
        for speed_kb, label in presets[:3]:
            marker = "✓ " if current_ul_limit == speed_kb * 1024 else ""
            ul_row.append(
                InlineKeyboardButton(
                    text=f"{marker}{label}",
                    callback_data=f"{CallbackData.SPEED_LIMIT}ul:{speed_kb}",
                )
            )
        keyboard.append(ul_row)

        ul_row2 = []
        for speed_kb, label in presets[3:]:
            marker = "✓ " if current_ul_limit == speed_kb * 1024 else ""
            ul_row2.append(
                InlineKeyboardButton(
                    text=f"{marker}{label}",
                    callback_data=f"{CallbackData.SPEED_LIMIT}ul:{speed_kb}",
                )
            )
        keyboard.append(ul_row2)

        keyboard.append([
            InlineKeyboardButton(text="◀️ Назад", callback_data=CallbackData.TORRENT_BACK),
        ])

        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def confirm_delete_torrent(torrent_hash: str, with_files: bool = False) -> InlineKeyboardMarkup:
        """Create confirmation keyboard for deleting a torrent."""
        hash_short = torrent_hash[:16]
        if with_files:
            confirm_callback = f"{CallbackData.TORRENT_DELETE_FILES}confirm:{hash_short}"
            text = "⚠️ Да, удалить с файлами"
        else:
            confirm_callback = f"{CallbackData.TORRENT_DELETE}confirm:{hash_short}"
            text = "Да, удалить торрент"

        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text=text, callback_data=confirm_callback),
                ],
                [
                    InlineKeyboardButton(text="❌ Отмена", callback_data=CallbackData.TORRENT_BACK),
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
    def trending_menu() -> InlineKeyboardMarkup:
        """Create trending/popular content selection menu."""
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🎬 Популярные фильмы", callback_data=CallbackData.TRENDING_MOVIES),
                ],
                [
                    InlineKeyboardButton(text="📺 Популярные сериалы", callback_data=CallbackData.TRENDING_SERIES),
                ],
            ]
        )

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
            InlineKeyboardButton(text="◀️ Назад", callback_data=CallbackData.BACK),
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
            InlineKeyboardButton(text="◀️ Назад", callback_data=CallbackData.BACK),
        ])

        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def movie_details(movie: MovieInfo) -> InlineKeyboardMarkup:
        """Create keyboard for movie details from trending."""
        keyboard = [
            [InlineKeyboardButton(text="➕ Добавить в Radarr", callback_data=f"{CallbackData.ADD_MOVIE}{movie.tmdb_id}")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data=CallbackData.TRENDING_MOVIES)],
        ]
        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def series_details(series: SeriesInfo) -> InlineKeyboardMarkup:
        """Create keyboard for series details from trending."""
        keyboard = [
            [InlineKeyboardButton(text="➕ Добавить в Sonarr", callback_data=f"{CallbackData.ADD_SERIES}{series.tmdb_id}")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data=CallbackData.TRENDING_SERIES)],
        ]
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
