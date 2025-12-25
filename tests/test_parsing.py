"""Tests for parsing functionality."""

import pytest

from bot.clients.prowlarr import ProwlarrClient
from bot.models import ContentType, QualityInfo


class TestQualityParsing:
    """Test quality parsing from release titles."""

    @pytest.fixture
    def prowlarr(self):
        """Create a Prowlarr client for testing parsing methods."""
        return ProwlarrClient("http://localhost:9696", "test-api-key")

    # Resolution tests
    @pytest.mark.parametrize("title,expected", [
        ("Movie.2024.2160p.UHD.BluRay.x265", "2160p"),
        ("Movie.2024.4K.WEB-DL.x265", "2160p"),
        ("Movie.2024.UHD.BluRay", "2160p"),
        ("Movie.2024.1080p.BluRay.x264", "1080p"),
        ("Movie.2024.720p.WEB-DL", "720p"),
        ("Movie.2024.480p.DVDRip", "480p"),
        ("Movie.2024.576p.DVDRip", "576p"),
        ("Movie.2024.BluRay.x264", None),  # No resolution
    ])
    def test_resolution_parsing(self, prowlarr, title, expected):
        """Test resolution extraction from titles."""
        quality = prowlarr._parse_quality(title)
        assert quality.resolution == expected

    # Source tests
    @pytest.mark.parametrize("title,expected", [
        ("Movie.2024.1080p.BluRay.x264", "BluRay"),
        ("Movie.2024.1080p.Blu-ray.x264", "BluRay"),
        ("Movie.2024.1080p.BDRip.x264", "BluRay"),
        ("Movie.2024.1080p.WEB-DL.x264", "WEB-DL"),
        ("Movie.2024.1080p.WEBDL.x264", "WEB-DL"),
        ("Movie.2024.1080p.WEBRip.x264", "WEBRip"),
        ("Movie.2024.1080p.WEB-Rip.x264", "WEBRip"),
        ("Movie.2024.1080p.HDTV.x264", "HDTV"),
        ("Movie.2024.DVDRip.x264", "DVDRip"),
        ("Movie.2024.DVD-Rip.x264", "DVDRip"),
        ("Movie.2024.CAM.x264", "CAM"),
        ("Movie.2024.HDCAM.x264", "CAM"),
        ("Movie.2024.TS.x264", "TS"),
        ("Movie.2024.HDTS.x264", "TS"),
        ("Movie.2024.TeleSync.x264", "TS"),
        ("Movie.2024.TC.x264", "TC"),
        ("Movie.2024.TeleCine.x264", "TC"),
    ])
    def test_source_parsing(self, prowlarr, title, expected):
        """Test source extraction from titles."""
        quality = prowlarr._parse_quality(title)
        assert quality.source == expected

    # Codec tests
    @pytest.mark.parametrize("title,expected", [
        ("Movie.2024.1080p.BluRay.x264", "x264"),
        ("Movie.2024.1080p.BluRay.H.264", "x264"),
        ("Movie.2024.1080p.BluRay.H264", "x264"),
        ("Movie.2024.1080p.BluRay.x265", "x265"),
        ("Movie.2024.1080p.BluRay.HEVC", "x265"),
        ("Movie.2024.1080p.BluRay.H.265", "x265"),
        ("Movie.2024.1080p.BluRay.H265", "x265"),
        ("Movie.2024.1080p.BluRay.AV1", "AV1"),
        ("Movie.2024.XviD", "XviD"),
        ("Movie.2024.DivX", "DivX"),
    ])
    def test_codec_parsing(self, prowlarr, title, expected):
        """Test codec extraction from titles."""
        quality = prowlarr._parse_quality(title)
        assert quality.codec == expected

    # HDR tests
    @pytest.mark.parametrize("title,expected", [
        ("Movie.2024.2160p.BluRay.HDR", "HDR"),
        ("Movie.2024.2160p.BluRay.HDR10", "HDR10"),
        ("Movie.2024.2160p.BluRay.HDR10+", "HDR10+"),
        ("Movie.2024.2160p.BluRay.DV", "DV"),
        ("Movie.2024.2160p.BluRay.DoVi", "DV"),
        ("Movie.2024.2160p.BluRay.Dolby.Vision", "DV"),
        ("Movie.2024.1080p.BluRay.x264", None),  # No HDR
    ])
    def test_hdr_parsing(self, prowlarr, title, expected):
        """Test HDR extraction from titles."""
        quality = prowlarr._parse_quality(title)
        assert quality.hdr == expected

    # Audio tests
    @pytest.mark.parametrize("title,expected", [
        ("Movie.2024.1080p.BluRay.Atmos", "Atmos"),
        ("Movie.2024.1080p.BluRay.TrueHD", "TrueHD"),
        ("Movie.2024.1080p.BluRay.True-HD", "TrueHD"),
        ("Movie.2024.1080p.BluRay.DTS-HD", "DTS-HD"),
        ("Movie.2024.1080p.BluRay.DTSHD", "DTS-HD"),
        ("Movie.2024.1080p.BluRay.DTS", "DTS"),
        ("Movie.2024.1080p.BluRay.DD5.1", "DD5.1"),
        ("Movie.2024.1080p.BluRay.DD.5.1", "DD5.1"),
        ("Movie.2024.1080p.BluRay.AC3", "DD5.1"),
        ("Movie.2024.1080p.BluRay.AAC", "AAC"),
    ])
    def test_audio_parsing(self, prowlarr, title, expected):
        """Test audio extraction from titles."""
        quality = prowlarr._parse_quality(title)
        assert quality.audio == expected

    # Special flags tests
    @pytest.mark.parametrize("title,is_remux", [
        ("Movie.2024.1080p.BluRay.REMUX", True),
        ("Movie.2024.1080p.BluRay.Remux", True),
        ("Movie.2024.1080p.BluRay.x264", False),
    ])
    def test_remux_parsing(self, prowlarr, title, is_remux):
        """Test REMUX flag extraction."""
        quality = prowlarr._parse_quality(title)
        assert quality.is_remux == is_remux

    @pytest.mark.parametrize("title,is_repack", [
        ("Movie.2024.1080p.BluRay.REPACK", True),
        ("Movie.2024.1080p.BluRay.Repack", True),
        ("Movie.2024.1080p.BluRay.RERIP", True),
        ("Movie.2024.1080p.BluRay.x264", False),
    ])
    def test_repack_parsing(self, prowlarr, title, is_repack):
        """Test REPACK flag extraction."""
        quality = prowlarr._parse_quality(title)
        assert quality.is_repack == is_repack

    @pytest.mark.parametrize("title,is_proper", [
        ("Movie.2024.1080p.BluRay.PROPER", True),
        ("Movie.2024.1080p.BluRay.Proper", True),
        ("Movie.2024.1080p.BluRay.x264", False),
    ])
    def test_proper_parsing(self, prowlarr, title, is_proper):
        """Test PROPER flag extraction."""
        quality = prowlarr._parse_quality(title)
        assert quality.is_proper == is_proper


