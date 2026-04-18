"""Message formatters for Telegram bot.

All output uses HTML parse_mode. User-provided content is escaped via html.escape().
"""

import html
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bot.models import (
    ActionLog,
    AlbumInfo,
    ArtistInfo,
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


def _e(text) -> str:
    """Escape HTML entities in user-provided text."""
    if not text:
        return ""
    return html.escape(str(text))


class Formatters:
    """Message formatting utilities — HTML mode."""

    @staticmethod
    def format_search_result(result: SearchResult, index: int) -> str:
        """Format a single search result for display."""
        lines = [f"<b>{index}. {_e(result.title)}</b>"]

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
        if result.quality.subtitle:
            quality_parts.append(f"💬{result.quality.subtitle}")

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
        lines.append(f"🔍 {_e(result.indexer)} | Score: {result.calculated_score}")

        return "\n".join(lines)

    @staticmethod
    def format_search_results_page(
        results: list[SearchResult],
        page: int,
        total_pages: int,
        query: str,
        content_type: ContentType,
        per_page: int = 5,
    ) -> str:
        """Format a page of search results."""
        type_emoji = "🎬" if content_type == ContentType.MOVIE else "📺"
        header = f"{type_emoji} <b>Результаты поиска:</b> <code>{_e(query)}</code>\n"
        header += f"Стр. {page + 1}/{total_pages} | Показано: {len(results)}\n\n"

        result_texts = []
        for i, result in enumerate(results):
            result_texts.append(
                Formatters.format_search_result(result, i + 1 + (page * per_page))
            )

        return header + "\n\n".join(result_texts)

    @staticmethod
    def format_release_details(result: SearchResult) -> str:
        """Format detailed view of a release."""
        lines = [f"<b>{_e(result.title)}</b>\n"]

        # Quality
        lines.append("<b>📊 Качество:</b>")
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
        if result.quality.subtitle:
            lines.append(f"  • 💬 Субтитры: {result.quality.subtitle}")

        lines.append("")

        # Size and protocol
        lines.append(f"💾 <b>Размер:</b> {result.size_formatted}")
        lines.append(f"📡 <b>Протокол:</b> {result.protocol.upper()}")

        # Torrent info
        if result.protocol == "torrent":
            if result.seeders is not None:
                lines.append(f"🌱 <b>Сиды:</b> {result.seeders}")
            if result.leechers is not None:
                lines.append(f"📥 <b>Личи:</b> {result.leechers}")

        # Indexer
        lines.append(f"🔍 <b>Индексатор:</b> {_e(result.indexer)}")

        # Score
        lines.append(f"\n<b>Оценка:</b> {result.calculated_score}/100")

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
            lines.append(f"📆 <b>Опубликовано:</b> {date_str}")

        return "\n".join(lines)

    @staticmethod
    def format_movie_info(movie: MovieInfo, compact: bool = False) -> str:
        """Format movie information."""
        if compact:
            return f"🎬 <b>{_e(movie.title)}</b> ({movie.year})"

        lines = [f"🎬 <b>{_e(movie.title)}</b> ({movie.year})"]

        if movie.original_title and movie.original_title != movie.title:
            lines.append(f"<i>Оригинал: {_e(movie.original_title)}</i>")

        if movie.runtime:
            lines.append(f"⏱ Длительность: {movie.runtime} мин")

        if movie.genres:
            lines.append(f"🎭 Жанры: {_e(', '.join(movie.genres[:5]))}")

        if movie.studio:
            lines.append(f"🏢 Студия: {_e(movie.studio)}")

        if movie.overview:
            overview = movie.overview[:300]
            if len(movie.overview) > 300:
                overview += "..."
            lines.append(f"\n📝 {_e(overview)}")

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
            return f"📺 <b>{_e(series.title)}</b>{year_str}"

        lines = [f"📺 <b>{_e(series.title)}</b>"]

        if series.year:
            lines[0] += f" ({series.year})"

        if series.original_title and series.original_title != series.title:
            lines.append(f"<i>Оригинал: {_e(series.original_title)}</i>")

        if series.network:
            lines.append(f"📡 Канал: {_e(series.network)}")

        if series.status:
            status_emoji = "🟢" if series.status.lower() == "continuing" else "🔴"
            status_text = (
                "Выходит" if series.status.lower() == "continuing" else "Завершён"
            )
            lines.append(f"{status_emoji} Статус: {status_text}")

        lines.append(
            f"📊 Сезонов: {series.season_count} | Серий: {series.total_episode_count}"
        )

        if series.runtime:
            lines.append(f"⏱ Длительность: ~{series.runtime} мин/серия")

        if series.genres:
            lines.append(f"🎭 Жанры: {_e(', '.join(series.genres[:5]))}")

        if series.overview:
            overview = series.overview[:300]
            if len(series.overview) > 300:
                overview += "..."
            lines.append(f"\n📝 {_e(overview)}")

        # Status in Sonarr
        if series.sonarr_id:
            lines.append("\n✅ В библиотеке")

        return "\n".join(lines)

    @staticmethod
    def format_artist_info(artist: ArtistInfo, compact: bool = False) -> str:
        """Format artist information for display."""
        name_line = f"🎵 <b>{_e(artist.name)}</b>"
        if artist.disambiguation:
            name_line += f" <i>[{_e(artist.disambiguation)}]</i>"

        if compact:
            return name_line

        lines = [name_line]

        if artist.artist_type:
            lines.append(f"🧑 Тип: {_e(artist.artist_type)}")
        if artist.status:
            status_emoji = "🟢" if artist.status.lower() == "active" else "🔴"
            lines.append(f"{status_emoji} Статус: {_e(artist.status)}")
        if artist.genres:
            lines.append(f"🎭 Жанры: {_e(', '.join(artist.genres[:5]))}")
        if artist.album_count:
            lines.append(f"💿 Альбомов: {artist.album_count} | Треков: {artist.track_count}")
        if artist.overview:
            overview = artist.overview[:300]
            if len(artist.overview) > 300:
                overview += "..."
            lines.append(f"\n📝 {_e(overview)}")

        if artist.lidarr_id:
            lines.append("\n✅ В библиотеке")

        return "\n".join(lines)

    @staticmethod
    def format_album_info(album: AlbumInfo, compact: bool = False) -> str:
        """Format album information."""
        artist_str = f" — {_e(album.artist_name)}" if album.artist_name else ""
        year_str = f" ({album.year})" if album.year else ""
        header = f"💿 <b>{_e(album.title)}</b>{artist_str}{year_str}"

        if compact:
            return header

        lines = [header]
        if album.album_type:
            lines.append(f"📂 Тип: {_e(album.album_type)}")
        if album.track_count:
            duration_min = album.duration_ms // 60000
            if duration_min:
                lines.append(f"🎵 Треков: {album.track_count} | ⏱ {duration_min} мин")
            else:
                lines.append(f"🎵 Треков: {album.track_count}")
        if album.genres:
            lines.append(f"🎭 Жанры: {_e(', '.join(album.genres[:5]))}")
        if album.overview:
            overview = album.overview[:300]
            if len(album.overview) > 300:
                overview += "..."
            lines.append(f"\n📝 {_e(overview)}")

        if album.has_file:
            lines.append("\n✅ Скачан")

        return "\n".join(lines)

    @staticmethod
    def format_trending_artists(artists: list[dict]) -> str:
        """Format trending artists list (from Deezer)."""
        lines = [
            "🎵 <b>Топ популярных артистов</b>\n",
            "<i>По данным Deezer</i>\n",
        ]
        for i, a in enumerate(artists[:10], 1):
            name = _e(a.get("name", "Unknown"))
            fans = a.get("fans")
            fans_str = f" 👥 {fans:,}" if isinstance(fans, int) and fans > 0 else ""
            lines.append(f"{i}. <b>{name}</b>{fans_str}")
        lines.append("\n💡 Нажмите на артиста чтобы посмотреть и добавить в Lidarr")
        return "\n".join(lines)

    @staticmethod
    def format_system_status(statuses: list[SystemStatus]) -> str:
        """Format system status information."""
        lines = ["<b>🔌 Статус сервисов</b>\n"]

        for status in statuses:
            if status.available:
                emoji = "✅"
                version_str = f" v{_e(status.version)}" if status.version else ""
                time_str = (
                    f" ({status.response_time_ms}мс)" if status.response_time_ms else ""
                )
                lines.append(
                    f"{emoji} <b>{_e(status.service)}</b>{version_str}{time_str}"
                )
            else:
                emoji = "❌"
                error_str = f": {_e(status.error)}" if status.error else ""
                lines.append(f"{emoji} <b>{_e(status.service)}</b>{error_str}")

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
        lines = ["<b>⚙️ Ваши настройки</b>\n"]

        # Radarr settings
        lines.append("<b>🎬 Radarr (фильмы):</b>")
        rp = next(
            (p for p in radarr_profiles if p.id == prefs.radarr_quality_profile_id),
            None,
        )
        lines.append(f"  Профиль: {_e(rp.name) if rp else 'Не выбран'}")
        rf = next(
            (f for f in radarr_folders if f.id == prefs.radarr_root_folder_id), None
        )
        lines.append(f"  Папка: {_e(rf.path) if rf else 'Не выбрана'}")

        # Sonarr settings
        lines.append("\n<b>📺 Sonarr (сериалы):</b>")
        sp = next(
            (p for p in sonarr_profiles if p.id == prefs.sonarr_quality_profile_id),
            None,
        )
        lines.append(f"  Профиль: {_e(sp.name) if sp else 'Не выбран'}")
        sf = next(
            (f for f in sonarr_folders if f.id == prefs.sonarr_root_folder_id), None
        )
        lines.append(f"  Папка: {_e(sf.path) if sf else 'Не выбрана'}")

        # General preferences
        lines.append("\n<b>🎯 Общие:</b>")
        lines.append(f"  Качество: {prefs.preferred_resolution or 'Любое'}")
        lines.append(
            f"  Авто-граб: {'ВКЛ ✓' if prefs.auto_grab_enabled else 'ВЫКЛ'}"
        )

        return "\n".join(lines)

    @staticmethod
    def format_action_log(actions: list[ActionLog], limit: int = 20) -> str:
        """Format action history."""
        if not actions:
            return "📭 История пуста."

        lines = ["<b>📋 Последние действия</b>\n"]

        for action in actions[:limit]:
            emoji = "✅" if action.success else "❌"
            type_emoji = "🎬" if action.content_type == ContentType.MOVIE else "📺"

            action_str = action.action_type.value.upper()
            title = action.content_title or action.query or "Неизвестно"

            if len(title) > 30:
                title = title[:27] + "..."

            date_str = action.created_at.strftime("%d.%m %H:%M")

            lines.append(
                f"{emoji} {type_emoji} {action_str}: {_e(title)} ({date_str})"
            )

            if not action.success and action.error_message:
                error = action.error_message[:50]
                lines.append(f"   ↳ Ошибка: {_e(error)}")

        return "\n".join(lines)

    @staticmethod
    def format_error(error: str, include_retry: bool = True) -> str:
        """Format error message."""
        msg = f"❌ <b>Ошибка:</b> {_e(error)}"
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

    # =========================================================================
    # qBittorrent / Torrent Formatters
    # =========================================================================

    @staticmethod
    def format_qbittorrent_status(status: QBittorrentStatus) -> str:
        """Format qBittorrent global status."""
        lines = ["<b>📊 Статус qBittorrent</b>\n"]

        # Version and connection
        lines.append(f"🖥 <b>Версия:</b> {_e(status.version)}")
        conn_emoji = "🟢" if status.connection_status == "connected" else "🔴"
        conn_text = (
            "подключён"
            if status.connection_status == "connected"
            else _e(status.connection_status)
        )
        lines.append(f"{conn_emoji} <b>Соединение:</b> {conn_text}")

        lines.append("")

        # Transfer speeds
        lines.append("<b>📡 Скорость:</b>")
        lines.append(f"  ⬇️ Загрузка: {status.download_speed_formatted}")
        lines.append(f"  ⬆️ Отдача: {status.upload_speed_formatted}")

        # Limits
        if status.download_limit > 0 or status.upload_limit > 0:
            from bot.models import format_speed

            dl_limit = (
                format_speed(status.download_limit)
                if status.download_limit > 0
                else "∞"
            )
            ul_limit = (
                format_speed(status.upload_limit)
                if status.upload_limit > 0
                else "∞"
            )
            lines.append(f"  📉 Лимиты: ⬇️ {dl_limit} | ⬆️ {ul_limit}")

        lines.append("")

        # Torrents
        lines.append("<b>📋 Торренты:</b>")
        lines.append(f"  Всего: {status.total_torrents}")
        lines.append(
            f"  Активных: ⬇️ {status.active_downloads} | ⬆️ {status.active_uploads}"
        )
        if status.paused_torrents > 0:
            lines.append(f"  На паузе: {status.paused_torrents}")

        lines.append("")

        # Disk
        lines.append(f"💾 <b>Свободно:</b> {status.free_space_formatted}")

        # DHT
        if status.dht_nodes > 0:
            lines.append(f"🌐 <b>DHT узлов:</b> {status.dht_nodes}")

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
        header = f"<b>📥 Загрузки</b> — {filter_name}\n"
        header += f"Показано {len(torrents)} из {total_count}"

        if total_pages > 1:
            header += f" (стр. {page + 1}/{total_pages})"

        return header

    @staticmethod
    def format_torrent_details(torrent: TorrentInfo) -> str:
        """Format detailed view of a torrent."""
        lines = [f"<b>{_e(torrent.name)}</b>\n"]

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
        lines.append(f"{torrent.state_emoji} <b>Статус:</b> {state_text}")
        lines.append(f"📊 <b>Прогресс:</b> {torrent.progress_percent}%")

        # Progress bar
        progress_bar = Formatters._progress_bar(torrent.progress)
        lines.append(f"<code>{progress_bar}</code>")

        lines.append("")

        # Size info
        from bot.models import format_bytes

        downloaded = format_bytes(torrent.downloaded)
        lines.append(f"💾 <b>Размер:</b> {downloaded} / {torrent.size_formatted}")

        # Speeds
        if torrent.download_speed > 0 or torrent.upload_speed > 0:
            lines.append(f"⬇️ <b>Загрузка:</b> {torrent.download_speed_formatted}")
            lines.append(f"⬆️ <b>Отдача:</b> {torrent.upload_speed_formatted}")

        # ETA
        if torrent.eta is not None and torrent.eta > 0 and torrent.progress < 1.0:
            lines.append(f"⏱ <b>Осталось:</b> {torrent.eta_formatted}")

        lines.append("")

        # Peers
        lines.append("<b>🌐 Пиры:</b>")
        lines.append(f"  Сиды: {torrent.seeds} (всего {torrent.seeds_total})")
        lines.append(f"  Личи: {torrent.peers} (всего {torrent.peers_total})")

        # Ratio
        lines.append(f"\n📈 <b>Рейтинг:</b> {torrent.ratio:.2f}")

        # Category and tags
        if torrent.category:
            lines.append(f"📁 <b>Категория:</b> {_e(torrent.category)}")
        if torrent.tags:
            lines.append(f"🏷 <b>Теги:</b> {_e(', '.join(torrent.tags))}")

        # Save path
        lines.append(f"\n📂 <b>Путь:</b> <code>{_e(torrent.save_path)}</code>")

        # Dates
        if torrent.added_on:
            lines.append(
                f"📅 <b>Добавлен:</b> {torrent.added_on.strftime('%d.%m.%Y %H:%M')}"
            )
        if torrent.completion_on and torrent.progress >= 1.0:
            lines.append(
                f"✅ <b>Завершён:</b> {torrent.completion_on.strftime('%d.%m.%Y %H:%M')}"
            )

        return "\n".join(lines)

    @staticmethod
    def format_torrent_compact(torrent: TorrentInfo) -> str:
        """Format compact single-line torrent info."""
        name = (
            torrent.name[:30] + "..." if len(torrent.name) > 33 else torrent.name
        )
        return f"{torrent.state_emoji} {torrent.progress_percent}% | {_e(name)}"

    @staticmethod
    def _progress_bar(progress: float, length: int = 20) -> str:
        """Create a text-based progress bar."""
        filled = int(length * progress)
        empty = length - filled
        return "█" * filled + "░" * empty

    @staticmethod
    def _safe_truncate(text: str, max_len: int = 3800) -> str:
        """Truncate text without breaking HTML tags (BUG-12).

        Strategy: if text fits, return as-is. Otherwise cut at the last
        newline before ``max_len``. If no newline exists in the budget,
        fall back to a safe character-boundary cut that avoids unclosed
        ``<…>`` tags.
        """
        SUFFIX = "\n\n... (truncated)"
        if len(text) <= max_len:
            return text

        # Reserve space for the suffix
        budget = max_len - len(SUFFIX)
        if budget <= 0:
            return text[:max_len]

        candidate = text[:budget]
        cut_at = candidate.rfind("\n")
        if cut_at == -1:
            cut_at = budget

        piece = candidate[:cut_at]
        # Guard: if the piece ends inside an HTML tag ("<..." without ">"),
        # walk back to the last safe position.
        last_open = piece.rfind("<")
        last_close = piece.rfind(">")
        if last_open > last_close:
            piece = piece[:last_open]
        return piece + SUFFIX

    @staticmethod
    def format_download_complete_notification(torrent: TorrentInfo) -> str:
        """Format notification message for completed download."""
        lines = ["✅ <b>Загрузка завершена!</b>\n"]
        lines.append(f"📥 <b>{_e(torrent.name)}</b>")
        lines.append(f"💾 Размер: {torrent.size_formatted}")
        lines.append(f"📂 Путь: <code>{_e(torrent.save_path)}</code>")

        if torrent.completion_on:
            lines.append(
                f"⏱ Завершено: {torrent.completion_on.strftime('%d.%m.%Y %H:%M')}"
            )

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
    def format_torrent_action(
        action: str, torrent_name: str, success: bool = True
    ) -> str:
        """Format message for torrent action result."""
        name = (
            torrent_name[:40] + "..."
            if len(torrent_name) > 43
            else torrent_name
        )

        if success:
            action_messages = {
                "pause": f"⏸ Пауза: {_e(name)}",
                "resume": f"▶️ Возобновлён: {_e(name)}",
                "delete": f"🗑 Удалён: {_e(name)}",
                "delete_files": f"🗑 Удалён с файлами: {_e(name)}",
            }
            return action_messages.get(action, f"✅ {action}: {_e(name)}")
        else:
            return f"❌ Ошибка {action}: {_e(name)}"

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
        lines = ["<b>📺 Emby Media Server</b>\n"]

        lines.append(f"🏷 <b>Сервер:</b> {_e(server_name)}")
        lines.append(f"🖥 <b>Версия:</b> {_e(version)}")
        lines.append(f"💻 <b>ОС:</b> {_e(operating_system)}")

        lines.append("")

        # Status indicators
        if has_update_available:
            lines.append("⬆️ <b>Доступно обновление!</b>")

        if has_pending_restart:
            lines.append("🔄 <b>Требуется перезагрузка</b>")

        if active_sessions > 0:
            lines.append(f"👥 <b>Активных сессий:</b> {active_sessions}")

        if libraries:
            lines.append("")
            lines.append("<b>📚 Библиотеки:</b>")
            for lib in libraries:
                lib_emoji = (
                    "🎬"
                    if lib.collection_type == "movies"
                    else "📺"
                    if lib.collection_type == "tvshows"
                    else "📁"
                )
                lines.append(f"  {lib_emoji} {_e(lib.name)}")

        return "\n".join(lines)

    @staticmethod
    def _get_rating(ratings: dict) -> Optional[float]:
        """Extract rating value from ratings dict."""
        if not ratings:
            return None
        # Try TMDb first, then other sources
        for source in ["tmdb", "imdb", "rottenTomatoes"]:
            if source in ratings:
                val = ratings[source]
                if isinstance(val, dict) and "value" in val:
                    return val["value"]
                elif isinstance(val, (int, float)):
                    return val
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
            title = _e(movie.title)

            lines.append(f"{i}. <b>{title}</b>{year}")
            if rating:
                lines.append(f"   {rating}")
            if movie.overview:
                overview = (
                    movie.overview[:100] + "..."
                    if len(movie.overview) > 100
                    else movie.overview
                )
                lines.append(f"   <i>{_e(overview)}</i>")
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
            title = _e(series.title)

            lines.append(f"{i}. <b>{title}</b>{year}")
            if rating:
                lines.append(f"   {rating}")
            if series.overview:
                overview = (
                    series.overview[:100] + "..."
                    if len(series.overview) > 100
                    else series.overview
                )
                lines.append(f"   <i>{_e(overview)}</i>")
            lines.append("")

        lines.append("\n💡 Нажмите на сериал чтобы увидеть постер")
        return "\n".join(lines)

    @staticmethod
    def format_movie_with_poster(movie) -> str:
        """Format movie details for display with poster."""
        rating_value = Formatters._get_rating(movie.ratings)
        rating = f"⭐ {rating_value:.1f}/10" if rating_value else "Нет рейтинга"
        year = f" ({movie.year})" if movie.year else ""
        title = _e(movie.title)

        lines = [
            f"🎬 <b>{title}</b>{year}\n",
            f"{rating}",
        ]

        if movie.overview:
            overview = movie.overview
            if len(overview) > 500:
                overview = overview[:497] + "..."
            lines.append(f"\n{_e(overview)}")

        lines.append("\n💡 Нажмите кнопку ниже для добавления в Radarr")
        return "\n".join(lines)

    @staticmethod
    def format_series_with_poster(series) -> str:
        """Format series details for display with poster."""
        rating_value = Formatters._get_rating(series.ratings)
        rating = f"⭐ {rating_value:.1f}/10" if rating_value else "Нет рейтинга"
        year = f" ({series.year})" if series.year else ""
        title = _e(series.title)

        lines = [
            f"📺 <b>{title}</b>{year}\n",
            f"{rating}",
        ]

        if series.network:
            lines.append(f"📡 {_e(series.network)}")

        if series.overview:
            overview = series.overview
            if len(overview) > 500:
                overview = overview[:497] + "..."
            lines.append(f"\n{_e(overview)}")

        lines.append("\n💡 Нажмите кнопку ниже для добавления в Sonarr")
        return "\n".join(lines)

    # =========================================================================
    # Calendar / Schedule Formatting
    # =========================================================================

    @staticmethod
    def format_calendar(
        episodes: list[dict],
        movies: list[dict],
        days: int = 7,
        albums: Optional[list[dict]] = None,
    ) -> str:
        """Format combined calendar for Sonarr/Radarr/Lidarr."""
        albums = albums or []
        lines = [f"📅 <b>Календарь релизов</b> ({days} дн.)\n"]

        if not episodes and not movies and not albums:
            lines.append("Нет предстоящих релизов.")
            return "\n".join(lines)

        now = datetime.now(timezone.utc)
        today = now.date()

        if episodes:
            lines.append(f"📺 <b>Сериалы ({len(episodes)})</b>")
            by_date: dict[str, list[dict]] = {}
            for ep in episodes:
                date_key = Formatters._extract_date_key(ep.get("air_date", ""))
                by_date.setdefault(date_key, []).append(ep)

            for date_key in sorted(by_date.keys()):
                date_header = Formatters._format_date_header(date_key, today)
                lines.append(f"\n  📆 <b>{date_header}</b>")
                for ep in by_date[date_key]:
                    s = ep.get("season", 0)
                    e = ep.get("episode", 0)
                    series = _e(ep.get("series_title", "?"))
                    ep_title = _e(ep.get("title", ""))
                    status = "✅" if ep.get("has_file") else "⏳"
                    ep_label = f"S{s:02d}E{e:02d}"
                    line = f"  {status} <b>{series}</b> {ep_label}"
                    if ep_title:
                        line += f" — {ep_title}"
                    lines.append(line)

        if movies:
            if episodes:
                lines.append("")
            lines.append(f"🎬 <b>Фильмы ({len(movies)})</b>")
            by_date: dict[str, list[dict]] = {}
            for m in movies:
                date_key = Formatters._extract_date_key(m.get("release_date", ""))
                by_date.setdefault(date_key, []).append(m)

            for date_key in sorted(by_date.keys()):
                date_header = Formatters._format_date_header(date_key, today)
                lines.append(f"\n  📆 <b>{date_header}</b>")
                for m in by_date[date_key]:
                    title = _e(m.get("title", "?"))
                    year = m.get("year", "")
                    year_str = f" ({year})" if year else ""
                    status = "✅" if m.get("has_file") else ("📀" if m.get("is_available") else "⏳")
                    runtime = m.get("runtime", 0)
                    runtime_str = f" • {runtime} мин" if runtime else ""

                    release_types = []
                    if m.get("digital_release"):
                        release_types.append("💾 цифровой")
                    if m.get("physical_release"):
                        release_types.append("📀 физический")
                    if m.get("in_cinemas"):
                        release_types.append("🎥 кино")
                    type_str = f" [{', '.join(release_types)}]" if release_types else ""

                    lines.append(f"  {status} <b>{title}</b>{year_str}{runtime_str}{type_str}")

        if albums:
            if episodes or movies:
                lines.append("")
            lines.append(f"🎵 <b>Музыка ({len(albums)})</b>")
            by_date: dict[str, list[dict]] = {}
            for a in albums:
                date_key = Formatters._extract_date_key(a.get("release_date", ""))
                by_date.setdefault(date_key, []).append(a)

            for date_key in sorted(by_date.keys()):
                date_header = Formatters._format_date_header(date_key, today)
                lines.append(f"\n  📆 <b>{date_header}</b>")
                for a in by_date[date_key]:
                    artist = _e(a.get("artist_name", "?"))
                    title = _e(a.get("title", "?"))
                    album_type = a.get("album_type", "")
                    type_str = f" [{_e(album_type)}]" if album_type else ""
                    status = "✅" if a.get("has_file") else "⏳"
                    lines.append(f"  {status} <b>{artist}</b> — {title}{type_str}")

        result = "\n".join(lines)
        return Formatters._safe_truncate(result, max_len=3800)

    @staticmethod
    def _extract_date_key(date_str: str) -> str:
        """Extract sortable date key (YYYY-MM-DD) from ISO date string.

        BUG-11: parse as tz-aware and convert to the configured TIMEZONE
        so the *local* calendar day is used for grouping.
        """
        if not date_str:
            return "9999-12-31"
        try:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            try:
                from bot.config import get_settings

                tz_name = get_settings().timezone
                tz = ZoneInfo(tz_name)
            except (ZoneInfoNotFoundError, Exception):
                tz = timezone.utc
            local = dt.astimezone(tz)
            return local.strftime("%Y-%m-%d")
        except (ValueError, IndexError):
            return date_str[:10] if len(date_str) >= 10 else "9999-12-31"

    @staticmethod
    def _format_date_header(date_key: str, today) -> str:
        """Format date key to human-readable header with relative day marker."""
        months = [
            "", "января", "февраля", "марта", "апреля", "мая", "июня",
            "июля", "августа", "сентября", "октября", "ноября", "декабря",
        ]
        try:
            from datetime import date as date_cls
            parts = date_key.split("-")
            dt_date = date_cls(int(parts[0]), int(parts[1]), int(parts[2]))
        except (ValueError, IndexError):
            return date_key

        diff = (dt_date - today).days
        day_month = f"{dt_date.day} {months[dt_date.month]}"

        if diff == 0:
            return f"{day_month} — сегодня"
        elif diff == 1:
            return f"{day_month} — завтра"
        elif diff == 2:
            return f"{day_month} — послезавтра"
        elif diff == -1:
            return f"{day_month} — вчера"
        elif diff < -1:
            return f"{day_month} ({-diff} дн. назад)"
        else:
            return f"{day_month} (через {diff} дн.)"
