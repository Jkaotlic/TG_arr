"""Message formatters for Telegram bot."""

import html
from datetime import datetime
from typing import Optional

from bot.models import (
    ActionLog,
    ContentType,
    MovieInfo,
    QBittorrentStatus,
    QualityProfile,
    RootFolder,
    SearchResult,
    SeriesInfo,
    SystemStatus,
    TorrentFilter,
    TorrentInfo,
    UserPreferences,
)


class Formatters:
    """Message formatting utilities."""

    @staticmethod
    def escape_markdown(text: str) -> str:
        """Escape special characters for MarkdownV2."""
        special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
        for char in special_chars:
            text = text.replace(char, f"\\{char}")
        return text

    @staticmethod
    def format_search_result(result: SearchResult, index: int) -> str:
        """Format a single search result for display."""
        lines = [f"**{index}. {result.title}**"]

        # Quality info
        quality_parts = []
        if result.quality.resolution:
            quality_parts.append(result.quality.resolution)
        if result.quality.source:
            quality_parts.append(result.quality.source)
        if result.quality.codec:
            quality_parts.append(result.quality.codec)
        if result.quality.hdr:
            quality_parts.append(result.quality.hdr)

        if quality_parts:
            lines.append(f"📊 Quality: {' / '.join(quality_parts)}")

        # Size
        if result.size > 0:
            lines.append(f"💾 Size: {result.size_formatted}")

        # Seeders/Leechers
        if result.protocol == "torrent":
            seeder_info = []
            if result.seeders is not None:
                seeder_info.append(f"S: {result.seeders}")
            if result.leechers is not None:
                seeder_info.append(f"L: {result.leechers}")
            if seeder_info:
                lines.append(f"🌱 {' | '.join(seeder_info)}")

        # Indexer and score
        lines.append(f"🔍 {result.indexer} | Score: {result.calculated_score}")

        return "\n".join(lines)

    @staticmethod
    def format_search_results_page(
        results: list[SearchResult],
        page: int,
        total_pages: int,
        query: str,
        content_type: ContentType,
    ) -> str:
        """Format a page of search results."""
        type_emoji = "🎬" if content_type == ContentType.MOVIE else "📺"
        header = f"{type_emoji} **Результаты поиска:** `{query}`\n"
        header += f"Стр. {page + 1}/{total_pages} | Показано: {len(results)}\n\n"

        result_texts = []
        for i, result in enumerate(results):
            result_texts.append(Formatters.format_search_result(result, i + 1 + (page * len(results))))

        return header + "\n\n".join(result_texts)

    @staticmethod
    def format_release_details(result: SearchResult) -> str:
        """Format detailed view of a release."""
        lines = [f"**{result.title}**\n"]

        # Quality
        lines.append("**📊 Качество:**")
        if result.quality.resolution:
            lines.append(f"  • Разрешение: {result.quality.resolution}")
        if result.quality.source:
            lines.append(f"  • Источник: {result.quality.source}")
        if result.quality.codec:
            lines.append(f"  • Кодек: {result.quality.codec}")
        if result.quality.hdr:
            lines.append(f"  • HDR: {result.quality.hdr}")
        if result.quality.audio:
            lines.append(f"  • Аудио: {result.quality.audio}")
        if result.quality.is_remux:
            lines.append("  • 📀 REMUX")
        if result.quality.is_repack:
            lines.append("  • 🔄 REPACK")

        lines.append("")

        # Size and protocol
        lines.append(f"💾 **Размер:** {result.size_formatted}")
        lines.append(f"📡 **Протокол:** {result.protocol.upper()}")

        # Torrent info
        if result.protocol == "torrent":
            if result.seeders is not None:
                lines.append(f"🌱 **Сиды:** {result.seeders}")
            if result.leechers is not None:
                lines.append(f"📥 **Личи:** {result.leechers}")

        # Indexer
        lines.append(f"🔍 **Индексатор:** {result.indexer}")

        # Score
        lines.append(f"\n**Оценка:** {result.calculated_score}/100")

        # Season/episode info
        if result.detected_season is not None:
            season_info = f"Сезон {result.detected_season}"
            if result.detected_episode is not None:
                season_info += f" Серия {result.detected_episode}"
            if result.is_season_pack:
                season_info += " (сезон целиком)"
            lines.append(f"📅 {season_info}")

        # Publish date
        if result.publish_date:
            date_str = result.publish_date.strftime("%d.%m.%Y %H:%M")
            lines.append(f"📆 **Опубликовано:** {date_str}")

        return "\n".join(lines)

    @staticmethod
    def format_movie_info(movie: MovieInfo, compact: bool = False) -> str:
        """Format movie information."""
        if compact:
            return f"🎬 **{movie.title}** ({movie.year})"

        lines = [f"🎬 **{movie.title}** ({movie.year})"]

        if movie.original_title and movie.original_title != movie.title:
            lines.append(f"_Оригинал: {movie.original_title}_")

        if movie.runtime:
            lines.append(f"⏱ Длительность: {movie.runtime} мин")

        if movie.genres:
            lines.append(f"🎭 Жанры: {', '.join(movie.genres[:5])}")

        if movie.studio:
            lines.append(f"🏢 Студия: {movie.studio}")

        if movie.overview:
            overview = movie.overview[:300]
            if len(movie.overview) > 300:
                overview += "..."
            lines.append(f"\n📝 {overview}")

        # Status in Radarr
        if movie.radarr_id:
            status = "✅ В библиотеке"
            if movie.has_file:
                status += " (скачан)"
            lines.append(f"\n{status}")

        return "\n".join(lines)

    @staticmethod
    def format_series_info(series: SeriesInfo, compact: bool = False) -> str:
        """Format series information."""
        if compact:
            year_str = f" ({series.year})" if series.year else ""
            return f"📺 **{series.title}**{year_str}"

        lines = [f"📺 **{series.title}**"]

        if series.year:
            lines[0] += f" ({series.year})"

        if series.original_title and series.original_title != series.title:
            lines.append(f"_Оригинал: {series.original_title}_")

        if series.network:
            lines.append(f"📡 Канал: {series.network}")

        if series.status:
            status_emoji = "🟢" if series.status.lower() == "continuing" else "🔴"
            status_text = "Выходит" if series.status.lower() == "continuing" else "Завершён"
            lines.append(f"{status_emoji} Статус: {status_text}")

        lines.append(f"📊 Сезонов: {series.season_count} | Серий: {series.total_episode_count}")

        if series.runtime:
            lines.append(f"⏱ Длительность: ~{series.runtime} мин/серия")

        if series.genres:
            lines.append(f"🎭 Жанры: {', '.join(series.genres[:5])}")

        if series.overview:
            overview = series.overview[:300]
            if len(series.overview) > 300:
                overview += "..."
            lines.append(f"\n📝 {overview}")

        # Status in Sonarr
        if series.sonarr_id:
            lines.append("\n✅ В библиотеке")

        return "\n".join(lines)

    @staticmethod
    def format_system_status(statuses: list[SystemStatus]) -> str:
        """Format system status information."""
        lines = ["**🔌 Статус сервисов**\n"]

        for status in statuses:
            if status.available:
                emoji = "✅"
                version_str = f" v{status.version}" if status.version else ""
                time_str = f" ({status.response_time_ms}мс)" if status.response_time_ms else ""
                lines.append(f"{emoji} **{status.service}**{version_str}{time_str}")
            else:
                emoji = "❌"
                error_str = f": {status.error}" if status.error else ""
                lines.append(f"{emoji} **{status.service}**{error_str}")

        return "\n".join(lines)

    @staticmethod
    def format_user_preferences(
        prefs: UserPreferences,
        radarr_profiles: list[QualityProfile],
        radarr_folders: list[RootFolder],
        sonarr_profiles: list[QualityProfile],
        sonarr_folders: list[RootFolder],
    ) -> str:
        """Format user preferences for settings display."""
        lines = ["**⚙️ Ваши настройки**\n"]

        # Radarr settings
        lines.append("**🎬 Radarr (фильмы):**")
        rp = next((p for p in radarr_profiles if p.id == prefs.radarr_quality_profile_id), None)
        lines.append(f"  Профиль: {rp.name if rp else 'Не выбран'}")
        rf = next((f for f in radarr_folders if f.id == prefs.radarr_root_folder_id), None)
        lines.append(f"  Папка: {rf.path if rf else 'Не выбрана'}")

        # Sonarr settings
        lines.append("\n**📺 Sonarr (сериалы):**")
        sp = next((p for p in sonarr_profiles if p.id == prefs.sonarr_quality_profile_id), None)
        lines.append(f"  Профиль: {sp.name if sp else 'Не выбран'}")
        sf = next((f for f in sonarr_folders if f.id == prefs.sonarr_root_folder_id), None)
        lines.append(f"  Папка: {sf.path if sf else 'Не выбрана'}")

        # General preferences
        lines.append("\n**🎯 Общие:**")
        lines.append(f"  Качество: {prefs.preferred_resolution or 'Любое'}")
        lines.append(f"  Авто-граб: {'ВКЛ ✓' if prefs.auto_grab_enabled else 'ВЫКЛ'}")

        return "\n".join(lines)

    @staticmethod
    def format_action_log(actions: list[ActionLog], limit: int = 10) -> str:
        """Format action history."""
        if not actions:
            return "📭 История пуста."

        lines = ["**📋 Последние действия**\n"]

        for action in actions[:limit]:
            emoji = "✅" if action.success else "❌"
            type_emoji = "🎬" if action.content_type == ContentType.MOVIE else "📺"

            action_str = action.action_type.value.upper()
            title = action.content_title or action.query or "Неизвестно"

            if len(title) > 30:
                title = title[:27] + "..."

            date_str = action.created_at.strftime("%d.%m %H:%M")

            lines.append(f"{emoji} {type_emoji} {action_str}: {title} ({date_str})")

            if not action.success and action.error_message:
                error = action.error_message[:50]
                lines.append(f"   ↳ Ошибка: {error}")

        return "\n".join(lines)

    @staticmethod
    def format_help() -> str:
        """Format help message."""
        return """**🤖 TG\\_arr — Справка**

**📌 Команды:**
• `/search` — поиск фильмов и сериалов
• `/movie` — поиск только фильмов
• `/series` — поиск только сериалов
• `/downloads` — активные загрузки
• `/qstatus` — статус qBittorrent
• `/settings` — настройки
• `/status` — статус сервисов
• `/history` — история действий

**💡 Примеры поиска:**
• `Дюна 2021` — поиск фильма
• `Breaking Bad S02` — 2 сезон сериала
• `1080p remux` — в названии

**⚡ Советы:**
• Просто напишите название для поиска
• Используйте `/settings` для качества по умолчанию
• Включите авто-граб для быстрой загрузки лучших релизов
"""

    @staticmethod
    def format_error(error: str, include_retry: bool = True) -> str:
        """Format error message."""
        msg = f"❌ **Ошибка:** {error}"
        if include_retry:
            msg += "\n\nПопробуйте ещё раз или /cancel для отмены."
        return msg

    @staticmethod
    def format_success(message: str) -> str:
        """Format success message."""
        return f"✅ {message}"

    @staticmethod
    def format_warning(message: str) -> str:
        """Format warning message."""
        return f"⚠️ {message}"

    @staticmethod
    def format_info(message: str) -> str:
        """Format info message."""
        return f"ℹ️ {message}"

    # =========================================================================
    # qBittorrent / Torrent Formatters
    # =========================================================================

    @staticmethod
    def format_qbittorrent_status(status: QBittorrentStatus) -> str:
        """Format qBittorrent global status."""
        lines = ["**📊 Статус qBittorrent**\n"]

        # Version and connection
        lines.append(f"🖥 **Версия:** {status.version}")
        conn_emoji = "🟢" if status.connection_status == "connected" else "🔴"
        conn_text = "подключён" if status.connection_status == "connected" else status.connection_status
        lines.append(f"{conn_emoji} **Соединение:** {conn_text}")

        lines.append("")

        # Transfer speeds
        lines.append("**📡 Скорость:**")
        lines.append(f"  ⬇️ Загрузка: {status.download_speed_formatted}")
        lines.append(f"  ⬆️ Отдача: {status.upload_speed_formatted}")

        # Limits
        if status.download_limit > 0 or status.upload_limit > 0:
            from bot.models import format_speed
            dl_limit = format_speed(status.download_limit) if status.download_limit > 0 else "∞"
            ul_limit = format_speed(status.upload_limit) if status.upload_limit > 0 else "∞"
            lines.append(f"  📉 Лимиты: ⬇️ {dl_limit} | ⬆️ {ul_limit}")

        lines.append("")

        # Torrents
        lines.append("**📋 Торренты:**")
        lines.append(f"  Всего: {status.total_torrents}")
        lines.append(f"  Активных: ⬇️ {status.active_downloads} | ⬆️ {status.active_uploads}")
        if status.paused_torrents > 0:
            lines.append(f"  На паузе: {status.paused_torrents}")

        lines.append("")

        # Disk
        lines.append(f"💾 **Свободно:** {status.free_space_formatted}")

        # DHT
        if status.dht_nodes > 0:
            lines.append(f"🌐 **DHT узлов:** {status.dht_nodes}")

        return "\n".join(lines)

    @staticmethod
    def format_torrent_list(
        torrents: list[TorrentInfo],
        page: int,
        total_pages: int,
        current_filter: TorrentFilter,
        total_count: int,
    ) -> str:
        """Format torrent list header."""
        filter_names = {
            TorrentFilter.ALL: "Все",
            TorrentFilter.DOWNLOADING: "Загружаются",
            TorrentFilter.SEEDING: "Раздаются",
            TorrentFilter.COMPLETED: "Завершены",
            TorrentFilter.PAUSED: "На паузе",
            TorrentFilter.ACTIVE: "Активные",
            TorrentFilter.INACTIVE: "Неактивные",
            TorrentFilter.STALLED: "Застряли",
            TorrentFilter.ERRORED: "С ошибками",
        }

        filter_name = filter_names.get(current_filter, "Все")
        header = f"**📥 Загрузки** — {filter_name}\n"
        header += f"Показано {len(torrents)} из {total_count}"

        if total_pages > 1:
            header += f" (стр. {page + 1}/{total_pages})"

        return header

    @staticmethod
    def format_torrent_details(torrent: TorrentInfo) -> str:
        """Format detailed view of a torrent."""
        lines = [f"**{torrent.name}**\n"]

        # State and progress
        state_names = {
            "downloading": "Загрузка",
            "seeding": "Раздача",
            "completed": "Завершён",
            "paused": "Пауза",
            "queued": "В очереди",
            "checking": "Проверка",
            "stalled": "Застрял",
            "error": "Ошибка",
            "moving": "Перемещение",
            "unknown": "Неизвестно",
        }
        state_text = state_names.get(torrent.state.value, torrent.state.value)
        lines.append(f"{torrent.state_emoji} **Статус:** {state_text}")
        lines.append(f"📊 **Прогресс:** {torrent.progress_percent}%")

        # Progress bar
        progress_bar = Formatters._progress_bar(torrent.progress)
        lines.append(f"`{progress_bar}`")

        lines.append("")

        # Size info
        from bot.models import format_bytes
        downloaded = format_bytes(torrent.downloaded)
        lines.append(f"💾 **Размер:** {downloaded} / {torrent.size_formatted}")

        # Speeds
        if torrent.download_speed > 0 or torrent.upload_speed > 0:
            lines.append(f"⬇️ **Загрузка:** {torrent.download_speed_formatted}")
            lines.append(f"⬆️ **Отдача:** {torrent.upload_speed_formatted}")

        # ETA
        if torrent.eta is not None and torrent.eta > 0 and torrent.progress < 1.0:
            lines.append(f"⏱ **Осталось:** {torrent.eta_formatted}")

        lines.append("")

        # Peers
        lines.append("**🌐 Пиры:**")
        lines.append(f"  Сиды: {torrent.seeds} (всего {torrent.seeds_total})")
        lines.append(f"  Личи: {torrent.peers} (всего {torrent.peers_total})")

        # Ratio
        lines.append(f"\n📈 **Рейтинг:** {torrent.ratio:.2f}")

        # Category and tags
        if torrent.category:
            lines.append(f"📁 **Категория:** {torrent.category}")
        if torrent.tags:
            lines.append(f"🏷 **Теги:** {', '.join(torrent.tags)}")

        # Save path
        lines.append(f"\n📂 **Путь:** `{torrent.save_path}`")

        # Dates
        if torrent.added_on:
            lines.append(f"📅 **Добавлен:** {torrent.added_on.strftime('%d.%m.%Y %H:%M')}")
        if torrent.completion_on and torrent.progress >= 1.0:
            lines.append(f"✅ **Завершён:** {torrent.completion_on.strftime('%d.%m.%Y %H:%M')}")

        return "\n".join(lines)

    @staticmethod
    def format_torrent_compact(torrent: TorrentInfo) -> str:
        """Format compact single-line torrent info."""
        name = torrent.name[:30] + "..." if len(torrent.name) > 33 else torrent.name
        return f"{torrent.state_emoji} {torrent.progress_percent}% | {name}"

    @staticmethod
    def _progress_bar(progress: float, length: int = 20) -> str:
        """Create a text-based progress bar."""
        filled = int(length * progress)
        empty = length - filled
        return "█" * filled + "░" * empty

    @staticmethod
    def format_download_complete_notification(torrent: TorrentInfo) -> str:
        """Format notification message for completed download."""
        lines = ["✅ **Загрузка завершена!**\n"]
        lines.append(f"📥 **{torrent.name}**")
        lines.append(f"💾 Размер: {torrent.size_formatted}")
        lines.append(f"📂 Путь: `{torrent.save_path}`")

        if torrent.completion_on:
            lines.append(f"⏱ Завершено: {torrent.completion_on.strftime('%d.%m.%Y %H:%M')}")

        return "\n".join(lines)

    @staticmethod
    def format_no_torrents(current_filter: TorrentFilter) -> str:
        """Format message when no torrents match the filter."""
        if current_filter == TorrentFilter.ALL:
            return "📭 Торрентов нет.\n\nИспользуйте /search для поиска контента."

        filter_names = {
            TorrentFilter.DOWNLOADING: "загружаемых",
            TorrentFilter.SEEDING: "раздаваемых",
            TorrentFilter.COMPLETED: "завершённых",
            TorrentFilter.PAUSED: "приостановленных",
            TorrentFilter.ACTIVE: "активных",
            TorrentFilter.STALLED: "застрявших",
            TorrentFilter.ERRORED: "с ошибками",
        }

        filter_name = filter_names.get(current_filter, "подходящих")
        return f"📭 Нет {filter_name} торрентов.\n\nПопробуйте другой фильтр."

    @staticmethod
    def format_speed_limit_changed(limit_type: str, speed_kb: int) -> str:
        """Format message for speed limit change."""
        if speed_kb == 0:
            speed_str = "без ограничений"
        else:
            from bot.models import format_speed
            speed_str = format_speed(speed_kb * 1024)

        direction = "Загрузка" if limit_type == "dl" else "Отдача"
        return f"✅ {direction}: {speed_str}"

    @staticmethod
    def format_torrent_action(action: str, torrent_name: str, success: bool = True) -> str:
        """Format message for torrent action result."""
        name = torrent_name[:40] + "..." if len(torrent_name) > 43 else torrent_name

        if success:
            action_messages = {
                "pause": f"⏸ Пауза: {name}",
                "resume": f"▶️ Возобновлён: {name}",
                "delete": f"🗑 Удалён: {name}",
                "delete_files": f"🗑 Удалён с файлами: {name}",
            }
            return action_messages.get(action, f"✅ {action}: {name}")
        else:
            return f"❌ Ошибка {action}: {name}"

    @staticmethod
    def format_bulk_action(action: str, count: int) -> str:
        """Format message for bulk torrent action."""
        action_messages = {
            "pause": f"⏸ Приостановлено: {count} торрентов",
            "resume": f"▶️ Возобновлено: {count} торрентов",
        }
        return action_messages.get(action, f"✅ {action}: {count} торрентов")

    # =========================================================================
    # Emby Formatters
    # =========================================================================

    @staticmethod
    def format_emby_status(
        server_name: str,
        version: str,
        operating_system: str,
        has_pending_restart: bool,
        has_update_available: bool,
        active_sessions: int = 0,
        libraries: list = None,
    ) -> str:
        """Format Emby server status."""
        lines = ["**📺 Emby Media Server**\n"]

        lines.append(f"🏷 **Сервер:** {server_name}")
        lines.append(f"🖥 **Версия:** {version}")
        lines.append(f"💻 **ОС:** {operating_system}")

        lines.append("")

        # Status indicators
        if has_update_available:
            lines.append("⬆️ **Доступно обновление!**")

        if has_pending_restart:
            lines.append("🔄 **Требуется перезагрузка**")

        if active_sessions > 0:
            lines.append(f"👥 **Активных сессий:** {active_sessions}")

        if libraries:
            lines.append("")
            lines.append("**📚 Библиотеки:**")
            for lib in libraries:
                lib_emoji = "🎬" if lib.collection_type == "movies" else "📺" if lib.collection_type == "tvshows" else "📁"
                lines.append(f"  {lib_emoji} {lib.name}")

        return "\n".join(lines)

    @staticmethod
    def format_emby_action(action: str, success: bool = True, error: str = None) -> str:
        """Format Emby action result."""
        if success:
            messages = {
                "scan_all": "✅ Сканирование всех библиотек запущено",
                "scan_movies": "✅ Сканирование фильмов запущено",
                "scan_series": "✅ Сканирование сериалов запущено",
                "restart": "🔁 Сервер перезагружается...",
                "update": "⬆️ Обновление устанавливается...",
            }
            return messages.get(action, f"✅ {action}")
        else:
            return f"❌ Ошибка: {error or action}"

    @staticmethod
    def _get_rating(ratings: dict) -> Optional[float]:
        """Extract rating value from ratings dict."""
        if not ratings:
            return None
        # Try TMDb first, then other sources
        for source in ["tmdb", "imdb", "rottenTomatoes"]:
            if source in ratings and "value" in ratings[source]:
                return ratings[source]["value"]
        return None

    @staticmethod
    def format_trending_movies(movies: list) -> str:
        """Format trending movies list."""
        lines = [
            "🔥 <b>Топ популярных фильмов</b>\n",
            "<i>По данным TMDb (The Movie Database)</i>\n",
        ]

        for i, movie in enumerate(movies[:10], 1):
            rating_value = Formatters._get_rating(movie.ratings)
            rating = f"⭐ {rating_value:.1f}" if rating_value else ""
            year = f" ({movie.year})" if movie.year else ""
            title = html.escape(movie.title)

            lines.append(f"{i}. <b>{title}</b>{year}")
            if rating:
                lines.append(f"   {rating}")
            if movie.overview:
                # Truncate overview to 100 chars
                overview = movie.overview[:100] + "..." if len(movie.overview) > 100 else movie.overview
                overview = html.escape(overview)
                lines.append(f"   <i>{overview}</i>")
            lines.append("")

        lines.append("\n💡 Нажмите на фильм чтобы увидеть постер")
        return "\n".join(lines)

    @staticmethod
    def format_trending_series(series_list: list) -> str:
        """Format trending series list."""
        lines = [
            "🔥 <b>Топ популярных сериалов</b>\n",
            "<i>По данным TMDb (The Movie Database)</i>\n",
        ]

        for i, series in enumerate(series_list[:10], 1):
            rating_value = Formatters._get_rating(series.ratings)
            rating = f"⭐ {rating_value:.1f}" if rating_value else ""
            year = f" ({series.year})" if series.year else ""
            title = html.escape(series.title)

            lines.append(f"{i}. <b>{title}</b>{year}")
            if rating:
                lines.append(f"   {rating}")
            if series.overview:
                # Truncate overview to 100 chars
                overview = series.overview[:100] + "..." if len(series.overview) > 100 else series.overview
                overview = html.escape(overview)
                lines.append(f"   <i>{overview}</i>")
            lines.append("")

        lines.append("\n💡 Нажмите на сериал чтобы увидеть постер")
        return "\n".join(lines)

    @staticmethod
    def format_movie_with_poster(movie) -> str:
        """Format movie details for display with poster."""
        rating_value = Formatters._get_rating(movie.ratings)
        rating = f"⭐ {rating_value:.1f}/10" if rating_value else "Нет рейтинга"
        year = f" ({movie.year})" if movie.year else ""
        title = html.escape(movie.title)

        lines = [
            f"🎬 <b>{title}</b>{year}\n",
            f"{rating}",
        ]

        if movie.overview:
            lines.append(f"\n{html.escape(movie.overview)}")

        lines.append("\n💡 Нажмите кнопку ниже для добавления в Radarr")
        return "\n".join(lines)

    @staticmethod
    def format_series_with_poster(series) -> str:
        """Format series details for display with poster."""
        rating_value = Formatters._get_rating(series.ratings)
        rating = f"⭐ {rating_value:.1f}/10" if rating_value else "Нет рейтинга"
        year = f" ({series.year})" if series.year else ""
        title = html.escape(series.title)

        lines = [
            f"📺 <b>{title}</b>{year}\n",
            f"{rating}",
        ]

        if series.network:
            lines.append(f"📡 {html.escape(series.network)}")

        if series.overview:
            lines.append(f"\n{html.escape(series.overview)}")

        lines.append("\n💡 Нажмите кнопку ниже для добавления в Sonarr")
        return "\n".join(lines)

    # =========================================================================
    # Calendar Formatters
    # =========================================================================

    @staticmethod
    def format_calendar_events(
        events: list,
        days: int,
        content_filter: Optional[str] = None,
    ) -> str:
        """Format calendar events list."""
        filter_text = ""
        if content_filter == "movie":
            filter_text = " (фильмы)"
        elif content_filter == "series":
            filter_text = " (сериалы)"

        lines = [
            f"📅 <b>Предстоящие релизы{filter_text}</b>",
            f"<i>Ближайшие {days} дней</i>\n",
        ]

        if not events:
            lines.append("📭 Нет предстоящих релизов.")
            return "\n".join(lines)

        # Group events by date
        events_by_date: dict = {}
        for event in events:
            date_key = event.release_date.strftime("%d.%m.%Y")
            if date_key not in events_by_date:
                events_by_date[date_key] = []
            events_by_date[date_key].append(event)

        for date_str, date_events in events_by_date.items():
            # Calculate relative day
            first_event = date_events[0]
            days_until = first_event.days_until_release

            if days_until == 0:
                day_label = "Сегодня"
            elif days_until == 1:
                day_label = "Завтра"
            elif days_until < 0:
                day_label = "Вышел"
            else:
                day_label = f"Через {days_until} дн."

            lines.append(f"\n<b>{date_str}</b> ({day_label}):")

            for event in date_events[:5]:  # Limit per day
                emoji = "🎬" if event.event_type.value == "movie" else "📺"
                title = html.escape(event.display_title)

                if event.event_type.value == "episode" and event.episode_title:
                    ep_title = html.escape(event.episode_title[:30])
                    lines.append(f"  {emoji} {title}")
                    lines.append(f"      <i>{ep_title}</i>")
                else:
                    lines.append(f"  {emoji} {title}")

        return "\n".join(lines)

    @staticmethod
    def format_release_notification(event) -> str:
        """Format notification message for upcoming release."""
        if event.event_type.value == "movie":
            emoji = "🎬"
            title = event.title
            year = f" ({event.year})" if event.year else ""

            if event.days_until_release == 0:
                header = "Сегодня выходит!"
            elif event.days_until_release == 1:
                header = "Завтра выходит!"
            else:
                header = f"Выходит через {event.days_until_release} дн."

            lines = [
                f"🔔 <b>{header}</b>\n",
                f"{emoji} <b>{html.escape(title)}</b>{year}",
                f"📅 {event.release_date_formatted}",
            ]
        else:  # Episode
            emoji = "📺"
            series_title = event.series_title or event.title
            ep_str = f"S{event.season_number:02d}E{event.episode_number:02d}"

            if event.days_until_release == 0:
                header = "Новый эпизод сегодня!"
            elif event.days_until_release == 1:
                header = "Новый эпизод завтра!"
            else:
                header = f"Новый эпизод через {event.days_until_release} дн."

            lines = [
                f"🔔 <b>{header}</b>\n",
                f"{emoji} <b>{html.escape(series_title)}</b>",
                f"📺 {ep_str}",
            ]

            if event.episode_title:
                lines.append(f"<i>{html.escape(event.episode_title)}</i>")

            lines.append(f"📅 {event.release_date_formatted}")

        return "\n".join(lines)
