"""Tests for the scoring service."""

import pytest

from bot.models import ContentType, QualityInfo, SearchResult
from bot.services.scoring import ScoringService, ScoringWeights


@pytest.fixture
def scoring_service():
    """Create a scoring service with default weights."""
    return ScoringService()


@pytest.fixture
def base_result():
    """Create a base search result for testing."""
    return SearchResult(
        guid="test-guid-123",
        title="Test.Movie.2024.1080p.BluRay.x264-GROUP",
        indexer="TestIndexer",
        size=5 * 1024 * 1024 * 1024,  # 5 GB
        seeders=100,
        protocol="torrent",
        quality=QualityInfo(
            resolution="1080p",
            source="BluRay",
            codec="x264",
        ),
    )


class TestScoringService:
    """Test cases for ScoringService."""

    def test_base_score_is_50(self, scoring_service, base_result):
        """Test that base score starts at 50."""
        # Remove all quality info to get base score
        base_result.quality = QualityInfo()
        base_result.seeders = 0

        score = scoring_service.calculate_score(base_result)
        assert score == 50

    def test_resolution_scoring_2160p(self, scoring_service, base_result):
        """Test 2160p resolution bonus."""
        base_result.quality.resolution = "2160p"
        score = scoring_service.calculate_score(base_result)

        base_result.quality.resolution = "1080p"
        score_1080p = scoring_service.calculate_score(base_result)

        assert score > score_1080p

    def test_resolution_scoring_1080p(self, scoring_service, base_result):
        """Test 1080p resolution bonus."""
        base_result.quality.resolution = "1080p"
        score = scoring_service.calculate_score(base_result)

        base_result.quality.resolution = "720p"
        score_720p = scoring_service.calculate_score(base_result)

        assert score > score_720p

    def test_source_bluray_bonus(self, scoring_service, base_result):
        """Test BluRay source bonus."""
        base_result.quality.source = "BluRay"
        score_bluray = scoring_service.calculate_score(base_result)

        base_result.quality.source = "HDTV"
        score_hdtv = scoring_service.calculate_score(base_result)

        assert score_bluray > score_hdtv

    def test_source_cam_penalty(self, scoring_service, base_result):
        """Test CAM source penalty."""
        base_result.quality.source = "BluRay"
        score_bluray = scoring_service.calculate_score(base_result)

        base_result.quality.source = "CAM"
        score_cam = scoring_service.calculate_score(base_result)

        assert score_cam < score_bluray
        assert score_cam < 50  # Should be below base score

    def test_codec_x265_bonus(self, scoring_service, base_result):
        """Test x265 codec bonus."""
        base_result.quality.codec = "x265"
        score_x265 = scoring_service.calculate_score(base_result)

        base_result.quality.codec = "x264"
        score_x264 = scoring_service.calculate_score(base_result)

        assert score_x265 > score_x264

    def test_hdr_bonus(self, scoring_service, base_result):
        """Test HDR bonus."""
        base_result.quality.hdr = "HDR10"
        score_hdr = scoring_service.calculate_score(base_result)

        base_result.quality.hdr = None
        score_no_hdr = scoring_service.calculate_score(base_result)

        assert score_hdr > score_no_hdr

    def test_dolby_vision_bonus_higher_than_hdr(self, scoring_service, base_result):
        """Test Dolby Vision bonus is higher than regular HDR."""
        base_result.quality.hdr = "DV"
        score_dv = scoring_service.calculate_score(base_result)

        base_result.quality.hdr = "HDR"
        score_hdr = scoring_service.calculate_score(base_result)

        assert score_dv > score_hdr

    def test_seeder_bonus(self, scoring_service, base_result):
        """Test seeder bonus."""
        base_result.seeders = 100
        score_100 = scoring_service.calculate_score(base_result)

        base_result.seeders = 10
        score_10 = scoring_service.calculate_score(base_result)

        base_result.seeders = 0
        score_0 = scoring_service.calculate_score(base_result)

        assert score_100 > score_10 > score_0

    def test_seeder_bonus_capped(self, scoring_service, base_result):
        """Test seeder bonus is capped."""
        base_result.seeders = 1000
        score_1000 = scoring_service.calculate_score(base_result)

        base_result.seeders = 10000
        score_10000 = scoring_service.calculate_score(base_result)

        # Should be the same due to cap
        assert score_1000 == score_10000

    def test_size_too_small_penalty(self, scoring_service, base_result):
        """Test penalty for suspiciously small files."""
        base_result.size = 5 * 1024 * 1024 * 1024  # 5 GB
        score_normal = scoring_service.calculate_score(base_result, ContentType.MOVIE)

        base_result.size = 100 * 1024 * 1024  # 100 MB (too small for movie)
        score_small = scoring_service.calculate_score(base_result, ContentType.MOVIE)

        assert score_small < score_normal

    def test_size_too_large_penalty(self, scoring_service, base_result):
        """Test penalty for very large files."""
        base_result.size = 5 * 1024 * 1024 * 1024  # 5 GB
        score_normal = scoring_service.calculate_score(base_result, ContentType.MOVIE)

        base_result.size = 100 * 1024 * 1024 * 1024  # 100 GB (too large)
        score_large = scoring_service.calculate_score(base_result, ContentType.MOVIE)

        assert score_large < score_normal

    def test_bad_keyword_sample_penalty(self, scoring_service, base_result):
        """Test penalty for 'sample' keyword."""
        base_result.title = "Test.Movie.2024.1080p.BluRay.x264-GROUP"
        score_normal = scoring_service.calculate_score(base_result)

        base_result.title = "Test.Movie.2024.1080p.BluRay.x264-GROUP-SAMPLE"
        score_sample = scoring_service.calculate_score(base_result)

        assert score_sample < score_normal
        assert score_sample < 0  # Should be heavily penalized

    def test_bad_keyword_trailer_penalty(self, scoring_service, base_result):
        """Test penalty for 'trailer' keyword."""
        base_result.title = "Test.Movie.2024.1080p.BluRay.x264-GROUP"
        score_normal = scoring_service.calculate_score(base_result)

        base_result.title = "Test.Movie.2024.TRAILER.1080p.BluRay.x264"
        score_trailer = scoring_service.calculate_score(base_result)

        assert score_trailer < score_normal

    def test_repack_bonus(self, scoring_service, base_result):
        """Test REPACK bonus."""
        base_result.quality.is_repack = True
        score_repack = scoring_service.calculate_score(base_result)

        base_result.quality.is_repack = False
        score_normal = scoring_service.calculate_score(base_result)

        assert score_repack > score_normal

    def test_proper_bonus(self, scoring_service, base_result):
        """Test PROPER bonus."""
        base_result.quality.is_proper = True
        score_proper = scoring_service.calculate_score(base_result)

        base_result.quality.is_proper = False
        score_normal = scoring_service.calculate_score(base_result)

        assert score_proper > score_normal

    def test_remux_bonus(self, scoring_service, base_result):
        """Test REMUX bonus."""
        base_result.quality.is_remux = True
        base_result.quality.source = "BluRay"
        score_remux = scoring_service.calculate_score(base_result)

        base_result.quality.is_remux = False
        score_normal = scoring_service.calculate_score(base_result)

        assert score_remux > score_normal

    def test_sort_results(self, scoring_service):
        """Test sorting results by score."""
        results = [
            SearchResult(
                guid="1",
                title="Low.Quality.CAM",
                quality=QualityInfo(source="CAM", resolution="720p"),
            ),
            SearchResult(
                guid="2",
                title="High.Quality.2160p.BluRay.x265.HDR",
                quality=QualityInfo(
                    resolution="2160p",
                    source="BluRay",
                    codec="x265",
                    hdr="HDR",
                ),
                seeders=100,
            ),
            SearchResult(
                guid="3",
                title="Medium.Quality.1080p.WEB-DL",
                quality=QualityInfo(
                    resolution="1080p",
                    source="WEB-DL",
                ),
                seeders=50,
            ),
        ]

        sorted_results = scoring_service.sort_results(results)

        # Best quality should be first
        assert sorted_results[0].guid == "2"
        # CAM should be last
        assert sorted_results[-1].guid == "1"

    def test_get_best_result(self, scoring_service):
        """Test getting the best result."""
        results = [
            SearchResult(
                guid="1",
                title="Low.Quality",
                quality=QualityInfo(source="CAM"),
            ),
            SearchResult(
                guid="2",
                title="High.Quality.1080p.BluRay",
                quality=QualityInfo(resolution="1080p", source="BluRay"),
                seeders=100,
            ),
        ]

        best = scoring_service.get_best_result(results)
        assert best is not None
        assert best.guid == "2"

    def test_get_best_result_min_score(self, scoring_service):
        """Test getting best result with minimum score requirement."""
        results = [
            SearchResult(
                guid="1",
                title="Low.Quality.CAM",
                quality=QualityInfo(source="CAM"),
            ),
        ]

        # CAM should have low score
        best = scoring_service.get_best_result(results, min_score=50)
        assert best is None

    def test_filter_by_quality_resolution(self, scoring_service):
        """Test filtering by preferred resolution."""
        results = [
            SearchResult(
                guid="1",
                title="720p.Release",
                quality=QualityInfo(resolution="720p"),
            ),
            SearchResult(
                guid="2",
                title="1080p.Release",
                quality=QualityInfo(resolution="1080p"),
            ),
            SearchResult(
                guid="3",
                title="2160p.Release",
                quality=QualityInfo(resolution="2160p"),
            ),
        ]

        filtered = scoring_service.filter_by_quality(results, preferred_resolution="1080p")
        assert len(filtered) == 1
        assert filtered[0].guid == "2"

    def test_filter_by_quality_exclude_cam(self, scoring_service):
        """Test filtering to exclude CAM/TS releases."""
        results = [
            SearchResult(
                guid="1",
                title="CAM.Release",
                quality=QualityInfo(source="CAM"),
            ),
            SearchResult(
                guid="2",
                title="TS.Release",
                quality=QualityInfo(source="TS"),
            ),
            SearchResult(
                guid="3",
                title="BluRay.Release",
                quality=QualityInfo(source="BluRay"),
            ),
        ]

        filtered = scoring_service.filter_by_quality(results, exclude_cam_ts=True)
        assert len(filtered) == 1
        assert filtered[0].guid == "3"

    def test_filter_by_quality_min_seeders(self, scoring_service):
        """Test filtering by minimum seeders."""
        results = [
            SearchResult(
                guid="1",
                title="Low.Seeders",
                seeders=5,
            ),
            SearchResult(
                guid="2",
                title="High.Seeders",
                seeders=100,
            ),
        ]

        filtered = scoring_service.filter_by_quality(results, min_seeders=10)
        assert len(filtered) == 1
        assert filtered[0].guid == "2"


