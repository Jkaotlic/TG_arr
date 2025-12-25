"""Message formatters for Telegram bot."""

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
        header = f"{type_emoji} **Search results for:** `{query}`\n"
        header += f"Page {page + 1}/{total_pages} | {len(results)} results shown\n\n"

        result_texts = []
        for i, result in enumerate(results):
            result_texts.append(Formatters.format_search_result(result, i + 1 + (page * len(results))))

        return header + "\n\n".join(result_texts)

    @staticmethod
    def format_release_details(result: SearchResult) -> str:
        """Format detailed view of a release."""
        lines = [f"**{result.title}**\n"]

        # Quality
        lines.append("**Quality:**")
        if result.quality.resolution:
            lines.append(f"  • Resolution: {result.quality.resolution}")
        if result.quality.source:
            lines.append(f"  • Source: {result.quality.source}")
        if result.quality.codec:
            lines.append(f"  • Codec: {result.quality.codec}")
        if result.quality.hdr:
            lines.append(f"  • HDR: {result.quality.hdr}")
        if result.quality.audio:
            lines.append(f"  • Audio: {result.quality.audio}")
        if result.quality.is_remux:
            lines.append("  • 📀 REMUX")
        if result.quality.is_repack:
            lines.append("  • 🔄 REPACK")

        lines.append("")

        # Size and protocol
        lines.append(f"💾 **Size:** {result.size_formatted}")
        lines.append(f"📡 **Protocol:** {result.protocol.upper()}")

        # Torrent info
        if result.protocol == "torrent":
            if result.seeders is not None:
                lines.append(f"🌱 **Seeders:** {result.seeders}")
            if result.leechers is not None:
                lines.append(f"📥 **Leechers:** {result.leechers}")

        # Indexer
        lines.append(f"🔍 **Indexer:** {result.indexer}")

        # Score
        lines.append(f"\n**Score:** {result.calculated_score}/100")

        # Season/episode info
        if result.detected_season is not None:
            season_info = f"Season {result.detected_season}"
            if result.detected_episode is not None:
                season_info += f" Episode {result.detected_episode}"
            if result.is_season_pack:
                season_info += " (Season Pack)"
            lines.append(f"📅 {season_info}")

        # Publish date
        if result.publish_date:
            date_str = result.publish_date.strftime("%Y-%m-%d %H:%M")
            lines.append(f"📆 **Published:** {date_str}")

        return "\n".join(lines)

    @staticmethod
    def format_movie_info(movie: MovieInfo, compact: bool = False) -> str:
        """Format movie information."""
        if compact:
            return f"🎬 **{movie.title}** ({movie.year})"

        lines = [f"🎬 **{movie.title}** ({movie.year})"]

        if movie.original_title and movie.original_title != movie.title:
            lines.append(f"_Original: {movie.original_title}_")

        if movie.runtime:
            lines.append(f"⏱ Runtime: {movie.runtime} min")

        if movie.genres:
            lines.append(f"🎭 Genres: {', '.join(movie.genres[:5])}")

        if movie.studio:
            lines.append(f"🏢 Studio: {movie.studio}")

        if movie.overview:
            overview = movie.overview[:300]
            if len(movie.overview) > 300:
                overview += "..."
            lines.append(f"\n📝 {overview}")

        # Status in Radarr
        if movie.radarr_id:
            status = "✅ In library"
            if movie.has_file:
                status += " (Downloaded)"
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
            lines.append(f"_Original: {series.original_title}_")

        if series.network:
            lines.append(f"📡 Network: {series.network}")

        if series.status:
            status_emoji = "🟢" if series.status.lower() == "continuing" else "🔴"
            lines.append(f"{status_emoji} Status: {series.status.capitalize()}")

        lines.append(f"📊 Seasons: {series.season_count} | Episodes: {series.total_episode_count}")

        if series.runtime:
            lines.append(f"⏱ Runtime: ~{series.runtime} min/episode")

        if series.genres:
            lines.append(f"🎭 Genres: {', '.join(series.genres[:5])}")

        if series.overview:
            overview = series.overview[:300]
            if len(series.overview) > 300:
                overview += "..."
            lines.append(f"\n📝 {overview}")

        # Status in Sonarr
        if series.sonarr_id:
            lines.append("\n✅ In library")

        return "\n".join(lines)

    @staticmethod
    def format_system_status(statuses: list[SystemStatus]) -> str:
        """Format system status information."""
        lines = ["**System Status**\n"]

        for status in statuses:
            if status.available:
                emoji = "✅"
                version_str = f" v{status.version}" if status.version else ""
                time_str = f" ({status.response_time_ms}ms)" if status.response_time_ms else ""
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
        lines = ["**Your Settings**\n"]

        # Radarr settings
        lines.append("**🎬 Radarr:**")
        rp = next((p for p in radarr_profiles if p.id == prefs.radarr_quality_profile_id), None)
        lines.append(f"  Profile: {rp.name if rp else 'Not set'}")
        rf = next((f for f in radarr_folders if f.id == prefs.radarr_root_folder_id), None)
        lines.append(f"  Folder: {rf.path if rf else 'Not set'}")

        # Sonarr settings
        lines.append("\n**📺 Sonarr:**")
        sp = next((p for p in sonarr_profiles if p.id == prefs.sonarr_quality_profile_id), None)
        lines.append(f"  Profile: {sp.name if sp else 'Not set'}")
        sf = next((f for f in sonarr_folders if f.id == prefs.sonarr_root_folder_id), None)
        lines.append(f"  Folder: {sf.path if sf else 'Not set'}")

        # General preferences
        lines.append("\n**⚙️ General:**")
        lines.append(f"  Preferred Quality: {prefs.preferred_resolution or 'Any'}")
        lines.append(f"  Auto-Grab: {'ON' if prefs.auto_grab_enabled else 'OFF'}")

        return "\n".join(lines)

    @staticmethod
    def format_action_log(actions: list[ActionLog], limit: int = 10) -> str:
        """Format action history."""
        if not actions:
            return "No actions recorded yet."

        lines = ["**Recent Actions**\n"]

        for action in actions[:limit]:
            emoji = "✅" if action.success else "❌"
            type_emoji = "🎬" if action.content_type == ContentType.MOVIE else "📺"

            action_str = action.action_type.value.upper()
            title = action.content_title or action.query or "Unknown"

            if len(title) > 30:
                title = title[:27] + "..."

            date_str = action.created_at.strftime("%m/%d %H:%M")

            lines.append(f"{emoji} {type_emoji} {action_str}: {title} ({date_str})")

            if not action.success and action.error_message:
                error = action.error_message[:50]
                lines.append(f"   ↳ Error: {error}")

        return "\n".join(lines)

    @staticmethod
    def format_help() -> str:
        """Format help message."""
        return """**TG_arr Bot Help**

**Commands:**
• `/start` - Start the bot
• `/help` - Show this help message
• `/search <query>` - Search for movies or series (auto-detect)
• `/movie <query>` - Search for a movie
• `/series <query>` - Search for a series
• `/settings` - Configure your preferences
• `/status` - Check Prowlarr/Radarr/Sonarr status
• `/history` - View your recent actions
• `/cancel` - Cancel current operation

**Download Management:**
• `/downloads` or `/dl` - View active downloads
• `/qstatus` - qBittorrent status overview

**Search Examples:**
• `Dune 2021` - Search for Dune (2021)
• `Breaking Bad S02` - Search Breaking Bad Season 2
• `The Office 1080p` - Search The Office in 1080p

**How it works:**
1. Send a search query
2. Select movie or series (if not auto-detected)
3. Choose from available releases
4. Confirm to add and download

**Tips:**
• Use `/settings` to set default quality profiles and folders
• Enable auto-grab to quickly download high-scored releases
• Results are sorted by quality score
• Use `/downloads` to manage active torrents
"""

    @staticmethod
    def format_error(error: str, include_retry: bool = True) -> str:
        """Format error message."""
        msg = f"❌ **Error:** {error}"
        if include_retry:
            msg += "\n\nPlease try again or use /cancel to start over."
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
        lines = ["**qBittorrent Status**\n"]

        # Version and connection
        lines.append(f"🖥 **Version:** {status.version}")
        conn_emoji = "🟢" if status.connection_status == "connected" else "🔴"
        lines.append(f"{conn_emoji} **Connection:** {status.connection_status}")

        lines.append("")

        # Transfer speeds
        lines.append("**📊 Transfer:**")
        lines.append(f"  ⬇️ Download: {status.download_speed_formatted}")
        lines.append(f"  ⬆️ Upload: {status.upload_speed_formatted}")

        # Limits
        if status.download_limit > 0 or status.upload_limit > 0:
            from bot.models import format_speed
            dl_limit = format_speed(status.download_limit) if status.download_limit > 0 else "♾"
            ul_limit = format_speed(status.upload_limit) if status.upload_limit > 0 else "♾"
            lines.append(f"  📉 Limits: ⬇️ {dl_limit} | ⬆️ {ul_limit}")

        lines.append("")

        # Torrents
        lines.append("**📋 Torrents:**")
        lines.append(f"  Total: {status.total_torrents}")
        lines.append(f"  Active: ⬇️ {status.active_downloads} | ⬆️ {status.active_uploads}")
        if status.paused_torrents > 0:
            lines.append(f"  Paused: {status.paused_torrents}")

        lines.append("")

        # Disk
        lines.append(f"💾 **Free space:** {status.free_space_formatted}")

        # DHT
        if status.dht_nodes > 0:
            lines.append(f"🌐 **DHT nodes:** {status.dht_nodes}")

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
            TorrentFilter.ALL: "All",
            TorrentFilter.DOWNLOADING: "Downloading",
            TorrentFilter.SEEDING: "Seeding",
            TorrentFilter.COMPLETED: "Completed",
            TorrentFilter.PAUSED: "Paused",
            TorrentFilter.ACTIVE: "Active",
            TorrentFilter.INACTIVE: "Inactive",
            TorrentFilter.STALLED: "Stalled",
            TorrentFilter.ERRORED: "Errored",
        }

        filter_name = filter_names.get(current_filter, "All")
        header = f"**📥 Downloads** — {filter_name}\n"
        header += f"Showing {len(torrents)} of {total_count} torrents"

        if total_pages > 1:
            header += f" (Page {page + 1}/{total_pages})"

        return header

    @staticmethod
    def format_torrent_details(torrent: TorrentInfo) -> str:
        """Format detailed view of a torrent."""
        lines = [f"**{torrent.name}**\n"]

        # State and progress
        lines.append(f"{torrent.state_emoji} **State:** {torrent.state.value.capitalize()}")
        lines.append(f"📊 **Progress:** {torrent.progress_percent}%")

        # Progress bar
        progress_bar = Formatters._progress_bar(torrent.progress)
        lines.append(f"`{progress_bar}`")

        lines.append("")

        # Size info
        from bot.models import format_bytes
        downloaded = format_bytes(torrent.downloaded)
        lines.append(f"💾 **Size:** {downloaded} / {torrent.size_formatted}")

        # Speeds
        if torrent.download_speed > 0 or torrent.upload_speed > 0:
            lines.append(f"⬇️ **Download:** {torrent.download_speed_formatted}")
            lines.append(f"⬆️ **Upload:** {torrent.upload_speed_formatted}")

        # ETA
        if torrent.eta is not None and torrent.eta > 0 and torrent.progress < 1.0:
            lines.append(f"⏱ **ETA:** {torrent.eta_formatted}")

        lines.append("")

        # Peers
        lines.append("**🌐 Peers:**")
        lines.append(f"  Seeds: {torrent.seeds} ({torrent.seeds_total} total)")
        lines.append(f"  Peers: {torrent.peers} ({torrent.peers_total} total)")

        # Ratio
        lines.append(f"\n📈 **Ratio:** {torrent.ratio:.2f}")

        # Category and tags
        if torrent.category:
            lines.append(f"📁 **Category:** {torrent.category}")
        if torrent.tags:
            lines.append(f"🏷 **Tags:** {', '.join(torrent.tags)}")

        # Save path
        lines.append(f"\n📂 **Path:** `{torrent.save_path}`")

        # Dates
        if torrent.added_on:
            lines.append(f"📅 **Added:** {torrent.added_on.strftime('%Y-%m-%d %H:%M')}")
        if torrent.completion_on and torrent.progress >= 1.0:
            lines.append(f"✅ **Completed:** {torrent.completion_on.strftime('%Y-%m-%d %H:%M')}")

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
        lines = ["✅ **Download Complete!**\n"]
        lines.append(f"📥 **{torrent.name}**")
        lines.append(f"💾 Size: {torrent.size_formatted}")
        lines.append(f"📂 Path: `{torrent.save_path}`")

        if torrent.completion_on:
            lines.append(f"⏱ Completed: {torrent.completion_on.strftime('%Y-%m-%d %H:%M')}")

        return "\n".join(lines)

    @staticmethod
    def format_no_torrents(current_filter: TorrentFilter) -> str:
        """Format message when no torrents match the filter."""
        if current_filter == TorrentFilter.ALL:
            return "📭 No torrents found.\n\nAdd content using /search or /movie commands."

        filter_names = {
            TorrentFilter.DOWNLOADING: "downloading",
            TorrentFilter.SEEDING: "seeding",
            TorrentFilter.COMPLETED: "completed",
            TorrentFilter.PAUSED: "paused",
            TorrentFilter.ACTIVE: "active",
            TorrentFilter.STALLED: "stalled",
            TorrentFilter.ERRORED: "errored",
        }

        filter_name = filter_names.get(current_filter, "matching")
        return f"📭 No {filter_name} torrents found.\n\nTry a different filter or check /downloads."

    @staticmethod
    def format_speed_limit_changed(limit_type: str, speed_kb: int) -> str:
        """Format message for speed limit change."""
        if speed_kb == 0:
            speed_str = "unlimited"
        else:
            from bot.models import format_speed
            speed_str = format_speed(speed_kb * 1024)

        direction = "Download" if limit_type == "dl" else "Upload"
        return f"✅ {direction} limit set to {speed_str}"

    @staticmethod
    def format_torrent_action(action: str, torrent_name: str, success: bool = True) -> str:
        """Format message for torrent action result."""
        name = torrent_name[:40] + "..." if len(torrent_name) > 43 else torrent_name

        if success:
            action_messages = {
                "pause": f"⏸ Paused: {name}",
                "resume": f"▶️ Resumed: {name}",
                "delete": f"🗑 Removed: {name}",
                "delete_files": f"🗑 Removed with files: {name}",
            }
            return action_messages.get(action, f"✅ {action}: {name}")
        else:
            return f"❌ Failed to {action}: {name}"

    @staticmethod
    def format_bulk_action(action: str, count: int) -> str:
        """Format message for bulk torrent action."""
        action_messages = {
            "pause": f"⏸ Paused {count} torrents",
            "resume": f"▶️ Resumed {count} torrents",
        }
        return action_messages.get(action, f"✅ {action} {count} torrents")
