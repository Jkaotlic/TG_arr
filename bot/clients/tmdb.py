"""TMDb API client for trending/popular content."""

from typing import Any, Optional

import structlog

from bot.clients.base import BaseAPIClient
from bot.models import MovieInfo, SeriesInfo

logger = structlog.get_logger()


class TMDbClient(BaseAPIClient):
    """Client for TMDb API to fetch trending/popular content."""

    def __init__(self, api_key: str, language: str = "en-US"):
        """Initialize TMDb client.

        Args:
            api_key: TMDb API key (v3)
            language: Language for TMDb content (e.g., ru-RU, en-US)
        """
        super().__init__("https://api.themoviedb.org/3", api_key, "TMDb")
        self.language = language

    def _get_headers(self) -> dict[str, str]:
        """Override to not include X-Api-Key header (TMDb uses query param)."""
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "TG_arr-bot/1.0",
        }

    async def get_trending_movies(self, time_window: str = "week", page: int = 1) -> list[MovieInfo]:
        """Get trending movies.

        Args:
            time_window: 'day' or 'week'
            page: Page number (1-1000)

        Returns:
            List of MovieInfo objects
        """
        log = logger.bind(time_window=time_window, page=page)
        log.info("Fetching trending movies from TMDb")

        params = {"api_key": self.api_key, "page": page, "language": self.language}
        result = await self.get(f"/trending/movie/{time_window}", params=params)

        if not isinstance(result, dict) or "results" not in result:
            return []

        movies = []
        for item in result["results"]:
            try:
                movie = self._parse_movie(item)
                if movie:
                    movies.append(movie)
            except Exception as e:
                log.warning("Failed to parse movie", error=str(e))

        log.info("Trending movies fetched", count=len(movies))
        return movies

    async def get_popular_movies(self, page: int = 1) -> list[MovieInfo]:
        """Get popular movies.

        Args:
            page: Page number (1-500)

        Returns:
            List of MovieInfo objects
        """
        log = logger.bind(page=page)
        log.info("Fetching popular movies from TMDb")

        params = {"api_key": self.api_key, "page": page, "language": self.language}
        result = await self.get("/movie/popular", params=params)

        if not isinstance(result, dict) or "results" not in result:
            return []

        movies = []
        for item in result["results"]:
            try:
                movie = self._parse_movie(item)
                if movie:
                    movies.append(movie)
            except Exception as e:
                log.warning("Failed to parse movie", error=str(e))

        log.info("Popular movies fetched", count=len(movies))
        return movies

    async def get_trending_series(self, time_window: str = "week", page: int = 1) -> list[SeriesInfo]:
        """Get trending TV series.

        Args:
            time_window: 'day' or 'week'
            page: Page number (1-1000)

        Returns:
            List of SeriesInfo objects
        """
        log = logger.bind(time_window=time_window, page=page)
        log.info("Fetching trending series from TMDb")

        params = {"api_key": self.api_key, "page": page, "language": self.language}
        result = await self.get(f"/trending/tv/{time_window}", params=params)

        if not isinstance(result, dict) or "results" not in result:
            return []

        series_list = []
        for item in result["results"]:
            try:
                series = self._parse_series(item)
                if series:
                    series_list.append(series)
            except Exception as e:
                log.warning("Failed to parse series", error=str(e))

        log.info("Trending series fetched", count=len(series_list))
        return series_list

    async def get_popular_series(self, page: int = 1) -> list[SeriesInfo]:
        """Get popular TV series.

        Args:
            page: Page number (1-500)

        Returns:
            List of SeriesInfo objects
        """
        log = logger.bind(page=page)
        log.info("Fetching popular series from TMDb")

        params = {"api_key": self.api_key, "page": page, "language": self.language}
        result = await self.get("/tv/popular", params=params)

        if not isinstance(result, dict) or "results" not in result:
            return []

        series_list = []
        for item in result["results"]:
            try:
                series = self._parse_series(item)
                if series:
                    series_list.append(series)
            except Exception as e:
                log.warning("Failed to parse series", error=str(e))

        log.info("Popular series fetched", count=len(series_list))
        return series_list

    def _parse_movie(self, item: dict) -> Optional[MovieInfo]:
        """Parse TMDb movie data to MovieInfo."""
        if not item.get("id"):
            return None

        # Build poster URL if poster_path exists
        poster_url = None
        if item.get("poster_path"):
            poster_url = f"https://image.tmdb.org/t/p/w500{item['poster_path']}"

        fanart_url = None
        if item.get("backdrop_path"):
            fanart_url = f"https://image.tmdb.org/t/p/original{item['backdrop_path']}"

        # Get year from release_date
        year = None
        if item.get("release_date"):
            try:
                year = int(item["release_date"][:4])
            except (ValueError, IndexError):
                pass

        return MovieInfo(
            title=item.get("title", "Unknown"),
            tmdb_id=item["id"],
            year=year or 0,
            overview=item.get("overview", ""),
            genres=[],  # TMDb returns genre_ids, would need lookup
            studio="",
            runtime=0,
            imdb_id=None,
            poster_url=poster_url,
            fanart_url=fanart_url,
            ratings={"tmdb": {"value": item.get("vote_average", 0.0), "votes": item.get("vote_count", 0)}},
        )

    def _parse_series(self, item: dict) -> Optional[SeriesInfo]:
        """Parse TMDb TV series data to SeriesInfo."""
        if not item.get("id"):
            return None

        # Build poster URL if poster_path exists
        poster_url = None
        if item.get("poster_path"):
            poster_url = f"https://image.tmdb.org/t/p/w500{item['poster_path']}"

        fanart_url = None
        if item.get("backdrop_path"):
            fanart_url = f"https://image.tmdb.org/t/p/original{item['backdrop_path']}"

        # Get year from first_air_date
        year = None
        if item.get("first_air_date"):
            try:
                year = int(item["first_air_date"][:4])
            except (ValueError, IndexError):
                pass

        # TMDb uses 'id' for series, but we need TVDB ID for Sonarr
        # We'll store TMDb ID temporarily and let Sonarr lookup handle TVDB conversion
        return SeriesInfo(
            title=item.get("name", "Unknown"),
            tvdb_id=0,  # Will be looked up via Sonarr
            tmdb_id=item["id"],  # Store TMDb ID
            year=year or 0,
            overview=item.get("overview", ""),
            network=item.get("networks", [{}])[0].get("name", "") if item.get("networks") else "",
            status="",
            genres=[],
            poster_url=poster_url,
            fanart_url=fanart_url,
            ratings={"tmdb": {"value": item.get("vote_average", 0.0), "votes": item.get("vote_count", 0)}},
        )
