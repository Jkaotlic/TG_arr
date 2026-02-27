"""Sonarr API client."""

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import structlog

from bot.clients.base import BaseAPIClient, NotFoundError
from bot.models import QualityProfile, RootFolder, SeriesInfo

logger = structlog.get_logger()


class SonarrClient(BaseAPIClient):
    """Client for Sonarr API."""

    def __init__(self, base_url: str, api_key: str):
        super().__init__(base_url, api_key, "Sonarr")

    async def lookup_series(self, query: str) -> list[SeriesInfo]:
        """
        Search for series by title.

        Args:
            query: Series title to search for

        Returns:
            List of SeriesInfo objects
        """
        log = logger.bind(query=query)
        log.info("Looking up series in Sonarr")

        params = {"term": query}
        results = await self.get("/api/v3/series/lookup", params=params)

        if not isinstance(results, list):
            return []

        series_list = []
        for item in results:
            try:
                series = self._parse_series(item)
                if series:
                    series_list.append(series)
            except Exception as e:
                log.warning("Failed to parse series", error=str(e))

        log.info("Lookup completed", series_count=len(series_list))
        return series_list

    async def lookup_series_by_tvdb(self, tvdb_id: int) -> Optional[SeriesInfo]:
        """Look up a series by TVDB ID."""
        params = {"term": f"tvdb:{tvdb_id}"}
        results = await self.get("/api/v3/series/lookup", params=params)

        if isinstance(results, list) and results:
            return self._parse_series(results[0])
        return None

    async def get_series(self, sonarr_id: int) -> Optional[SeriesInfo]:
        """Get series by Sonarr ID."""
        try:
            result = await self.get(f"/api/v3/series/{sonarr_id}")
            if isinstance(result, dict):
                return self._parse_series(result)
        except NotFoundError:
            return None
        return None

    async def get_series_by_tvdb(self, tvdb_id: int) -> Optional[SeriesInfo]:
        """Get series from library by TVDB ID."""
        params = {"tvdbId": tvdb_id}
        results = await self.get("/api/v3/series", params=params)

        if isinstance(results, list) and results:
            return self._parse_series(results[0])
        return None

    async def add_series(
        self,
        series: SeriesInfo,
        quality_profile_id: int,
        root_folder_path: str,
        monitored: bool = True,
        season_folder: bool = True,
        search_for_missing: bool = True,
        search_for_cutoff_unmet: bool = False,
        monitor_type: str = "all",
        tags: Optional[list[int]] = None,
    ) -> SeriesInfo:
        """
        Add a series to Sonarr.

        Args:
            series: SeriesInfo object with at least tvdb_id and title
            quality_profile_id: Quality profile ID
            root_folder_path: Root folder path
            monitored: Whether to monitor the series
            season_folder: Whether to use season folders
            search_for_missing: Whether to search for missing episodes
            search_for_cutoff_unmet: Whether to search for cutoff unmet
            monitor_type: Monitor type (all, future, missing, existing, pilot, firstSeason, latestSeason, none)
            tags: Optional list of tag IDs

        Returns:
            SeriesInfo of added series
        """
        log = logger.bind(title=series.title, tvdb_id=series.tvdb_id)
        log.info("Adding series to Sonarr")

        # Build seasons with monitoring based on monitor_type
        seasons = []
        if series.seasons:
            for s in series.seasons:
                season_num = s.get("seasonNumber", 0)
                season_monitored = self._should_monitor_season(season_num, monitor_type, series.season_count)
                seasons.append({
                    "seasonNumber": season_num,
                    "monitored": season_monitored,
                })

        payload = {
            "title": series.title,
            "tvdbId": series.tvdb_id,
            "qualityProfileId": quality_profile_id,
            "rootFolderPath": root_folder_path,
            "monitored": monitored,
            "seasonFolder": season_folder,
            "seriesType": "standard",
            "addOptions": {
                "searchForMissingEpisodes": search_for_missing,
                "searchForCutoffUnmetEpisodes": search_for_cutoff_unmet,
                "monitor": monitor_type,
            },
        }

        if series.imdb_id:
            payload["imdbId"] = series.imdb_id

        if seasons:
            payload["seasons"] = seasons

        if tags:
            payload["tags"] = tags

        result = await self.post("/api/v3/series", json_data=payload)

        if isinstance(result, dict):
            added_series = self._parse_series(result)
            if added_series:
                log.info("Series added successfully", sonarr_id=added_series.sonarr_id)
                return added_series

        raise ValueError("Не удалось добавить сериал в Sonarr")

    def _should_monitor_season(self, season_num: int, monitor_type: str, total_seasons: int) -> bool:
        """Determine if a season should be monitored based on monitor type."""
        if monitor_type == "all":
            return True
        elif monitor_type == "none":
            return False
        elif monitor_type == "future":
            return False  # Future episodes will be monitored automatically
        elif monitor_type == "missing":
            return True
        elif monitor_type == "existing":
            return True
        elif monitor_type == "pilot":
            return season_num == 1
        elif monitor_type == "firstSeason":
            return season_num == 1
        elif monitor_type == "latestSeason":
            return season_num == total_seasons
        return True

    async def get_episodes(self, series_id: int, season_number: Optional[int] = None) -> list[dict[str, Any]]:
        """Get episodes for a series."""
        params = {"seriesId": series_id}
        if season_number is not None:
            params["seasonNumber"] = season_number

        results = await self.get("/api/v3/episode", params=params)
        return results if isinstance(results, list) else []

    async def get_releases(self, episode_id: Optional[int] = None, series_id: Optional[int] = None) -> list[dict[str, Any]]:
        """Get available releases for an episode or series."""
        params = {}
        if episode_id:
            params["episodeId"] = episode_id
        elif series_id:
            params["seriesId"] = series_id
        else:
            return []

        results = await self.get("/api/v3/release", params=params)
        return results if isinstance(results, list) else []

    async def grab_release(self, guid: str, indexer_id: int) -> dict[str, Any]:
        """
        Grab a specific release for download.

        Args:
            guid: Release GUID
            indexer_id: Indexer ID

        Returns:
            Grab result
        """
        payload = {
            "guid": guid,
            "indexerId": indexer_id,
        }
        result = await self.post("/api/v3/release", json_data=payload)
        return result if isinstance(result, dict) else {}

    async def push_release(
        self,
        title: str,
        download_url: str,
        protocol: str = "torrent",
        publish_date: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Push a release to Sonarr for processing.

        Args:
            title: Release title
            download_url: Download URL
            protocol: Protocol (torrent or usenet)
            publish_date: Publication date ISO string

        Returns:
            Push result
        """
        payload = {
            "title": title,
            "downloadUrl": download_url,
            "protocol": protocol.capitalize(),
        }

        if publish_date:
            payload["publishDate"] = publish_date

        result = await self.post("/api/v3/release/push", json_data=payload)
        return result if isinstance(result, dict) else {}

    async def search_series(self, series_id: int) -> dict[str, Any]:
        """Trigger a search for all episodes of a series."""
        payload = {
            "name": "SeriesSearch",
            "seriesId": series_id,
        }
        result = await self.post("/api/v3/command", json_data=payload)
        return result if isinstance(result, dict) else {}

    async def search_season(self, series_id: int, season_number: int) -> dict[str, Any]:
        """Trigger a search for a specific season."""
        payload = {
            "name": "SeasonSearch",
            "seriesId": series_id,
            "seasonNumber": season_number,
        }
        result = await self.post("/api/v3/command", json_data=payload)
        return result if isinstance(result, dict) else {}

    async def search_episodes(self, episode_ids: list[int]) -> dict[str, Any]:
        """Trigger a search for specific episodes."""
        payload = {
            "name": "EpisodeSearch",
            "episodeIds": episode_ids,
        }
        result = await self.post("/api/v3/command", json_data=payload)
        return result if isinstance(result, dict) else {}

    async def get_calendar(self, days: int = 7) -> list[dict[str, Any]]:
        """Get upcoming episodes from Sonarr calendar.

        Args:
            days: Number of days to look ahead.

        Returns:
            List of episode dicts with series info.
        """
        now = datetime.now(timezone.utc)
        start = now.strftime("%Y-%m-%d")
        end = (now + timedelta(days=days)).strftime("%Y-%m-%d")

        params = {
            "start": start,
            "end": end,
            "includeSeries": "true",
            "includeEpisodeFile": "false",
        }
        results = await self.get("/api/v3/calendar", params=params)
        if not isinstance(results, list):
            return []

        episodes = []
        for ep in results:
            try:
                episodes.append({
                    "series_title": ep.get("series", {}).get("title", "Unknown"),
                    "season": ep.get("seasonNumber", 0),
                    "episode": ep.get("episodeNumber", 0),
                    "title": ep.get("title", ""),
                    "air_date": ep.get("airDateUtc", ""),
                    "has_file": ep.get("hasFile", False),
                    "overview": ep.get("overview", ""),
                })
            except Exception as e:
                logger.warning("Failed to parse calendar episode", error=str(e))

        return episodes

    async def get_quality_profiles(self) -> list[QualityProfile]:
        """Get all quality profiles."""
        results = await self.get("/api/v3/qualityprofile")
        profiles = []
        if isinstance(results, list):
            for item in results:
                profiles.append(QualityProfile(
                    id=item["id"],
                    name=item["name"],
                ))
        return profiles

    async def get_root_folders(self) -> list[RootFolder]:
        """Get all root folders."""
        results = await self.get("/api/v3/rootfolder")
        folders = []
        if isinstance(results, list):
            for item in results:
                folders.append(RootFolder(
                    id=item["id"],
                    path=item["path"],
                    free_space=item.get("freeSpace"),
                ))
        return folders

    async def get_tags(self) -> list[dict[str, Any]]:
        """Get all tags."""
        results = await self.get("/api/v3/tag")
        return results if isinstance(results, list) else []

    def _parse_series(self, item: dict[str, Any]) -> Optional[SeriesInfo]:
        """Parse Sonarr series response to SeriesInfo."""
        tvdb_id = item.get("tvdbId")
        if not tvdb_id:
            return None

        title = item.get("title") or item.get("sortTitle") or ""
        if not title:
            return None

        # Parse images
        poster_url = None
        fanart_url = None
        for image in item.get("images", []):
            cover_type = image.get("coverType", "").lower()
            url = image.get("remoteUrl") or image.get("url")
            if cover_type == "poster" and not poster_url:
                poster_url = url
            elif cover_type == "fanart" and not fanart_url:
                fanart_url = url

        # Parse ratings
        ratings = {}
        if "ratings" in item:
            rating_data = item["ratings"]
            if isinstance(rating_data, dict):
                if "value" in rating_data:
                    ratings["default"] = rating_data["value"]
                else:
                    for source, data in rating_data.items():
                        if isinstance(data, dict) and "value" in data:
                            ratings[source] = data["value"]

        # Parse seasons
        seasons = item.get("seasons", [])
        season_count = len([s for s in seasons if s.get("seasonNumber", 0) > 0])

        # Calculate total episodes
        total_episodes = sum(s.get("statistics", {}).get("totalEpisodeCount", 0) for s in seasons)

        return SeriesInfo(
            tvdb_id=tvdb_id,
            imdb_id=item.get("imdbId"),
            title=title,
            original_title=item.get("originalTitle"),
            year=item.get("year"),
            overview=item.get("overview"),
            runtime=item.get("runtime"),
            network=item.get("network"),
            status=item.get("status"),
            genres=item.get("genres", []),
            poster_url=poster_url,
            fanart_url=fanart_url,
            ratings=ratings,
            season_count=season_count,
            total_episode_count=total_episodes,
            sonarr_id=item.get("id"),
            quality_profile_id=item.get("qualityProfileId"),
            root_folder_path=item.get("rootFolderPath") or item.get("path"),
            seasons=seasons,
        )

    async def check_connection(self) -> tuple[bool, str | None, float | None]:
        """Check if Sonarr is available. Uses v3 API."""
        start_time = time.monotonic()
        try:
            result = await self.get("/api/v3/system/status")
            elapsed = (time.monotonic() - start_time) * 1000
            version = result.get("version") if isinstance(result, dict) else None
            return True, version, round(elapsed, 2)
        except Exception as e:
            elapsed = (time.monotonic() - start_time) * 1000
            logger.warning("Sonarr health check failed", error=str(e))
            return False, None, round(elapsed, 2)
