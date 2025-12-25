"""Tests for service layer."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from bot.models import ContentType, MovieInfo, QualityInfo, SearchResult, SeriesInfo
from bot.services.scoring import ScoringService
from bot.services.search_service import SearchService


class TestSearchService:
    """Test search service functionality."""

    @pytest.fixture
    def mock_clients(self):
        """Create mock clients."""
        prowlarr = AsyncMock()
        radarr = AsyncMock()
        sonarr = AsyncMock()
        return prowlarr, radarr, sonarr

    @pytest.fixture
    def search_service(self, mock_clients):
        """Create search service with mock clients."""
        prowlarr, radarr, sonarr = mock_clients
        return SearchService(prowlarr, radarr, sonarr)

    def test_parse_query_simple(self, search_service):
        """Test parsing a simple query."""
        result = search_service.parse_query("test movie")

        assert result["original"] == "test movie"
        assert result["title"] == "test movie"
        assert result["year"] is None
        assert result["season"] is None

    def test_parse_query_with_year(self, search_service):
        """Test parsing query with year."""
        result = search_service.parse_query("Dune 2021")

        assert result["title"] == "Dune"
        assert result["year"] == 2021

    def test_parse_query_with_year_parentheses(self, search_service):
        """Test parsing query with year in parentheses."""
        result = search_service.parse_query("Dune (2021)")

        assert result["title"] == "Dune"
        assert result["year"] == 2021

    def test_parse_query_with_season(self, search_service):
        """Test parsing query with season."""
        result = search_service.parse_query("Breaking Bad S02")

        assert "Breaking Bad" in result["title"]
        assert result["season"] == 2
        assert result["episode"] is None

    def test_parse_query_with_season_episode(self, search_service):
        """Test parsing query with season and episode."""
        result = search_service.parse_query("The Office S03E05")

        assert result["season"] == 3
        assert result["episode"] == 5

    def test_parse_query_with_quality(self, search_service):
        """Test parsing query with quality preference."""
        result = search_service.parse_query("Movie 1080p")

        assert result["quality"] == "1080p"
        assert "1080p" not in result["title"]

    def test_parse_query_with_4k(self, search_service):
        """Test parsing query with 4K."""
        result = search_service.parse_query("Movie 4k")

        assert result["quality"] == "2160p"

    def test_parse_query_russian_season(self, search_service):
        """Test parsing Russian season format."""
        result = search_service.parse_query("Пацаны сезон 3")

        assert result["season"] == 3

    @pytest.mark.asyncio
    async def test_detect_content_type_with_season(self, search_service):
        """Test content type detection with season in query."""
        content_type = await search_service.detect_content_type("Show S01")
        assert content_type == ContentType.SERIES

    @pytest.mark.asyncio
    async def test_detect_content_type_with_episode(self, search_service):
        """Test content type detection with episode in query."""
        content_type = await search_service.detect_content_type("Show S01E05")
        assert content_type == ContentType.SERIES

    @pytest.mark.asyncio
    async def test_search_releases(self, search_service, mock_clients):
        """Test searching for releases."""
        prowlarr, _, _ = mock_clients

        prowlarr.search.return_value = [
            SearchResult(
                guid="1",
                title="Movie.1080p",
                indexer="Test",
                quality=QualityInfo(resolution="1080p"),
            ),
            SearchResult(
                guid="2",
                title="Movie.720p",
                indexer="Test",
                quality=QualityInfo(resolution="720p"),
            ),
        ]

        results = await search_service.search_releases("test", ContentType.MOVIE)

        assert len(results) == 2
        prowlarr.search.assert_called_once()

    @pytest.mark.asyncio
    async def test_lookup_movie(self, search_service, mock_clients):
        """Test looking up a movie."""
        _, radarr, _ = mock_clients

        radarr.lookup_movie.return_value = [
            MovieInfo(tmdb_id=123, title="Test Movie", year=2024),
        ]

        movies = await search_service.lookup_movie("test")

        assert len(movies) == 1
        assert movies[0].tmdb_id == 123
        radarr.lookup_movie.assert_called_once_with("test")

    @pytest.mark.asyncio
    async def test_lookup_series(self, search_service, mock_clients):
        """Test looking up a series."""
        _, _, sonarr = mock_clients

        sonarr.lookup_series.return_value = [
            SeriesInfo(tvdb_id=456, title="Test Series"),
        ]

        series = await search_service.lookup_series("test")

        assert len(series) == 1
        assert series[0].tvdb_id == 456
        sonarr.lookup_series.assert_called_once_with("test")


class TestScoringServiceEdgeCases:
    """Additional edge case tests for scoring service."""

    @pytest.fixture
    def scoring(self):
        return ScoringService()

    def test_score_with_null_seeders(self, scoring):
        """Test scoring when seeders is None."""
        result = SearchResult(
            guid="test",
            title="Test.1080p",
            seeders=None,
            quality=QualityInfo(resolution="1080p"),
        )

        score = scoring.calculate_score(result)
        assert isinstance(score, int)

    def test_score_with_zero_size(self, scoring):
        """Test scoring when size is zero."""
        result = SearchResult(
            guid="test",
            title="Test.1080p",
            size=0,
            quality=QualityInfo(resolution="1080p"),
        )

        score = scoring.calculate_score(result)
        assert isinstance(score, int)

    def test_score_with_empty_quality(self, scoring):
        """Test scoring with no quality info."""
        result = SearchResult(
            guid="test",
            title="Unknown Release",
            quality=QualityInfo(),
        )

        score = scoring.calculate_score(result)
        assert score == 50  # Base score

    def test_score_bounds(self, scoring):
        """Test that score stays within bounds."""
        # Very good release
        good_result = SearchResult(
            guid="test",
            title="Movie.2160p.REMUX.HDR.Atmos",
            seeders=1000,
            size=50 * 1024 * 1024 * 1024,
            quality=QualityInfo(
                resolution="2160p",
                source="BluRay",
                codec="x265",
                hdr="HDR10+",
                audio="Atmos",
                is_remux=True,
            ),
        )
        good_score = scoring.calculate_score(good_result)
        assert good_score <= 150

        # Very bad release
        bad_result = SearchResult(
            guid="test",
            title="Movie.CAM.SAMPLE.TRAILER",
            seeders=0,
            size=100 * 1024 * 1024,
            quality=QualityInfo(source="CAM"),
        )
        bad_score = scoring.calculate_score(bad_result)
        assert bad_score >= -100

    def test_filter_empty_list(self, scoring):
        """Test filtering an empty list."""
        filtered = scoring.filter_by_quality([])
        assert filtered == []

    def test_sort_empty_list(self, scoring):
        """Test sorting an empty list."""
        sorted_results = scoring.sort_results([])
        assert sorted_results == []

    def test_get_best_empty_list(self, scoring):
        """Test getting best from empty list."""
        best = scoring.get_best_result([])
        assert best is None

    def test_combined_hdr_formats(self, scoring):
        """Test combined HDR formats (DV+HDR10)."""
        result = SearchResult(
            guid="test",
            title="Movie.2160p.BluRay.DV.HDR10",
            quality=QualityInfo(
                resolution="2160p",
                source="BluRay",
                hdr="DV+HDR10",
            ),
        )

        score = scoring.calculate_score(result)
        # Should get bonuses for both
        assert score > 50

    def test_series_size_thresholds(self, scoring):
        """Test size thresholds differ for series."""
        # For movies, 500MB is suspiciously small
        movie_result = SearchResult(
            guid="test",
            title="Movie.1080p",
            size=500 * 1024 * 1024,
            quality=QualityInfo(resolution="1080p"),
        )
        movie_score = scoring.calculate_score(movie_result, ContentType.MOVIE)

        # For series episodes, 500MB is reasonable
        series_result = SearchResult(
            guid="test",
            title="Show.S01E01.1080p",
            size=500 * 1024 * 1024,
            quality=QualityInfo(resolution="1080p"),
            is_season_pack=False,
        )
        series_score = scoring.calculate_score(series_result, ContentType.SERIES)

        # Series shouldn't be penalized as heavily for smaller size
        assert series_score >= movie_score