class TestYearParsing:
    """Test year extraction from release titles."""

    @pytest.fixture
    def prowlarr(self):
        return ProwlarrClient("http://localhost:9696", "test-api-key")

    @pytest.mark.parametrize("title,expected", [
        ("Movie (2024) 1080p BluRay", 2024),
        ("Movie [2024] 1080p BluRay", 2024),
        ("Movie.2024.1080p.BluRay", 2024),
        ("Movie 2024 1080p BluRay", 2024),
        ("Movie.1999.1080p.BluRay", 1999),
        ("Movie.2025.1080p.BluRay", 2025),
        ("Movie.1080p.BluRay", None),  # No year
        ("Movie.1080.BluRay", None),  # 1080 is not a year
    ])
    def test_year_extraction(self, prowlarr, title, expected):
        """Test year extraction from various formats."""
        year = prowlarr._extract_year(title)
        assert year == expected


class TestSeasonEpisodeParsing:
    """Test season/episode extraction from release titles."""

    @pytest.fixture
    def prowlarr(self):
        return ProwlarrClient("http://localhost:9696", "test-api-key")

    @pytest.mark.parametrize("title,expected_season,expected_episode", [
        ("Show.S01E01.1080p.WEB-DL", 1, 1),
        ("Show.S01E10.1080p.WEB-DL", 1, 10),
        ("Show.S10E01.1080p.WEB-DL", 10, 1),
        ("Show.S1E1.1080p.WEB-DL", 1, 1),
        ("Show.S01.1080p.WEB-DL", 1, None),  # Season only
        ("Show.Season.1.1080p.WEB-DL", 1, None),
        ("Show.Season.1.Episode.5.1080p", 1, 5),
        ("Show.1x01.1080p.WEB-DL", 1, 1),
        ("Show.10x05.1080p.WEB-DL", 10, 5),
        ("Show.1080p.WEB-DL", None, None),  # No season info
    ])
    def test_season_episode_extraction(self, prowlarr, title, expected_season, expected_episode):
        """Test season and episode extraction from various formats."""
        season, episode = prowlarr._extract_season_episode(title)
        assert season == expected_season
        assert episode == expected_episode


class TestSeasonPackDetection:
    """Test season pack detection."""

    @pytest.fixture
    def prowlarr(self):
        return ProwlarrClient("http://localhost:9696", "test-api-key")

    @pytest.mark.parametrize("title,is_pack", [
        ("Show.S01.Complete.1080p.WEB-DL", True),
        ("Show.S01.1080p.WEB-DL", True),
        ("Show.Season.1.Complete.1080p", True),
        ("Show.Complete.Season.1.1080p", True),
        ("Show.Season.Pack.S01.1080p", True),
        ("Show.Full.Season.1.1080p", True),
        ("Show.S01E01.1080p.WEB-DL", False),  # Single episode
        ("Show.S01E01-E10.1080p.WEB-DL", False),  # Episode range, not detected as pack
        ("Movie.2024.1080p.BluRay", False),  # Not a series
    ])
    def test_season_pack_detection(self, prowlarr, title, is_pack):
        """Test season pack detection from titles."""
        result = prowlarr._is_season_pack(title)
        assert result == is_pack