class TestScoringWeights:
    """Test cases for ScoringWeights customization."""

    def test_custom_weights(self):
        """Test custom scoring weights."""
        custom_weights = ScoringWeights(
            resolution_1080p=50,  # Higher than default
            source_bluray=40,
        )

        service = ScoringService(weights=custom_weights)

        result = SearchResult(
            guid="test",
            title="Test.1080p.BluRay",
            quality=QualityInfo(resolution="1080p", source="BluRay"),
        )

        score = service.calculate_score(result)

        # Should be higher than with default weights
        default_service = ScoringService()
        default_score = default_service.calculate_score(result)

        assert score > default_score

    def test_custom_bad_keywords(self):
        """Test custom bad keywords."""
        custom_weights = ScoringWeights(
            bad_keywords={
                "badword": -100,
            }
        )

        service = ScoringService(weights=custom_weights)

        result = SearchResult(
            guid="test",
            title="Test.Movie.badword.1080p",
            quality=QualityInfo(resolution="1080p"),
        )

        score = service.calculate_score(result)

        # Should be penalized
        result_clean = SearchResult(
            guid="test2",
            title="Test.Movie.1080p",
            quality=QualityInfo(resolution="1080p"),
        )
        score_clean = service.calculate_score(result_clean)

        assert score < score_clean
