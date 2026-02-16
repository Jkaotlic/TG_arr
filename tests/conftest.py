"""Pytest configuration and fixtures."""

import os
import sys

import pytest

# Add bot package to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def mock_env(monkeypatch):
    """Set up mock environment variables for testing."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test_token_123")
    monkeypatch.setenv("ALLOWED_TG_IDS", "123456789,987654321")
    monkeypatch.setenv("ADMIN_TG_IDS", "123456789")
    monkeypatch.setenv("PROWLARR_URL", "http://localhost:9696")
    monkeypatch.setenv("PROWLARR_API_KEY", "test_prowlarr_key")
    monkeypatch.setenv("RADARR_URL", "http://localhost:7878")
    monkeypatch.setenv("RADARR_API_KEY", "test_radarr_key")
    monkeypatch.setenv("SONARR_URL", "http://localhost:8989")
    monkeypatch.setenv("SONARR_API_KEY", "test_sonarr_key")
    monkeypatch.setenv("DATABASE_PATH", ":memory:")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")


@pytest.fixture
def sample_prowlarr_response():
    """Sample Prowlarr search response."""
    return [
        {
            "guid": "test-guid-1",
            "title": "Test.Movie.2024.1080p.BluRay.x264-GROUP",
            "size": 5368709120,
            "seeders": 150,
            "leechers": 25,
            "indexer": "TestIndexer",
            "indexerId": 1,
            "protocol": "torrent",
            "downloadUrl": "http://example.com/download/1",
            "infoUrl": "http://example.com/info/1",
            "publishDate": "2024-01-15T12:00:00Z",
            "categories": [{"id": 2000, "name": "Movies"}],
        },
        {
            "guid": "test-guid-2",
            "title": "Test.Movie.2024.2160p.UHD.BluRay.x265.HDR-GROUP",
            "size": 21474836480,
            "seeders": 75,
            "leechers": 10,
            "indexer": "TestIndexer",
            "indexerId": 1,
            "protocol": "torrent",
            "downloadUrl": "http://example.com/download/2",
            "categories": [{"id": 2000, "name": "Movies"}],
        },
        {
            "guid": "test-guid-3",
            "title": "Test.Movie.2024.CAM.x264-BADGROUP",
            "size": 1073741824,
            "seeders": 500,
            "leechers": 100,
            "indexer": "BadIndexer",
            "indexerId": 2,
            "protocol": "torrent",
            "downloadUrl": "http://example.com/download/3",
            "categories": [{"id": 2000, "name": "Movies"}],
        },
    ]


@pytest.fixture
def sample_radarr_movie():
    """Sample Radarr movie lookup response."""
    return {
        "tmdbId": 123456,
        "imdbId": "tt1234567",
        "title": "Test Movie",
        "originalTitle": "Test Movie Original",
        "year": 2024,
        "overview": "This is a test movie about testing things.",
        "runtime": 120,
        "studio": "Test Studios",
        "genres": ["Action", "Adventure", "Sci-Fi"],
        "images": [
            {"coverType": "poster", "remoteUrl": "http://example.com/poster.jpg"},
            {"coverType": "fanart", "remoteUrl": "http://example.com/fanart.jpg"},
        ],
        "ratings": {
            "imdb": {"value": 7.5},
            "tmdb": {"value": 7.2},
        },
    }


@pytest.fixture
def sample_sonarr_series():
    """Sample Sonarr series lookup response."""
    return {
        "tvdbId": 654321,
        "imdbId": "tt7654321",
        "title": "Test Series",
        "originalTitle": "Test Series Original",
        "year": 2020,
        "overview": "This is a test series about testing things.",
        "runtime": 45,
        "network": "Test Network",
        "status": "continuing",
        "genres": ["Drama", "Mystery"],
        "images": [
            {"coverType": "poster", "remoteUrl": "http://example.com/poster.jpg"},
            {"coverType": "fanart", "remoteUrl": "http://example.com/fanart.jpg"},
        ],
        "seasons": [
            {"seasonNumber": 0, "monitored": False},
            {"seasonNumber": 1, "monitored": True, "statistics": {"totalEpisodeCount": 10}},
            {"seasonNumber": 2, "monitored": True, "statistics": {"totalEpisodeCount": 10}},
            {"seasonNumber": 3, "monitored": True, "statistics": {"totalEpisodeCount": 8}},
        ],
    }


@pytest.fixture
def sample_quality_profiles():
    """Sample quality profiles."""
    return [
        {"id": 1, "name": "Any"},
        {"id": 2, "name": "SD"},
        {"id": 3, "name": "HD-720p"},
        {"id": 4, "name": "HD-1080p"},
        {"id": 5, "name": "Ultra-HD"},
    ]


@pytest.fixture
def sample_root_folders():
    """Sample root folders."""
    return [
        {"id": 1, "path": "/movies", "freeSpace": 1099511627776},
        {"id": 2, "path": "/movies-4k", "freeSpace": 549755813888},
    ]