class TestResultNormalization:
    """Test result normalization from Prowlarr API responses."""

    @pytest.fixture
    def prowlarr(self):
        return ProwlarrClient("http://localhost:9696", "test-api-key")

    def test_normalize_basic_result(self, prowlarr):
        """Test normalization of a basic result."""
        raw = {
            "guid": "test-guid-123",
            "title": "Test.Movie.2024.1080p.BluRay.x264-GROUP",
            "size": 5368709120,  # 5 GB
            "seeders": 100,
            "leechers": 10,
            "indexer": "TestIndexer",
            "indexerId": 1,
            "protocol": "torrent",
            "downloadUrl": "http://example.com/download",
            "categories": [{"id": 2000, "name": "Movies"}],
        }

        result = prowlarr._normalize_result(raw)

        assert result is not None
        assert result.guid == "test-guid-123"
        assert result.title == "Test.Movie.2024.1080p.BluRay.x264-GROUP"
        assert result.size == 5368709120
        assert result.seeders == 100
        assert result.leechers == 10
        assert result.indexer == "TestIndexer"
        assert result.indexer_id == 1
        assert result.protocol == "torrent"
        assert result.detected_type == ContentType.MOVIE
        assert result.detected_year == 2024
        assert result.quality.resolution == "1080p"
        assert result.quality.source == "BluRay"
        assert result.quality.codec == "x264"

    def test_normalize_series_result(self, prowlarr):
        """Test normalization of a series result."""
        raw = {
            "guid": "test-guid-456",
            "title": "Show.S01E01.1080p.WEB-DL.x265-GROUP",
            "size": 1073741824,  # 1 GB
            "seeders": 50,
            "indexer": "TestIndexer",
            "indexerId": 2,
            "protocol": "torrent",
            "categories": [{"id": 5000, "name": "TV"}],
        }

        result = prowlarr._normalize_result(raw)

        assert result is not None
        assert result.detected_type == ContentType.SERIES
        assert result.detected_season == 1
        assert result.detected_episode == 1
        assert result.is_season_pack is False

    def test_normalize_season_pack_result(self, prowlarr):
        """Test normalization of a season pack result."""
        raw = {
            "guid": "test-guid-789",
            "title": "Show.S02.Complete.1080p.BluRay.x264-GROUP",
            "size": 21474836480,  # 20 GB
            "seeders": 75,
            "indexer": "TestIndexer",
            "indexerId": 3,
            "protocol": "torrent",
            "categories": [{"id": 5000, "name": "TV"}],
        }

        result = prowlarr._normalize_result(raw)

        assert result is not None
        assert result.detected_type == ContentType.SERIES
        assert result.detected_season == 2
        assert result.detected_episode is None
        assert result.is_season_pack is True

    def test_normalize_missing_guid_returns_none(self, prowlarr):
        """Test that missing GUID returns None."""
        raw = {
            "title": "Test.Movie.2024.1080p",
            "size": 5000000000,
        }

        result = prowlarr._normalize_result(raw)
        assert result is None

    def test_normalize_missing_title_returns_none(self, prowlarr):
        """Test that missing title returns None."""
        raw = {
            "guid": "test-guid",
            "size": 5000000000,
        }

        result = prowlarr._normalize_result(raw)
        assert result is None

    def test_normalize_alternative_field_names(self, prowlarr):
        """Test normalization with alternative field names."""
        raw = {
            "guid": "test-guid",
            "title": "Test.Movie",
            "indexerName": "AltIndexer",  # Alternative field name
            "peers": 20,  # Alternative for leechers
        }

        result = prowlarr._normalize_result(raw)

        assert result is not None
        assert result.indexer == "AltIndexer"
        assert result.leechers == 20

    def test_normalize_usenet_result(self, prowlarr):
        """Test normalization of usenet result."""
        raw = {
            "guid": "test-guid",
            "title": "Test.Movie.2024.1080p.BluRay",
            "size": 5000000000,
            "protocol": "usenet",
            "downloadUrl": "http://example.com/download.nzb",
        }

        result = prowlarr._normalize_result(raw)

        assert result is not None
        assert result.protocol == "usenet"
        assert result.seeders is None  # Usenet doesn't have seeders

    def test_normalize_category_as_int(self, prowlarr):
        """Test normalization with categories as integers."""
        raw = {
            "guid": "test-guid",
            "title": "Test.Movie",
            "categories": [2000, 2010],  # Integer categories
        }

        result = prowlarr._normalize_result(raw)

        assert result is not None
        assert 2000 in result.categories
        assert 2010 in result.categories
        assert result.detected_type == ContentType.MOVIE
