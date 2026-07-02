"""Pytest configuration and fixtures."""

import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

# Add bot package to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# TEST-12: shared test helpers, previously copy-pasted across multiple test
# modules. Kept as plain importable functions (not fixtures) since callers
# need to invoke them with different arguments inline, not just receive a
# fixed value.
# ---------------------------------------------------------------------------


def mock_http_with_cookie(status_code: int, text: str, cookie_name: str | None = "SID"):
    """Build a mock httpx AsyncClient whose .post() returns a response with
    the given status/body, and whose .cookies.jar contains a session cookie
    named ``cookie_name`` (or is empty if ``cookie_name`` is falsy).

    Shared by test_qbittorrent.py and test_r4_C2-qbit.py (qBittorrent login
    contract tests: 200/"Ok.", 204 no-content, "Fails." body, missing cookie).
    """
    mock_http = AsyncMock()
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_response.text = text
    mock_http.post.return_value = mock_response

    cookies_jar = []
    if cookie_name:
        cookie_obj = MagicMock()
        cookie_obj.name = cookie_name
        cookies_jar.append(cookie_obj)
    mock_http.cookies.jar = cookies_jar
    return mock_http


def callback_with_status():
    """Build a MagicMock CallbackQuery whose message.answer(...) returns a
    status_msg with an AsyncMock edit_text — the common shape used by
    trending "add to Radarr/Sonarr" flows that post a status message and
    then edit it in place.

    Shared by test_r4_C5-handler-perf.py and test_audit_r4_fixes.py.
    """
    status_msg = MagicMock()
    status_msg.edit_text = AsyncMock()
    cb = MagicMock()
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.answer = AsyncMock(return_value=status_msg)
    return cb, status_msg


def build_add_service(radarr=None, sonarr=None, lidarr=None, qbt=None):
    """Construct an AddService with AsyncMock clients for any arg left as
    None (except lidarr/qbt, which default to None — not every test wants
    those wired up).

    Shared by test_add_service.py and test_r4_C4-services.py.
    """
    from bot.services.add_service import AddService

    return AddService(
        prowlarr=AsyncMock(),
        radarr=radarr or AsyncMock(),
        sonarr=sonarr or AsyncMock(),
        qbittorrent=qbt,
        lidarr=lidarr,
    )


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
