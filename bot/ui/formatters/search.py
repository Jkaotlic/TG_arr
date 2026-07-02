"""Search-result and library-content (movie/series/artist) formatters, plus
generic status/preferences/action-log/message formatters that don't belong
to a more specific domain (torrent, emby, calendar).
"""

from bot.models import (
    ActionLog,
    ArtistInfo,
    ContentType,
    MovieInfo,
    QualityProfile,
    RootFolder,
    SearchResult,
    SeriesInfo,
    SystemStatus,
    UserPreferences,
)
from bot.ui.formatters._common import _e, _safe_truncate, _to_local


class _SearchFormatters:
    """Search / content-info / status / preferences formatting mixin."""

    # BUG-11: cap an individual release title before rendering — some indexers
    # (notably RuTracker) return 300+ char titles; 5 of those on one page can
    # blow past Telegram's 4096-char message cap and the search silently
    # "fails" (MESSAGE_TOO_LONG).
    _MAX_RESULT_TITLE_LEN = 150

    @staticmethod
    def format_search_result(result: SearchResult, index: int) -> str:
        """Format a single search result for display."""
        title = result.title or ""
        if len(title) > _SearchFormatters._MAX_RESULT_TITLE_LEN:
            title = title[: _SearchFormatters._MAX_RESULT_TITLE_LEN - 1] + "…"
        lines = [f"<b>{index}. {_e(title)}</b>"]

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
                _SearchFormatters.format_search_result(result, i + 1 + (page * per_page))
            )

        page_text = header + "\n\n".join(result_texts)
        # BUG-11/TEST-07: hard safety net on top of per-title truncation —
        # keeps the page well under Telegram's 4096-char message limit.
        return _safe_truncate(page_text, max_len=3800)

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

        # Publish date (BUG-06: shown in the configured local timezone)
        if result.publish_date:
            date_str = _to_local(result.publish_date).strftime("%d.%m.%Y %H:%M")
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

        _TYPE_EMOJI = {
            ContentType.MOVIE: "🎬",
            ContentType.SERIES: "📺",
            ContentType.MUSIC: "🎵",  # LOGIC-22: music actions used to show the series emoji
        }

        for action in actions[:limit]:
            emoji = "✅" if action.success else "❌"
            type_emoji = _TYPE_EMOJI.get(action.content_type, "📺")

            action_str = action.action_type.value.upper()
            title = action.content_title or action.query or "Неизвестно"

            if len(title) > 30:
                title = title[:27] + "..."

            date_str = _to_local(action.created_at).strftime("%d.%m %H:%M")

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
