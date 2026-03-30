"""Search service for finding content."""

import asyncio
import re
from typing import Optional

import structlog

from bot.clients.prowlarr import ProwlarrClient
from bot.clients.radarr import RadarrClient
from bot.clients.sonarr import SonarrClient
from bot.models import ContentType, MovieInfo, SearchResult, SeriesInfo
from bot.services.scoring import ScoringService

logger = structlog.get_logger()


class SearchService:
    """Service for searching content across Prowlarr, Radarr, and Sonarr."""

    def __init__(
        self,
        prowlarr: ProwlarrClient,
        radarr: RadarrClient,
        sonarr: SonarrClient,
        scoring: Optional[ScoringService] = None,
    ):
        self.prowlarr = prowlarr
        self.radarr = radarr
        self.sonarr = sonarr
        self.scoring = scoring or ScoringService()

    async def detect_content_type(self, query: str) -> ContentType:
        """
        Try to detect if query is for a movie or series.

        Args:
            query: User's search query

        Returns:
            Detected ContentType
        """
        query_lower = query.lower()

        # Series indicators
        series_patterns = [
            r"s\d{1,2}",  # S01, S1
            r"s\d{1,2}e\d{1,3}",  # S01E01
            r"season\s*\d+",  # Season 1
            r"series\s*\d+",  # Series 1 (UK style)
            r"\d{1,2}x\d{1,3}",  # 1x01
            r"сезон",  # Russian "season"
            r"серия",  # Russian "episode"
        ]

        for pattern in series_patterns:
            if re.search(pattern, query_lower):
                return ContentType.SERIES

        # Try to identify by looking up in both services in parallel
        movie_task = asyncio.create_task(self.radarr.lookup_movie(query))
        series_task = asyncio.create_task(self.sonarr.lookup_series(query))
        movies_result, series_result = await asyncio.gather(movie_task, series_task, return_exceptions=True)
        movies = movies_result if not isinstance(movies_result, Exception) else []
        series = series_result if not isinstance(series_result, Exception) else []

        if isinstance(movies_result, Exception):
            logger.warning("Radarr lookup failed during type detection", error=str(movies_result))
        if isinstance(series_result, Exception):
            logger.warning("Sonarr lookup failed during type detection", error=str(series_result))

        if movies:
            for movie in movies[:3]:
                if self._title_matches(query, movie.title, movie.year):
                    return ContentType.MOVIE

        if series:
            for s in series[:3]:
                if self._title_matches(query, s.title, s.year):
                    return ContentType.SERIES

        return ContentType.UNKNOWN

    def _title_matches(self, query: str, title: str, year: Optional[int]) -> bool:
        """Check if a title matches the query reasonably well."""
        query_lower = query.lower()
        title_lower = title.lower()

        # Extract year from query if present
        year_match = re.search(r"(\d{4})", query)
        query_year = int(year_match.group(1)) if year_match else None

        # Remove year from query for title comparison
        year_match_clean = re.search(r"[\(\[]?(\d{4})[\)\]]?", query_lower)
        if year_match_clean and 1900 <= int(year_match_clean.group(1)) <= 2100:
            query_clean = (query_lower[:year_match_clean.start()] + query_lower[year_match_clean.end():]).strip()
        else:
            query_clean = query_lower

        # Check title similarity
        if query_clean in title_lower or title_lower in query_clean:
            # If years are specified, they should match
            if query_year and year:
                return abs(query_year - year) <= 1
            return True

        return False

    async def search_releases(
        self,
        query: str,
        content_type: ContentType = ContentType.UNKNOWN,
        sort_by_score: bool = True,
    ) -> list[SearchResult]:
        """
        Search for releases using Prowlarr.

        Args:
            query: Search query
            content_type: Type of content to search for
            sort_by_score: Whether to sort results by score

        Returns:
            List of SearchResult objects
        """
        log = logger.bind(query=query, content_type=content_type.value)
        log.info("Searching for releases")

        results = await self.prowlarr.search(query, content_type)

        if not results:
            log.info("No results found")
            return []

        # Filter by detected type if we know it
        if content_type != ContentType.UNKNOWN:
            results = [r for r in results if r.detected_type == content_type or r.detected_type == ContentType.UNKNOWN]

        if sort_by_score:
            results = self.scoring.sort_results(results, content_type)

        log.info("Search completed", result_count=len(results))
        return results

    async def lookup_movie(self, query: str) -> list[MovieInfo]:
        """
        Look up movies in Radarr.

        Args:
            query: Movie title to search

        Returns:
            List of MovieInfo objects
        """
        return await self.radarr.lookup_movie(query)

    async def lookup_series(self, query: str) -> list[SeriesInfo]:
        """
        Look up series in Sonarr.

        Args:
            query: Series title to search

        Returns:
            List of SeriesInfo objects
        """
        return await self.sonarr.lookup_series(query)

    async def get_movie_by_tmdb(self, tmdb_id: int) -> Optional[MovieInfo]:
        """Get movie info by TMDB ID."""
        # First check if already in library
        movie = await self.radarr.get_movie_by_tmdb(tmdb_id)
        if movie:
            return movie

        # Otherwise lookup
        return await self.radarr.lookup_movie_by_tmdb(tmdb_id)

    async def get_series_by_tvdb(self, tvdb_id: int) -> Optional[SeriesInfo]:
        """Get series info by TVDB ID."""
        # First check if already in library
        series = await self.sonarr.get_series_by_tvdb(tvdb_id)
        if series:
            return series

        # Otherwise lookup
        return await self.sonarr.lookup_series_by_tvdb(tvdb_id)

    def parse_query(self, query: str) -> dict:
        """
        Parse search query to extract metadata.

        Args:
            query: Raw search query

        Returns:
            Dict with parsed components: title, year, season, episode, quality
        """
        result = {
            "original": query,
            "title": query,
            "year": None,
            "season": None,
            "episode": None,
            "quality": None,
        }

        query_lower = query.lower()

        # Extract year
        year_match = re.search(r"[\(\[]?(\d{4})[\)\]]?", query)
        if year_match:
            year = int(year_match.group(1))
            if 1900 <= year <= 2100:
                result["year"] = year
                # Remove year from title
                result["title"] = re.sub(r"[\(\[]?\d{4}[\)\]]?", "", result["title"]).strip()

        # Extract season/episode
        se_match = re.search(r"s(\d{1,2})(?:e(\d{1,3}))?", query_lower)
        if se_match:
            result["season"] = int(se_match.group(1))
            if se_match.group(2):
                result["episode"] = int(se_match.group(2))
            # Remove from title
            result["title"] = re.sub(r"s\d{1,2}(?:e\d{1,3})?", "", result["title"], flags=re.IGNORECASE).strip()

        # Check for season word
        season_match = re.search(r"(?:season|сезон)\s*(\d+)", query_lower)
        if season_match and result["season"] is None:
            result["season"] = int(season_match.group(1))
            result["title"] = re.sub(r"(?:season|сезон)\s*\d+", "", result["title"], flags=re.IGNORECASE).strip()

        # Extract quality preference
        quality_patterns = ["2160p", "4k", "1080p", "720p", "480p"]
        for q in quality_patterns:
            if q in query_lower:
                result["quality"] = q if q != "4k" else "2160p"
                result["title"] = re.sub(re.escape(q), "", result["title"], flags=re.IGNORECASE).strip()
                break

        # Clean up title
        result["title"] = re.sub(r"\s+", " ", result["title"]).strip()

        return result
