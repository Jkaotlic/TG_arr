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

    # DEAD-06: get_best_result/filter_by_quality were unused-in-production
    # dead code — removed from ScoringService. Their one useful idea
    # (preferred_resolution affecting ranking) is now covered by
    # TestPreferredResolutionBonus below, wired into calculate_score/sort_results.


class TestPreferredResolutionBonus:
    """DEAD-06: preferred_resolution (settings 'Качество') is wired into
    calculate_score as a bonus, and sort_results/search.py propagate it."""

    def test_calculate_score_bonus_for_matching_resolution(self, scoring_service):
        result = SearchResult(
            guid="test",
            title="Test.1080p.WEB-DL",
            quality=QualityInfo(resolution="1080p", source="WEB-DL"),
        )
        score_no_pref = scoring_service.calculate_score(result)
        score_with_pref = scoring_service.calculate_score(result, preferred_resolution="1080p")
        assert score_with_pref > score_no_pref

    def test_calculate_score_no_bonus_for_mismatched_resolution(self, scoring_service):
        result = SearchResult(
            guid="test",
            title="Test.1080p.WEB-DL",
            quality=QualityInfo(resolution="1080p", source="WEB-DL"),
        )
        score_no_pref = scoring_service.calculate_score(result)
        score_mismatch = scoring_service.calculate_score(result, preferred_resolution="2160p")
        assert score_mismatch == score_no_pref

    def test_sort_results_with_preferred_resolution_promotes_matching_release(self, scoring_service):
        """A user preferring 1080p should see an equally-good 1080p release
        ranked above an equally-good 2160p one."""
        r_1080p = SearchResult(
            guid="1080",
            title="Movie.1080p.BluRay.x264",
            quality=QualityInfo(resolution="1080p", source="BluRay", codec="x264"),
            seeders=50,
        )
        r_2160p = SearchResult(
            guid="2160",
            title="Movie.2160p.BluRay.x264",
            quality=QualityInfo(resolution="2160p", source="BluRay", codec="x264"),
            seeders=50,
        )

        # Without preference, 2160p naturally scores higher (bigger resolution bonus).
        default_sorted = scoring_service.sort_results([r_1080p, r_2160p])
        assert default_sorted[0].guid == "2160"

        # With a 1080p preference, the +15 bonus flips the ordering.
        preferred_sorted = scoring_service.sort_results(
            [r_1080p, r_2160p], preferred_resolution="1080p"
        )
        assert preferred_sorted[0].guid == "1080"


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
