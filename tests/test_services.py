"""Tests for service layer."""

import asyncio

import pytest
from unittest.mock import AsyncMock

from bot.models import ContentType, QualityInfo, SearchResult
from bot.services.scoring import ScoringService
from bot.services.search_service import SearchService


@pytest.mark.asyncio
async def test_detection_propagates_task_cancellation():
    """BUG-04: shutdown cancellation must not become a normal detection result.

    Rollback 2026-08-10: detection fans out to Radarr/Sonarr/Lidarr again, and
    `gather(return_exceptions=True)` captures a child's own CancelledError into
    the results list instead of propagating it — so `_lookup_branch` re-raises
    it explicitly and the caller re-raises again. This test pins that path.
    """
    radarr = AsyncMock()

    async def cancelled_lookup(*_a, **_kw):
        raise asyncio.CancelledError()

    radarr.lookup_movie = AsyncMock(side_effect=cancelled_lookup)
    sonarr = AsyncMock()
    sonarr.lookup_series = AsyncMock(return_value=[])
    svc = SearchService(radarr, sonarr)

    with pytest.raises(asyncio.CancelledError):
        await svc.detect_content_type("anything at all")


class TestSearchService:
    """Test search service functionality."""

    @pytest.fixture
    def mock_clients(self):
        """Radarr/Sonarr mocks that find nothing, so query heuristics decide."""
        radarr = AsyncMock()
        radarr.lookup_movie = AsyncMock(return_value=[])
        sonarr = AsyncMock()
        sonarr.lookup_series = AsyncMock(return_value=[])
        return radarr, sonarr

    @pytest.fixture
    def search_service(self, mock_clients):
        """Search service backed by the mocked *arr clients."""
        radarr, sonarr = mock_clients
        return SearchService(radarr, sonarr)

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

    @pytest.mark.parametrize("query,season,title", [
        ("Тед Лассо 4 сезон", 4, "Тед Лассо"),
        ("Пацаны 3-й сезон", 3, "Пацаны"),
        ("Ведьмак 2 сезон 1080p", 2, "Ведьмак"),
    ])
    def test_parse_query_russian_season_before_the_word(self, search_service, query, season, title):
        """Живой прогон 2026-08-12: «Тед Лассо 4 сезон» давал season=None —
        шаблон ловил только порядок «сезон N», а по-русски обычный порядок
        обратный. Цена промаха: бот спрашивал сезон, который уже назвали.

        Третий случай ловит заодно и «сезон 1080p» → season=1080: шаблон
        «сезон N» не был ограничен по числу цифр."""
        result = search_service.parse_query(query)

        assert result["season"] == season
        assert result["title"] == title

    @pytest.mark.asyncio
    async def test_detect_content_type_with_season(self, search_service):
        """A season marker in the query means SERIES even with no metadata hit."""
        content_type = (await search_service.detect_content_type("Show S01")).content_type
        assert content_type == ContentType.SERIES

    @pytest.mark.asyncio
    async def test_detect_content_type_with_episode(self, search_service):
        """Test content type detection with episode in query."""
        content_type = (await search_service.detect_content_type("Show S01E05")).content_type
        assert content_type == ContentType.SERIES

    @pytest.mark.asyncio
    async def test_search_releases_for_a_movie_reads_radarr(self, search_service, mock_clients):
        """Releases come from Radarr's interactive search, keyed by movie id.

        Rollback 2026-08-10: this replaces the removed backend's
        `search_releases(title_id, ...)`. Radarr returns releases it has
        already judged, so the service only orders them.
        """
        radarr, _ = mock_clients
        radarr.get_releases = AsyncMock(return_value=[
            SearchResult(
                guid="1", title="Movie.1080p", indexer="Test",
                quality=QualityInfo(resolution="1080p"), origin="arr",
            ),
            SearchResult(
                guid="2", title="Movie.720p", indexer="Test",
                quality=QualityInfo(resolution="720p"), origin="arr",
            ),
        ])

        results = await search_service.search_releases_for_title(ContentType.MOVIE, arr_id=15)

        assert len(results) == 2
        radarr.get_releases.assert_awaited_once_with(15)

    @pytest.mark.asyncio
    async def test_search_releases_for_a_series_reads_sonarr(self, search_service, mock_clients):
        """A season pick must reach Sonarr rather than filtering locally."""
        _, sonarr = mock_clients
        sonarr.get_releases = AsyncMock(return_value=[])

        await search_service.search_releases_for_title(ContentType.SERIES, arr_id=3, season=2)

        sonarr.get_releases.assert_awaited_once_with(3, season_number=2)


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

    def test_sort_empty_list(self, scoring):
        """Test sorting an empty list."""
        sorted_results = scoring.sort_results([])
        assert sorted_results == []

    # DEAD-06: filter_by_quality/get_best_result removed (unused dead code).

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
