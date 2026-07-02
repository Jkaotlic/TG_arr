"""Emby server status and trending/poster (TMDb-backed) formatters."""

from typing import Optional

from bot.ui.formatters._common import _e


class _EmbyFormatters:
    """Emby server status and trending-content formatting mixin."""

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
            rating_value = _EmbyFormatters._get_rating(movie.ratings)
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
            rating_value = _EmbyFormatters._get_rating(series.ratings)
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
        rating_value = _EmbyFormatters._get_rating(movie.ratings)
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
        rating_value = _EmbyFormatters._get_rating(series.ratings)
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
