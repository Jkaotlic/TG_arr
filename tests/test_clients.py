"""Tests for API clients."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import httpx

from bot.clients.base import BaseAPIClient, APIError, ConnectionError, AuthenticationError
from bot.clients.prowlarr import ProwlarrClient
from bot.clients.radarr import RadarrClient
from bot.clients.sonarr import SonarrClient
from bot.models import ContentType


class TestBaseAPIClient:
    """Test base API client functionality."""

    @pytest.fixture
    def client(self):
        """Create a base API client for testing."""
        return BaseAPIClient("http://localhost:8080", "test-api-key", "TestService")

    def test_init(self, client):
        """Test client initialization."""
        assert client.base_url == "http://localhost:8080"
        assert client.api_key == "test-api-key"
        assert client.service_name == "TestService"

    def test_strip_trailing_slash(self):
        """Test that trailing slashes are stripped from URL."""
        client = BaseAPIClient("http://localhost:8080/", "key", "Test")
        assert client.base_url == "http://localhost:8080"

    def test_get_headers(self, client):
        """Test default headers."""
        headers = client._get_headers()
        assert headers["X-Api-Key"] == "test-api-key"
        assert headers["Content-Type"] == "application/json"
        assert headers["Accept"] == "application/json"


class TestProwlarrClient:
    """Test Prowlarr client functionality."""

    @pytest.fixture
    def client(self):
        """Create a Prowlarr client for testing."""
        return ProwlarrClient("http://localhost:9696", "test-api-key")

    def test_parse_quality_full(self, client):
        """Test parsing quality from a complete title."""
        quality = client._parse_quality(
            "Movie.2024.2160p.UHD.BluRay.REMUX.HDR10.DV.TrueHD.Atmos.x265-GROUP"
        )

        assert quality.resolution == "2160p"
        assert quality.source == "BluRay"
        assert quality.codec == "x265"
        assert quality.is_remux is True
        assert "DV" in quality.hdr
        assert quality.audio == "Atmos"

    def test_parse_quality_minimal(self, client):
        """Test parsing quality from a minimal title."""
        quality = client._parse_quality("Movie.2024")

        assert quality.resolution is None
        assert quality.source is None
        assert quality.codec is None

    def test_extract_year_parentheses(self, client):
        """Test year extraction with parentheses."""
        assert client._extract_year("Movie (2024)") == 2024

    def test_extract_year_brackets(self, client):
        """Test year extraction with brackets."""
        assert client._extract_year("Movie [2024]") == 2024

    def test_extract_year_dots(self, client):
        """Test year extraction with dots."""
        assert client._extract_year("Movie.2024.1080p") == 2024

    def test_extract_year_none(self, client):
        """Test year extraction when no year present."""
        assert client._extract_year("Movie.1080p") is None

    def test_extract_season_episode_standard(self, client):
        """Test S01E01 format extraction."""
        season, episode = client._extract_season_episode("Show.S01E05.1080p")
        assert season == 1
        assert episode == 5

    def test_extract_season_only(self, client):
        """Test season-only extraction."""
        season, episode = client._extract_season_episode("Show.S02.Complete")
        assert season == 2
        assert episode is None

    def test_extract_season_episode_1x01(self, client):
        """Test 1x01 format extraction."""
        season, episode = client._extract_season_episode("Show.3x10.720p")
        assert season == 3
        assert episode == 10

    def test_is_season_pack_true(self, client):
        """Test season pack detection - true cases."""
        assert client._is_season_pack("Show.S01.Complete.1080p") is True
        assert client._is_season_pack("Show.S02.1080p.WEB-DL") is True
        assert client._is_season_pack("Show.Season.1.Complete") is True

    def test_is_season_pack_false(self, client):
        """Test season pack detection - false cases."""
        assert client._is_season_pack("Show.S01E01.1080p") is False
        assert client._is_season_pack("Movie.2024.1080p") is False

    def test_normalize_result_movie(self, client):
        """Test normalizing a movie result."""
        raw = {
            "guid": "test-guid",
            "title": "Test.Movie.2024.1080p.BluRay.x264-GROUP",
            "size": 5000000000,
            "seeders": 100,
            "leechers": 20,
            "indexer": "TestIndexer",
            "indexerId": 1,
            "protocol": "torrent",
            "categories": [{"id": 2000, "name": "Movies"}],
        }

        result = client._normalize_result(raw)

        assert result is not None
        assert result.guid == "test-guid"
        assert result.detected_type == ContentType.MOVIE
        assert result.detected_year == 2024
        assert result.quality.resolution == "1080p"

    def test_normalize_result_series(self, client):
        """Test normalizing a series result."""
        raw = {
            "guid": "test-guid",
            "title": "Show.S03E05.1080p.WEB-DL.x265-GROUP",
            "size": 1500000000,
            "seeders": 50,
            "indexer": "TestIndexer",
            "indexerId": 1,
            "protocol": "torrent",
            "categories": [{"id": 5000, "name": "TV"}],
        }

        result = client._normalize_result(raw)

        assert result is not None
        assert result.detected_type == ContentType.SERIES
        assert result.detected_season == 3
        assert result.detected_episode == 5
        assert result.is_season_pack is False

    def test_normalize_result_invalid(self, client):
        """Test normalizing an invalid result."""
        # Missing guid
        assert client._normalize_result({"title": "Test"}) is None

        # Missing title
        assert client._normalize_result({"guid": "test"}) is None


class TestRadarrClient:
    """Test Radarr client functionality."""

    @pytest.fixture
    def client(self):
        """Create a Radarr client for testing."""
        return RadarrClient("http://localhost:7878", "test-api-key")

    def test_parse_movie(self, client, sample_radarr_movie):
        """Test parsing a movie response."""
        movie = client._parse_movie(sample_radarr_movie)

        assert movie is not None
        assert movie.tmdb_id == 123456
        assert movie.imdb_id == "tt1234567"
        assert movie.title == "Test Movie"
        assert movie.year == 2024
        assert movie.runtime == 120
        assert "Action" in movie.genres
        assert movie.poster_url == "http://example.com/poster.jpg"

    def test_parse_movie_minimal(self, client):
        """Test parsing a minimal movie response."""
        movie = client._parse_movie({
            "tmdbId": 123,
            "title": "Minimal Movie",
            "year": 2024,
        })

        assert movie is not None
        assert movie.tmdb_id == 123
        assert movie.title == "Minimal Movie"

    def test_parse_movie_missing_tmdb(self, client):
        """Test parsing fails without TMDB ID."""
        movie = client._parse_movie({
            "title": "No TMDB",
            "year": 2024,
        })
        assert movie is None


class TestSonarrClient:
    """Test Sonarr client functionality."""

    @pytest.fixture
    def client(self):
        """Create a Sonarr client for testing."""
        return SonarrClient("http://localhost:8989", "test-api-key")

    def test_parse_series(self, client, sample_sonarr_series):
        """Test parsing a series response."""
        series = client._parse_series(sample_sonarr_series)

        assert series is not None
        assert series.tvdb_id == 654321
        assert series.imdb_id == "tt7654321"
        assert series.title == "Test Series"
        assert series.year == 2020
        assert series.network == "Test Network"
        assert series.status == "continuing"
        assert series.season_count == 3  # Excludes season 0

    def test_parse_series_minimal(self, client):
        """Test parsing a minimal series response."""
        series = client._parse_series({
            "tvdbId": 123,
            "title": "Minimal Series",
        })

        assert series is not None
        assert series.tvdb_id == 123
        assert series.title == "Minimal Series"

    def test_parse_series_missing_tvdb(self, client):
        """Test parsing fails without TVDB ID."""
        series = client._parse_series({
            "title": "No TVDB",
        })
        assert series is None

    def test_should_monitor_season_all(self, client):
        """Test season monitoring - all."""
        assert client._should_monitor_season(1, "all", 5) is True
        assert client._should_monitor_season(3, "all", 5) is True

    def test_should_monitor_season_none(self, client):
        """Test season monitoring - none."""
        assert client._should_monitor_season(1, "none", 5) is False
        assert client._should_monitor_season(3, "none", 5) is False

    def test_should_monitor_season_first(self, client):
        """Test season monitoring - first season."""
        assert client._should_monitor_season(1, "firstSeason", 5) is True
        assert client._should_monitor_season(2, "firstSeason", 5) is False

    def test_should_monitor_season_latest(self, client):
        """Test season monitoring - latest season."""
        assert client._should_monitor_season(5, "latestSeason", 5) is True
        assert client._should_monitor_season(4, "latestSeason", 5) is False

    def test_should_monitor_season_pilot(self, client):
        """Test season monitoring - pilot."""
        assert client._should_monitor_season(1, "pilot", 5) is True
        assert client._should_monitor_season(2, "pilot", 5) is False
