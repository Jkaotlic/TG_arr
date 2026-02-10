"""Radarr API client."""

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import structlog

from bot.clients.base import BaseAPIClient, NotFoundError
from bot.models import MovieInfo, QualityProfile, RootFolder

logger = structlog.get_logger()


class RadarrClient(BaseAPIClient):
    """Client for Radarr API."""

    def __init__(self, base_url: str, api_key: str):
        super().__init__(base_url, api_key, "Radarr")

    async def lookup_movie(self, query: str) -> list[MovieInfo]:
        """
        Search for movies by title.

        Args:
            query: Movie title to search for

        Returns:
            List of MovieInfo objects
        """
        log = logger.bind(query=query)
        log.info("Looking up movie in Radarr")

        params = {"term": query}
        results = await self.get("/api/v3/movie/lookup", params=params)

        if not isinstance(results, list):
            return []

        movies = []
        for item in results:
            try:
                movie = self._parse_movie(item)
                if movie:
                    movies.append(movie)
            except Exception as e:
                log.warning("Failed to parse movie", error=str(e))

        log.info("Lookup completed", movie_count=len(movies))
        return movies

    async def lookup_movie_by_tmdb(self, tmdb_id: int) -> Optional[MovieInfo]:
        """Look up a movie by TMDB ID."""
        params = {"tmdbId": tmdb_id}
        results = await self.get("/api/v3/movie/lookup/tmdb", params=params)

        if isinstance(results, dict):
            return self._parse_movie(results)
        elif isinstance(results, list) and results:
            return self._parse_movie(results[0])
        return None

    async def get_movie(self, radarr_id: int) -> Optional[MovieInfo]:
        """Get movie by Radarr ID."""
        try:
            result = await self.get(f"/api/v3/movie/{radarr_id}")
            if isinstance(result, dict):
                return self._parse_movie(result)
        except NotFoundError:
            return None
        return None

    async def get_movie_by_tmdb(self, tmdb_id: int) -> Optional[MovieInfo]:
        """Get movie from library by TMDB ID."""
        params = {"tmdbId": tmdb_id}
        results = await self.get("/api/v3/movie", params=params)

        if isinstance(results, list) and results:
            return self._parse_movie(results[0])
        return None

    async def add_movie(
        self,
        movie: MovieInfo,
        quality_profile_id: int,
        root_folder_path: str,
        monitored: bool = True,
        search_for_movie: bool = True,
        minimum_availability: str = "released",
        tags: Optional[list[int]] = None,
    ) -> MovieInfo:
        """
        Add a movie to Radarr.

        Args:
            movie: MovieInfo object with at least tmdb_id and title
            quality_profile_id: Quality profile ID
            root_folder_path: Root folder path
            monitored: Whether to monitor the movie
            search_for_movie: Whether to search for the movie after adding
            minimum_availability: When to consider the movie available
            tags: Optional list of tag IDs

        Returns:
            MovieInfo of added movie
        """
        log = logger.bind(title=movie.title, tmdb_id=movie.tmdb_id)
        log.info("Adding movie to Radarr")

        payload = {
            "title": movie.title,
            "tmdbId": movie.tmdb_id,
            "year": movie.year,
            "qualityProfileId": quality_profile_id,
            "rootFolderPath": root_folder_path,
            "monitored": monitored,
            "minimumAvailability": minimum_availability,
            "addOptions": {
                "searchForMovie": search_for_movie,
            },
        }

        if movie.imdb_id:
            payload["imdbId"] = movie.imdb_id

        if tags:
            payload["tags"] = tags

        result = await self.post("/api/v3/movie", json_data=payload)

        if isinstance(result, dict):
            added_movie = self._parse_movie(result)
            if added_movie:
                log.info("Movie added successfully", radarr_id=added_movie.radarr_id)
                return added_movie

        raise ValueError("Не удалось добавить фильм в Radarr")

    async def get_releases(self, movie_id: int) -> list[dict[str, Any]]:
        """Get available releases for a movie."""
        params = {"movieId": movie_id}
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
        Push a release to Radarr for processing.

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

    async def search_movie(self, movie_id: int) -> dict[str, Any]:
        """Trigger a search for a movie."""
        payload = {
            "name": "MoviesSearch",
            "movieIds": [movie_id],
        }
        result = await self.post("/api/v3/command", json_data=payload)
        return result if isinstance(result, dict) else {}

    async def get_calendar(self, days: int = 7) -> list[dict[str, Any]]:
        """Get upcoming movie releases from Radarr calendar.

        Args:
            days: Number of days to look ahead.

        Returns:
            List of movie dicts with release info.
        """
        now = datetime.now(timezone.utc)
        start = now.strftime("%Y-%m-%d")
        end = (now + timedelta(days=days)).strftime("%Y-%m-%d")

        params = {
            "start": start,
            "end": end,
        }
        results = await self.get("/api/v3/calendar", params=params)
        if not isinstance(results, list):
            return []

        movies = []
        for item in results:
            try:
                # Radarr calendar returns full movie objects
                digital_release = item.get("digitalRelease", "")
                physical_release = item.get("physicalRelease", "")
                in_cinemas = item.get("inCinemas", "")
                release_date = digital_release or physical_release or in_cinemas

                movies.append({
                    "title": item.get("title", "Unknown"),
                    "year": item.get("year", 0),
                    "release_date": release_date,
                    "has_file": item.get("hasFile", False),
                    "is_available": item.get("isAvailable", False),
                    "overview": item.get("overview", ""),
                    "runtime": item.get("runtime", 0),
                    "digital_release": digital_release,
                    "physical_release": physical_release,
                    "in_cinemas": in_cinemas,
                })
            except Exception as e:
                logger.warning("Failed to parse calendar movie", error=str(e))

        return movies

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

    def _parse_movie(self, item: dict[str, Any]) -> Optional[MovieInfo]:
        """Parse Radarr movie response to MovieInfo."""
        tmdb_id = item.get("tmdbId")
        if not tmdb_id:
            return None

        title = item.get("title") or item.get("originalTitle") or ""
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
            for source, data in item["ratings"].items():
                if isinstance(data, dict) and "value" in data:
                    ratings[source] = data["value"]

        return MovieInfo(
            tmdb_id=tmdb_id,
            imdb_id=item.get("imdbId"),
            title=title,
            original_title=item.get("originalTitle"),
            year=item.get("year", 0),
            overview=item.get("overview"),
            runtime=item.get("runtime"),
            studio=item.get("studio"),
            genres=item.get("genres", []),
            poster_url=poster_url,
            fanart_url=fanart_url,
            ratings=ratings,
            radarr_id=item.get("id"),
            is_available=item.get("isAvailable", False),
            has_file=item.get("hasFile", False),
            quality_profile_id=item.get("qualityProfileId"),
            root_folder_path=item.get("rootFolderPath") or item.get("path"),
        )

    async def check_connection(self) -> tuple[bool, str | None, float | None]:
        """Check if Radarr is available. Uses v3 API."""
        import time
        start_time = time.monotonic()
        try:
            result = await self.get("/api/v3/system/status")
            elapsed = (time.monotonic() - start_time) * 1000
            version = result.get("version") if isinstance(result, dict) else None
            return True, version, round(elapsed, 2)
        except Exception as e:
            elapsed = (time.monotonic() - start_time) * 1000
            logger.warning("Radarr health check failed", error=str(e))
            return False, None, round(elapsed, 2)
