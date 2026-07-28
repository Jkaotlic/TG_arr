"""Tests for parsing functionality."""

import pytest

from bot.services.release_parser import (
    extract_season_episode,
    extract_year,
    is_season_pack,
    parse_quality,
)


class TestQualityParsing:
    """Test quality parsing from release titles."""

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
    def test_resolution_parsing(self, title, expected):
        """Test resolution extraction from titles."""
        quality = parse_quality(title)
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
    def test_source_parsing(self, title, expected):
        """Test source extraction from titles."""
        quality = parse_quality(title)
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
    def test_codec_parsing(self, title, expected):
        """Test codec extraction from titles."""
        quality = parse_quality(title)
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
    def test_hdr_parsing(self, title, expected):
        """Test HDR extraction from titles."""
        quality = parse_quality(title)
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
    def test_audio_parsing(self, title, expected):
        """Test audio extraction from titles."""
        quality = parse_quality(title)
        assert quality.audio == expected

    # Special flags tests
    @pytest.mark.parametrize("title,is_remux", [
        ("Movie.2024.1080p.BluRay.REMUX", True),
        ("Movie.2024.1080p.BluRay.Remux", True),
        ("Movie.2024.1080p.BluRay.x264", False),
    ])
    def test_remux_parsing(self, title, is_remux):
        """Test REMUX flag extraction."""
        quality = parse_quality(title)
        assert quality.is_remux == is_remux

    @pytest.mark.parametrize("title,is_repack", [
        ("Movie.2024.1080p.BluRay.REPACK", True),
        ("Movie.2024.1080p.BluRay.Repack", True),
        ("Movie.2024.1080p.BluRay.RERIP", True),
        ("Movie.2024.1080p.BluRay.x264", False),
    ])
    def test_repack_parsing(self, title, is_repack):
        """Test REPACK flag extraction."""
        quality = parse_quality(title)
        assert quality.is_repack == is_repack

    @pytest.mark.parametrize("title,is_proper", [
        ("Movie.2024.1080p.BluRay.PROPER", True),
        ("Movie.2024.1080p.BluRay.Proper", True),
        ("Movie.2024.1080p.BluRay.x264", False),
    ])
    def test_proper_parsing(self, title, is_proper):
        """Test PROPER flag extraction."""
        quality = parse_quality(title)
        assert quality.is_proper == is_proper


class TestYearParsing:
    """Test year extraction from release titles."""

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
    def test_year_extraction(self, title, expected):
        """Test year extraction from various formats."""
        year = extract_year(title)
        assert year == expected


class TestSeasonEpisodeParsing:
    """Test season/episode extraction from release titles."""

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
    def test_season_episode_extraction(self, title, expected_season, expected_episode):
        """Test season and episode extraction from various formats."""
        season, episode = extract_season_episode(title)
        assert season == expected_season
        assert episode == expected_episode


class TestSeasonPackDetection:
    """Test season pack detection."""

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
    def test_season_pack_detection(self, title, is_pack):
        """Test season pack detection from titles."""
        result = is_season_pack(title)
        assert result == is_pack



# Removed with the Scryer migration (2026-07-28): TestResultNormalization
# exercised ProwlarrClient._normalize_result, i.e. the mapping of raw
# Prowlarr JSON onto SearchResult. Scryer returns already-structured
# releases; that mapping now lives in ScryerClient._release_to_model and is
# covered by tests/test_scryer_client.py.
