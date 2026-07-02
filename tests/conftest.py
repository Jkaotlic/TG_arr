"""Pytest configuration and fixtures."""

import os
import sys

import pytest

# Add bot package to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def _default_env(monkeypatch):
    """
    Populate required env vars for every test so that any lazy get_settings()
    call during a fixture/test doesn't fail with ValidationError.
    BUG-23: also clear the lru_cache so each test sees its own env.
    """
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test_token_123:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
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

    from bot.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


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
