"""Callback data prefixes for parsing.

Split out of the former monolithic ``bot/ui/keyboards.py`` (see the package
``__init__.py`` docstring for the split rationale). This module has no
dependents inside the ``keyboards`` package other than through ``CallbackData``
itself — it exists standalone so every domain module can import just the
constants it needs without pulling in keyboard-building code.
"""


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
