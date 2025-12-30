"""Service for adding content to Radarr/Sonarr."""

from typing import Any, Optional

import structlog

from bot.clients.base import APIError
from bot.clients.prowlarr import ProwlarrClient
from bot.clients.qbittorrent import QBittorrentClient, QBittorrentError
from bot.clients.radarr import RadarrClient
from bot.clients.sonarr import SonarrClient
from bot.models import (
    ActionLog,
    ActionType,
    ContentType,
    MovieInfo,
    QualityProfile,
    RootFolder,
    SearchResult,
    SeriesInfo,
)

logger = structlog.get_logger()


class AddService:
    """Service for adding content and grabbing releases."""

    def __init__(
        self,
        prowlarr: ProwlarrClient,
        radarr: RadarrClient,
        sonarr: SonarrClient,
        qbittorrent: Optional[QBittorrentClient] = None,
    ):
        self.prowlarr = prowlarr
        self.radarr = radarr
        self.sonarr = sonarr
        self.qbittorrent = qbittorrent

    async def get_radarr_profiles(self) -> list[QualityProfile]:
        """Get Radarr quality profiles."""
        return await self.radarr.get_quality_profiles()

    async def get_radarr_root_folders(self) -> list[RootFolder]:
        """Get Radarr root folders."""
        return await self.radarr.get_root_folders()

    async def get_sonarr_profiles(self) -> list[QualityProfile]:
        """Get Sonarr quality profiles."""
        return await self.sonarr.get_quality_profiles()

    async def get_sonarr_root_folders(self) -> list[RootFolder]:
        """Get Sonarr root folders."""
        return await self.sonarr.get_root_folders()

    async def add_movie(
        self,
        movie: MovieInfo,
        quality_profile_id: int,
        root_folder_path: str,
        search_for_movie: bool = True,
        tags: Optional[list[int]] = None,
    ) -> tuple[MovieInfo, ActionLog]:
        """
        Add a movie to Radarr.

        Args:
            movie: MovieInfo object
            quality_profile_id: Quality profile ID
            root_folder_path: Root folder path
            search_for_movie: Whether to trigger search after adding
            tags: Optional tag IDs

        Returns:
            Tuple of (added MovieInfo, ActionLog)
        """
        log = logger.bind(title=movie.title, tmdb_id=movie.tmdb_id)
        log.info("Adding movie to Radarr")

        action = ActionLog(
            user_id=0,  # Will be set by handler
            action_type=ActionType.ADD,
            content_type=ContentType.MOVIE,
            content_title=movie.title,
            content_id=str(movie.tmdb_id),
        )

        try:
            # Check if already exists
            existing = await self.radarr.get_movie_by_tmdb(movie.tmdb_id)
            if existing and existing.radarr_id:
                log.info("Movie already exists", radarr_id=existing.radarr_id)
                action.success = True
                return existing, action

            added = await self.radarr.add_movie(
                movie=movie,
                quality_profile_id=quality_profile_id,
                root_folder_path=root_folder_path,
                search_for_movie=search_for_movie,
                tags=tags,
            )

            action.success = True
            log.info("Movie added successfully", radarr_id=added.radarr_id)
            return added, action

        except Exception as e:
            log.error("Failed to add movie", error=str(e))
            action.success = False
            action.error_message = str(e)
            raise

    async def add_series(
        self,
        series: SeriesInfo,
        quality_profile_id: int,
        root_folder_path: str,
        monitor_type: str = "all",
        search_for_missing: bool = True,
        tags: Optional[list[int]] = None,
    ) -> tuple[SeriesInfo, ActionLog]:
        """
        Add a series to Sonarr.

        Args:
            series: SeriesInfo object
            quality_profile_id: Quality profile ID
            root_folder_path: Root folder path
            monitor_type: What to monitor
            search_for_missing: Whether to search for missing episodes
            tags: Optional tag IDs

        Returns:
            Tuple of (added SeriesInfo, ActionLog)
        """
        log = logger.bind(title=series.title, tvdb_id=series.tvdb_id)
        log.info("Adding series to Sonarr")

        action = ActionLog(
            user_id=0,  # Will be set by handler
            action_type=ActionType.ADD,
            content_type=ContentType.SERIES,
            content_title=series.title,
            content_id=str(series.tvdb_id),
        )

        try:
            # Check if already exists
            existing = await self.sonarr.get_series_by_tvdb(series.tvdb_id)
            if existing and existing.sonarr_id:
                log.info("Series already exists", sonarr_id=existing.sonarr_id)
                action.success = True
                return existing, action

            added = await self.sonarr.add_series(
                series=series,
                quality_profile_id=quality_profile_id,
                root_folder_path=root_folder_path,
                monitor_type=monitor_type,
                search_for_missing=search_for_missing,
                tags=tags,
            )

            action.success = True
            log.info("Series added successfully", sonarr_id=added.sonarr_id)
            return added, action

        except Exception as e:
            log.error("Failed to add series", error=str(e))
            action.success = False
            action.error_message = str(e)
            raise

    async def grab_movie_release(
        self,
        movie: MovieInfo,
        release: SearchResult,
        quality_profile_id: int,
        root_folder_path: str,
        force_download: bool = False,
    ) -> tuple[bool, ActionLog, str]:
        """
        Grab a specific release for a movie.

        First adds the movie to Radarr (if not exists), then tries to grab the release.
        Falls back to push release if direct grab fails.
        If force_download=True and release is rejected, downloads directly via qBittorrent.

        Args:
            movie: MovieInfo object
            release: SearchResult to grab
            quality_profile_id: Quality profile ID
            root_folder_path: Root folder path
            force_download: Force download even if rejected by Radarr

        Returns:
            Tuple of (success, ActionLog, message)
        """
        log = logger.bind(
            title=movie.title,
            release_title=release.title,
            indexer=release.indexer,
        )
        log.info("Grabbing movie release")

        action = ActionLog(
            user_id=0,
            action_type=ActionType.GRAB,
            content_type=ContentType.MOVIE,
            content_title=movie.title,
            content_id=str(movie.tmdb_id),
            release_title=release.title,
        )

        try:
            # Ensure movie is in Radarr
            existing = await self.radarr.get_movie_by_tmdb(movie.tmdb_id)
            if not existing or not existing.radarr_id:
                log.info("Movie not in library, adding first")
                existing, _ = await self.add_movie(
                    movie=movie,
                    quality_profile_id=quality_profile_id,
                    root_folder_path=root_folder_path,
                    search_for_movie=False,  # We'll grab manually
                )

            # Try to push the release
            release_rejected = False
            rejections = []
            if release.download_url:
                try:
                    log.info("Attempting push_release", download_url=release.download_url[:100])
                    result = await self.radarr.push_release(
                        title=release.title,
                        download_url=release.download_url,
                        protocol=release.protocol,
                        publish_date=release.publish_date.isoformat() if release.publish_date else None,
                    )
                    log.info("Push release result", result=result)
                    if result and result.get("approved") is True:
                        action.success = True
                        log.info("Release pushed successfully")
                        return True, action, "Релиз отправлен на скачивание"
                    else:
                        rejections = result.get("rejections", []) if result else []
                        release_rejected = True
                        rejection_msg = f"rejections: {rejections}" if rejections else "no explicit approval from Radarr/Sonarr"
                        log.warning("Release was not approved", reason=rejection_msg)
                except APIError as e:
                    log.warning("Push release failed, trying direct grab", error=str(e))

            # Try direct grab through indexer
            if release.indexer_id > 0 and not release_rejected:
                try:
                    await self.radarr.grab_release(release.guid, release.indexer_id)
                    action.success = True
                    log.info("Release grabbed successfully")
                    return True, action, "Релиз захвачен"
                except APIError as e:
                    log.warning("Direct grab failed", error=str(e))

            # Force download via qBittorrent if rejected and force_download enabled
            if release_rejected and force_download and self.qbittorrent:
                download_url = release.download_url or release.magnet_url
                if download_url:
                    try:
                        success = await self.qbittorrent.add_torrent_url(
                            download_url,
                            category="radarr",  # Tag for Radarr/movies
                        )
                        if success:
                            action.success = True
                            log.info("Force downloaded via qBittorrent with radarr category")
                            return True, action, "Принудительно загружено через qBittorrent"
                        else:
                            log.error("qBittorrent rejected torrent", download_url=download_url[:100])
                            raise QBittorrentError("Failed to add torrent to qBittorrent")
                    except Exception as e:
                        log.error("Force download failed", error=str(e))

            # If rejected, return rejection info without fallback
            if release_rejected:
                action.success = False
                rejection_msg = ", ".join(rejections) if rejections else "Отклонено"
                action.error_message = rejection_msg
                return False, action, f"Релиз отклонён: {rejection_msg}"

            # Fallback: trigger search
            if existing and existing.radarr_id:
                await self.radarr.search_movie(existing.radarr_id)
                action.success = True
                log.info("Triggered automatic search as fallback")
                return True, action, "Запущен автопоиск"

            action.success = False
            action.error_message = "Could not grab release or trigger search"
            return False, action, "Не удалось захватить релиз"

        except Exception as e:
            log.error("Failed to grab release", error=str(e))
            action.success = False
            action.error_message = str(e)
            return False, action, f"Error: {str(e)}"

    async def grab_series_release(
        self,
        series: SeriesInfo,
        release: SearchResult,
        quality_profile_id: int,
        root_folder_path: str,
        monitor_type: str = "all",
        force_download: bool = False,
    ) -> tuple[bool, ActionLog, str]:
        """
        Grab a specific release for a series.

        Args:
            series: SeriesInfo object
            release: SearchResult to grab
            quality_profile_id: Quality profile ID
            root_folder_path: Root folder path
            monitor_type: What to monitor
            force_download: Force download even if rejected by Sonarr

        Returns:
            Tuple of (success, ActionLog, message)
        """
        log = logger.bind(
            title=series.title,
            release_title=release.title,
            indexer=release.indexer,
        )
        log.info("Grabbing series release")

        action = ActionLog(
            user_id=0,
            action_type=ActionType.GRAB,
            content_type=ContentType.SERIES,
            content_title=series.title,
            content_id=str(series.tvdb_id),
            release_title=release.title,
        )

        try:
            # Ensure series is in Sonarr
            existing = await self.sonarr.get_series_by_tvdb(series.tvdb_id)
            if not existing or not existing.sonarr_id:
                log.info("Series not in library, adding first")
                existing, _ = await self.add_series(
                    series=series,
                    quality_profile_id=quality_profile_id,
                    root_folder_path=root_folder_path,
                    monitor_type=monitor_type,
                    search_for_missing=False,
                )

            # Try to push the release
            release_rejected = False
            rejections = []
            if release.download_url:
                try:
                    log.info("Attempting push_release", download_url=release.download_url[:100])
                    result = await self.sonarr.push_release(
                        title=release.title,
                        download_url=release.download_url,
                        protocol=release.protocol,
                        publish_date=release.publish_date.isoformat() if release.publish_date else None,
                    )
                    log.info("Push release result", result=result)
                    if result and result.get("approved") is True:
                        action.success = True
                        log.info("Release pushed successfully")
                        return True, action, "Релиз отправлен на скачивание"
                    else:
                        rejections = result.get("rejections", []) if result else []
                        release_rejected = True
                        rejection_msg = f"rejections: {rejections}" if rejections else "no explicit approval from Radarr/Sonarr"
                        log.warning("Release was not approved", reason=rejection_msg)
                except APIError as e:
                    log.warning("Push release failed, trying direct grab", error=str(e))

            # Try direct grab
            if release.indexer_id > 0 and not release_rejected:
                try:
                    await self.sonarr.grab_release(release.guid, release.indexer_id)
                    action.success = True
                    log.info("Release grabbed successfully")
                    return True, action, "Релиз захвачен"
                except APIError as e:
                    log.warning("Direct grab failed", error=str(e))

            # Force download via qBittorrent if rejected and force_download enabled
            if release_rejected and force_download and self.qbittorrent:
                download_url = release.download_url or release.magnet_url
                if download_url:
                    try:
                        success = await self.qbittorrent.add_torrent_url(
                            download_url,
                            category="tv-sonarr",  # Tag for Sonarr/series
                        )
                        if success:
                            action.success = True
                            log.info("Force downloaded via qBittorrent with sonarr category")
                            return True, action, "Принудительно загружено через qBittorrent"
                        else:
                            log.error("qBittorrent rejected torrent", download_url=download_url[:100])
                            raise QBittorrentError("Failed to add torrent to qBittorrent")
                    except Exception as e:
                        log.error("Force download failed", error=str(e))

            # If rejected, return rejection info without fallback
            if release_rejected:
                action.success = False
                rejection_msg = ", ".join(rejections) if rejections else "Отклонено"
                action.error_message = rejection_msg
                return False, action, f"Релиз отклонён: {rejection_msg}"

            # Fallback: trigger appropriate search
            if existing and existing.sonarr_id:
                if release.is_season_pack and release.detected_season is not None:
                    await self.sonarr.search_season(existing.sonarr_id, release.detected_season)
                    msg = f"Запущен поиск сезона {release.detected_season}"
                else:
                    await self.sonarr.search_series(existing.sonarr_id)
                    msg = "Запущен полный поиск сериала"

                action.success = True
                log.info("Triggered automatic search as fallback")
                return True, action, msg

            action.success = False
            action.error_message = "Could not grab release or trigger search"
            return False, action, "Не удалось захватить релиз"

        except Exception as e:
            log.error("Failed to grab release", error=str(e))
            action.success = False
            action.error_message = str(e)
            return False, action, f"Error: {str(e)}"

    async def search_and_grab_best(
        self,
        content_type: ContentType,
        content_id: int,  # Radarr movie ID or Sonarr series ID
        season_number: Optional[int] = None,
    ) -> tuple[bool, str]:
        """
        Trigger a search for the best available release.

        Args:
            content_type: Movie or Series
            content_id: Radarr movie ID or Sonarr series ID
            season_number: Optional season number for series

        Returns:
            Tuple of (success, message)
        """
        try:
            if content_type == ContentType.MOVIE:
                await self.radarr.search_movie(content_id)
                return True, "Movie search triggered"
            else:
                if season_number is not None:
                    await self.sonarr.search_season(content_id, season_number)
                    return True, f"Season {season_number} search triggered"
                else:
                    await self.sonarr.search_series(content_id)
                    return True, "Series search triggered"
        except Exception as e:
            logger.error("Search failed", error=str(e))
            return False, f"Search failed: {str(e)}"
