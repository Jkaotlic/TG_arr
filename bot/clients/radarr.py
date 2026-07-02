"""Radarr API client."""

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import structlog

from bot.clients.base import APIError, ArrBaseClient
from bot.models import MovieInfo

logger = structlog.get_logger()


class RadarrClient(ArrBaseClient):
    """Client for Radarr API."""

    _api_prefix = "/api/v3"

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

        raise APIError("Не удалось добавить фильм в Radarr")

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

    # check_connection/push_release/get_quality_profiles/get_root_folders
    # inherited from ArrBaseClient (r5 follow-up: ArrBaseClient dedup) — Radarr
    # uses the default _api_prefix = "/api/v3" set on ArrBaseClient.
