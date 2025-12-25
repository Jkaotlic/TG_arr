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

    # Confirmation
    CONFIRM_ADD = "confirm_add"
    CONFIRM_GRAB = "confirm_grab"

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
    SPEED_LIMIT = "speed:"  # speed:1024 (KB/s)
    SPEED_MENU = "speed_menu"


class Keyboards:
    """Inline keyboard builders."""

    @staticmethod
    def main_menu() -> ReplyKeyboardMarkup:
        """Create main (reply) menu keyboard with the most used commands."""
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="/search"), KeyboardButton(text="/movie"), KeyboardButton(text="/series")],
                [KeyboardButton(text="/downloads"), KeyboardButton(text="/qstatus"), KeyboardButton(text="/status")],
                [KeyboardButton(text="/pause"), KeyboardButton(text="/resume")],
                [KeyboardButton(text="/settings"), KeyboardButton(text="/history")],
                [KeyboardButton(text="/help")],
            ],
            resize_keyboard=True,
            input_field_placeholder="Send a title to search, or pick a command",
        )

    @staticmethod
    def content_type_selection() -> InlineKeyboardMarkup:
        """Create keyboard for selecting content type (movie/series)."""
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🎬 Movie", callback_data=CallbackData.TYPE_MOVIE),
                    InlineKeyboardButton(text="📺 Series", callback_data=CallbackData.TYPE_SERIES),
                ],
                [
                    InlineKeyboardButton(text="❌ Cancel", callback_data=CallbackData.CANCEL),
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
                    text=f"⚡ Grab Best (Score: {best_score})",
                    callback_data=CallbackData.GRAB_BEST,
                )
            ])

        # Pagination row
        nav_buttons = []
        if current_page > 0:
            nav_buttons.append(
                InlineKeyboardButton(text="◀️ Prev", callback_data=f"{CallbackData.PAGE}{current_page - 1}")
            )

        nav_buttons.append(
            InlineKeyboardButton(text=f"{current_page + 1}/{total_pages}", callback_data="noop")
        )

        if current_page < total_pages - 1:
            nav_buttons.append(
                InlineKeyboardButton(text="Next ▶️", callback_data=f"{CallbackData.PAGE}{current_page + 1}")
            )

        if nav_buttons:
            keyboard.append(nav_buttons)

        # Cancel button
        keyboard.append([
            InlineKeyboardButton(text="❌ Cancel", callback_data=CallbackData.CANCEL),
        ])

        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def release_details(
        result: SearchResult,
        content_type: ContentType,
        can_grab: bool = True,
    ) -> InlineKeyboardMarkup:
        """Create keyboard for release details view."""
        keyboard = []

        if can_grab:
            keyboard.append([
                InlineKeyboardButton(text="✅ Grab This Release", callback_data=CallbackData.CONFIRM_GRAB),
            ])

        keyboard.append([
            InlineKeyboardButton(text="◀️ Back to Results", callback_data=CallbackData.BACK),
        ])

        keyboard.append([
            InlineKeyboardButton(text="❌ Cancel", callback_data=CallbackData.CANCEL),
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
            InlineKeyboardButton(text="❌ Cancel", callback_data=CallbackData.CANCEL),
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
            InlineKeyboardButton(text="❌ Cancel", callback_data=CallbackData.CANCEL),
        ])

        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def confirm_add(content_type: ContentType, has_release: bool = False) -> InlineKeyboardMarkup:
        """Create confirmation keyboard for adding content."""
        keyboard = []

        if has_release:
            keyboard.append([
                InlineKeyboardButton(text="✅ Add & Grab Release", callback_data=CallbackData.CONFIRM_GRAB),
            ])
        else:
            keyboard.append([
                InlineKeyboardButton(text="✅ Add & Search", callback_data=CallbackData.CONFIRM_ADD),
            ])

        keyboard.append([
            InlineKeyboardButton(text="◀️ Back", callback_data=CallbackData.BACK),
            InlineKeyboardButton(text="❌ Cancel", callback_data=CallbackData.CANCEL),
        ])

        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def monitor_type_selection() -> InlineKeyboardMarkup:
        """Create keyboard for selecting monitor type (series)."""
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="All Episodes", callback_data=f"{CallbackData.MONITOR}all"),
                    InlineKeyboardButton(text="Future Only", callback_data=f"{CallbackData.MONITOR}future"),
                ],
                [
                    InlineKeyboardButton(text="Missing Only", callback_data=f"{CallbackData.MONITOR}missing"),
                    InlineKeyboardButton(text="First Season", callback_data=f"{CallbackData.MONITOR}firstSeason"),
                ],
                [
                    InlineKeyboardButton(text="Latest Season", callback_data=f"{CallbackData.MONITOR}latestSeason"),
                    InlineKeyboardButton(text="Pilot Only", callback_data=f"{CallbackData.MONITOR}pilot"),
                ],
                [
                    InlineKeyboardButton(text="None", callback_data=f"{CallbackData.MONITOR}none"),
                ],
                [
                    InlineKeyboardButton(text="❌ Cancel", callback_data=CallbackData.CANCEL),
                ],
            ]
        )

    @staticmethod
    def season_selection(seasons: list[dict], include_all: bool = True) -> InlineKeyboardMarkup:
        """Create keyboard for selecting season(s)."""
        keyboard = []

        if include_all:
            keyboard.append([
                InlineKeyboardButton(text="📦 All Seasons", callback_data=f"{CallbackData.SEASON}all"),
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
            InlineKeyboardButton(text="❌ Cancel", callback_data=CallbackData.CANCEL),
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
            InlineKeyboardButton(text="◀️ Back", callback_data=CallbackData.SETTINGS),
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
            InlineKeyboardButton(text="◀️ Back", callback_data=CallbackData.SETTINGS),
        ])

        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def settings_menu() -> InlineKeyboardMarkup:
        """Create main settings menu keyboard."""
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🎬 Radarr Profile", callback_data="settings:radarr_profile"),
                    InlineKeyboardButton(text="📁 Radarr Folder", callback_data="settings:radarr_folder"),
                ],
                [
                    InlineKeyboardButton(text="📺 Sonarr Profile", callback_data="settings:sonarr_profile"),
                    InlineKeyboardButton(text="📁 Sonarr Folder", callback_data="settings:sonarr_folder"),
                ],
                [
                    InlineKeyboardButton(text="🎯 Preferred Quality", callback_data="settings:resolution"),
                ],
                [
                    InlineKeyboardButton(text="⚡ Auto-Grab", callback_data="settings:auto_grab"),
                ],
                [
                    InlineKeyboardButton(text="❌ Close", callback_data=CallbackData.CANCEL),
                ],
            ]
        )

    @staticmethod
    def resolution_selection() -> InlineKeyboardMarkup:
        """Create keyboard for selecting preferred resolution."""
        resolutions = ["2160p", "1080p", "720p", "Any"]
        keyboard = []

        for i in range(0, len(resolutions), 2):
            row = []
            for res in resolutions[i:i + 2]:
                callback = f"{CallbackData.SET_RESOLUTION}{res.lower()}"
                row.append(InlineKeyboardButton(text=res, callback_data=callback))
            keyboard.append(row)

        keyboard.append([
            InlineKeyboardButton(text="◀️ Back", callback_data=CallbackData.SETTINGS),
        ])

        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def auto_grab_toggle(current: bool) -> InlineKeyboardMarkup:
        """Create keyboard for toggling auto-grab."""
        current_text = "ON ✓" if current else "OFF"
        new_value = 0 if current else 1

        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"Auto-Grab: {current_text}",
                        callback_data=f"{CallbackData.SET_AUTO_GRAB}{new_value}",
                    ),
                ],
                [
                    InlineKeyboardButton(text="◀️ Back", callback_data=CallbackData.SETTINGS),
                ],
            ]
        )

    @staticmethod
    def simple_back_cancel() -> InlineKeyboardMarkup:
        """Simple keyboard with back and cancel buttons."""
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="◀️ Back", callback_data=CallbackData.BACK),
                    InlineKeyboardButton(text="❌ Cancel", callback_data=CallbackData.CANCEL),
                ],
            ]
        )

    @staticmethod
    def cancel_only() -> InlineKeyboardMarkup:
        """Simple keyboard with only cancel button."""
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="❌ Cancel", callback_data=CallbackData.CANCEL),
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
        per_page: int = 5,
        current_filter: TorrentFilter = TorrentFilter.ALL,
    ) -> InlineKeyboardMarkup:
        """Create keyboard for torrent list with pagination."""
        total_pages = max(1, (len(torrents) + per_page - 1) // per_page)
        start_idx = current_page * per_page
        page_torrents = torrents[start_idx:start_idx + per_page]

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
            InlineKeyboardButton(text="🔄 Refresh", callback_data=CallbackData.TORRENT_REFRESH),
            InlineKeyboardButton(text="🔍 Filter", callback_data=f"{CallbackData.TORRENT_FILTER}menu"),
        ])

        keyboard.append([
            InlineKeyboardButton(text="⏸ Pause All", callback_data=CallbackData.TORRENT_PAUSE_ALL),
            InlineKeyboardButton(text="▶️ Resume All", callback_data=CallbackData.TORRENT_RESUME_ALL),
        ])

        keyboard.append([
            InlineKeyboardButton(text="🚀 Speed Limits", callback_data=CallbackData.SPEED_MENU),
        ])

        keyboard.append([
            InlineKeyboardButton(text="❌ Close", callback_data=CallbackData.CANCEL),
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
                InlineKeyboardButton(text="▶️ Resume", callback_data=f"{CallbackData.TORRENT_RESUME}{hash_short}"),
            ])
        else:
            keyboard.append([
                InlineKeyboardButton(text="⏸ Pause", callback_data=f"{CallbackData.TORRENT_PAUSE}{hash_short}"),
            ])

        # Delete options
        keyboard.append([
            InlineKeyboardButton(text="🗑 Remove", callback_data=f"{CallbackData.TORRENT_DELETE}{hash_short}"),
            InlineKeyboardButton(text="🗑 + Files", callback_data=f"{CallbackData.TORRENT_DELETE_FILES}{hash_short}"),
        ])

        # Back button
        keyboard.append([
            InlineKeyboardButton(text="◀️ Back to List", callback_data=CallbackData.TORRENT_BACK),
        ])

        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def torrent_filters(current_filter: TorrentFilter = TorrentFilter.ALL) -> InlineKeyboardMarkup:
        """Create keyboard for selecting torrent filter."""
        filters = [
            (TorrentFilter.ALL, "📋 All"),
            (TorrentFilter.DOWNLOADING, "⬇️ Downloading"),
            (TorrentFilter.SEEDING, "⬆️ Seeding"),
            (TorrentFilter.COMPLETED, "✅ Completed"),
            (TorrentFilter.PAUSED, "⏸ Paused"),
            (TorrentFilter.ACTIVE, "🔥 Active"),
            (TorrentFilter.STALLED, "⚠️ Stalled"),
            (TorrentFilter.ERRORED, "❌ Errored"),
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
            InlineKeyboardButton(text="◀️ Back", callback_data=CallbackData.TORRENT_BACK),
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
            (0, "♾ Unlimited"),
            (512, "512 KB/s"),
            (1024, "1 MB/s"),
            (2048, "2 MB/s"),
            (5120, "5 MB/s"),
            (10240, "10 MB/s"),
        ]

        keyboard = []

        # Download limits
        keyboard.append([
            InlineKeyboardButton(text="⬇️ Download Limit:", callback_data="noop"),
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
            InlineKeyboardButton(text="⬆️ Upload Limit:", callback_data="noop"),
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
            InlineKeyboardButton(text="◀️ Back", callback_data=CallbackData.TORRENT_BACK),
        ])

        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def confirm_delete_torrent(torrent_hash: str, with_files: bool = False) -> InlineKeyboardMarkup:
        """Create confirmation keyboard for deleting a torrent."""
        hash_short = torrent_hash[:16]
        if with_files:
            confirm_callback = f"{CallbackData.TORRENT_DELETE_FILES}confirm:{hash_short}"
            text = "⚠️ Yes, delete torrent and files"
        else:
            confirm_callback = f"{CallbackData.TORRENT_DELETE}confirm:{hash_short}"
            text = "Yes, remove torrent"

        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text=text, callback_data=confirm_callback),
                ],
                [
                    InlineKeyboardButton(text="❌ Cancel", callback_data=CallbackData.TORRENT_BACK),
                ],
            ]
        )
