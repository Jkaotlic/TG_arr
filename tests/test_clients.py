"""Tests for API clients."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from bot.clients.base import BaseAPIClient, ServiceConnectionError
from bot.clients.prowlarr import ProwlarrClient
from bot.clients.radarr import RadarrClient
from bot.clients.sonarr import SonarrClient
from bot.clients.tmdb import TMDbClient
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

    def test_delete_method_removed(self, client):
        """DEAD-11: BaseAPIClient.delete had zero callers — removed."""
        assert not hasattr(client, "delete")

    async def test_retry_logs_attempt_then_recovers(self, client, monkeypatch):
        """OBS-14: a timeout that recovers on retry logs a WARNING per
        retried attempt (request_retry_attempt) but must NOT log the
        terminal request_retries_exhausted (all attempts did not fail)."""
        import structlog.testing

        # Avoid real sleeping between tenacity retries in the test.
        monkeypatch.setattr("asyncio.sleep", AsyncMock())

        calls = {"n": 0}

        async def fake_request(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.TimeoutException("slow")
            resp = AsyncMock()
            resp.status_code = 200
            resp.json = lambda: {"ok": True}
            return resp

        mock_httpx_client = AsyncMock()
        mock_httpx_client.is_closed = False
        mock_httpx_client.request = AsyncMock(side_effect=fake_request)
        client._client = mock_httpx_client

        with structlog.testing.capture_logs() as logs:
            result = await client.get("/x")

        assert result == {"ok": True}
        retry_events = [e for e in logs if e.get("event") == "request_retry_attempt"]
        assert len(retry_events) == 1
        assert retry_events[0]["attempt"] == 1
        assert retry_events[0]["service"] == "TestService"
        exhausted_events = [e for e in logs if e.get("event") == "request_retries_exhausted"]
        assert exhausted_events == []

    async def test_retry_exhausted_logs_warning(self, client, monkeypatch):
        """OBS-14: when ALL attempts fail, a final WARNING
        request_retries_exhausted must be logged (in addition to the
        per-attempt request_retry_attempt logs)."""
        import structlog.testing

        monkeypatch.setattr("asyncio.sleep", AsyncMock())

        mock_httpx_client = AsyncMock()
        mock_httpx_client.is_closed = False
        mock_httpx_client.request = AsyncMock(side_effect=httpx.TimeoutException("slow"))
        client._client = mock_httpx_client

        with structlog.testing.capture_logs() as logs:
            with pytest.raises(ServiceConnectionError):
                await client.get("/x")

        retry_events = [e for e in logs if e.get("event") == "request_retry_attempt"]
        # stop_after_attempt(3) -> 2 retries logged before the 3rd (final) failure
        assert len(retry_events) == 2
        exhausted_events = [e for e in logs if e.get("event") == "request_retries_exhausted"]
        assert len(exhausted_events) == 1
        assert exhausted_events[0]["service"] == "TestService"


class TestProwlarrClient:
    """Test Prowlarr client functionality.

    TEST-12: title/quality parsing (_parse_quality, _extract_year,
    _extract_season_episode, _is_season_pack) and _normalize_result are
    covered far more thoroughly (parametrized, many more cases) by
    test_parsing.py — see TestQualityParsing/TestYearParsing/
    TestSeasonEpisodeParsing/TestSeasonPackDetection/TestResultNormalization
    there. Only the retry/timeout behaviour unique to this module (not
    parsing) is kept here.
    """

    @pytest.fixture
    def client(self):
        """Create a Prowlarr client for testing."""
        return ProwlarrClient("http://localhost:9696", "test-api-key")

    @pytest.mark.asyncio
    async def test_search_retries_on_timeout_then_succeeds(self, client):
        """First attempt times out (RuTracker лагает), retry returns results."""
        good_item = {"guid": "g1", "title": "Movie 2024 1080p", "size": 1, "indexer": "1337x"}

        async def fake_do_search(params, timeout, log, attempt):
            if attempt == 1:
                raise httpx.TimeoutException("simulated indexer hang")
            return [client._normalize_result(good_item)]

        with patch.object(client, "_do_search", new=AsyncMock(side_effect=fake_do_search)), \
             patch("bot.clients.prowlarr.asyncio.sleep", new=AsyncMock()):  # ускоряем тест
            results = await client.search("Movie 2024", ContentType.MOVIE)

        assert len(results) == 1
        assert results[0].title == "Movie 2024 1080p"

    @pytest.mark.asyncio
    async def test_search_raises_when_all_attempts_timeout(self, client):
        """All retries fail → ServiceConnectionError surfaces."""
        with patch.object(
            client, "_do_search",
            new=AsyncMock(side_effect=httpx.TimeoutException("hang")),
        ), patch("bot.clients.prowlarr.asyncio.sleep", new=AsyncMock()):
            with pytest.raises(ServiceConnectionError) as ex:
                await client.search("X", ContentType.MOVIE)

        assert "попыток" in str(ex.value)

    @pytest.mark.asyncio
    async def test_search_no_retry_on_connect_error(self, client):
        """ConnectError must not retry — fail fast (Prowlarr is down, not flaky)."""
        do_search = AsyncMock(side_effect=httpx.ConnectError("boom"))
        with patch.object(client, "_do_search", new=do_search):
            with pytest.raises(ServiceConnectionError):
                await client.search("X", ContentType.MOVIE)

        assert do_search.call_count == 1


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

    @pytest.mark.asyncio
    async def test_push_release_unwraps_list_response(self, client):
        """BUG-01: POST /release/push returns List<ReleaseResource> — the
        client must unwrap it to the first dict, not collapse it to {}."""
        with patch.object(
            client,
            "_post_no_retry",
            new=AsyncMock(return_value=[{"approved": True, "rejections": []}]),
        ):
            result = await client.push_release(
                title="Test.Release.1080p",
                download_url="http://example.com/file.torrent",
            )

        assert result == {"approved": True, "rejections": []}

    @pytest.mark.asyncio
    async def test_push_release_empty_list_returns_empty_dict(self, client):
        """An empty list response must not raise — falls back to {}."""
        with patch.object(client, "_post_no_retry", new=AsyncMock(return_value=[])):
            result = await client.push_release(
                title="Test.Release.1080p",
                download_url="http://example.com/file.torrent",
            )

        assert result == {}

    @pytest.mark.asyncio
    async def test_get_quality_profiles_is_cached(self, client):
        """PERF-07: a second call within the TTL must NOT hit the API again."""
        get_mock = AsyncMock(return_value=[{"id": 1, "name": "HD-1080p"}])
        with patch.object(client, "get", new=get_mock):
            first = await client.get_quality_profiles()
            second = await client.get_quality_profiles()

        assert first == second
        assert len(first) == 1
        get_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_root_folders_is_cached(self, client):
        """PERF-07: a second call within the TTL must NOT hit the API again."""
        get_mock = AsyncMock(return_value=[{"id": 1, "path": "/movies", "freeSpace": 100}])
        with patch.object(client, "get", new=get_mock):
            first = await client.get_root_folders()
            second = await client.get_root_folders()

        assert first == second
        get_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_quality_profiles_refetches_after_ttl_expires(self, client):
        """PERF-07: once the TTL elapses, the next call must hit the API again."""
        get_mock = AsyncMock(return_value=[{"id": 1, "name": "HD-1080p"}])
        with patch.object(client, "get", new=get_mock):
            await client.get_quality_profiles()
            # Simulate TTL expiry by rewinding the cached timestamp.
            key = "quality_profiles"
            ts, value = client._ttl_cache[key]
            client._ttl_cache[key] = (ts - client._PROFILE_CACHE_TTL - 1, value)
            await client.get_quality_profiles()

        assert get_mock.await_count == 2

    @pytest.mark.asyncio
    async def test_quality_profiles_and_root_folders_cached_independently(self, client):
        """Different cache keys must not collide with each other."""
        profiles_mock = AsyncMock(return_value=[{"id": 1, "name": "HD-1080p"}])
        folders_mock = AsyncMock(return_value=[{"id": 1, "path": "/movies"}])

        async def fake_get(endpoint, **kwargs):
            if "qualityprofile" in endpoint:
                return await profiles_mock()
            return await folders_mock()

        with patch.object(client, "get", new=AsyncMock(side_effect=fake_get)):
            await client.get_quality_profiles()
            await client.get_root_folders()
            await client.get_quality_profiles()
            await client.get_root_folders()

        profiles_mock.assert_awaited_once()
        folders_mock.assert_awaited_once()


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

    @pytest.mark.asyncio
    async def test_push_release_unwraps_list_response(self, client):
        """BUG-01: POST /release/push returns List<ReleaseResource> — the
        client must unwrap it to the first dict, not collapse it to {}."""
        with patch.object(
            client,
            "_post_no_retry",
            new=AsyncMock(return_value=[{"approved": True, "rejections": []}]),
        ):
            result = await client.push_release(
                title="Test.Release.1080p",
                download_url="http://example.com/file.torrent",
            )

        assert result == {"approved": True, "rejections": []}

    @pytest.mark.asyncio
    async def test_push_release_empty_list_returns_empty_dict(self, client):
        """An empty list response must not raise — falls back to {}."""
        with patch.object(client, "_post_no_retry", new=AsyncMock(return_value=[])):
            result = await client.push_release(
                title="Test.Release.1080p",
                download_url="http://example.com/file.torrent",
            )

        assert result == {}


class TestTMDbClient:
    """BUG-13: TMDb has two incompatible credential formats — a v3 key
    (short opaque string, must go as ?api_key=) and a v4 read-access token
    (a JWT starting with "eyJ", must go as Authorization: Bearer)."""

    def test_v3_key_uses_query_param_not_bearer(self):
        client = TMDbClient(api_key="abc123v3key")
        headers = client._get_headers()
        assert "Authorization" not in headers

    def test_v4_token_uses_bearer_header(self):
        v4_token = "eyJhbGciOiJIUzI1NiJ9.fake.token"
        client = TMDbClient(api_key=v4_token)
        headers = client._get_headers()
        assert headers["Authorization"] == f"Bearer {v4_token}"

    @pytest.mark.asyncio
    async def test_v3_key_injects_api_key_param_on_get(self):
        client = TMDbClient(api_key="abc123v3key")
        with patch.object(
            client, "_safe_request", new=AsyncMock(return_value={"results": []}),
        ) as safe_request:
            await client.get("/trending/movie/week", params={"page": 1})

        call_kwargs = safe_request.await_args.kwargs
        assert call_kwargs["params"]["api_key"] == "abc123v3key"
        assert call_kwargs["params"]["page"] == 1

    @pytest.mark.asyncio
    async def test_v4_token_does_not_inject_api_key_param(self):
        v4_token = "eyJhbGciOiJIUzI1NiJ9.fake.token"
        client = TMDbClient(api_key=v4_token)
        with patch.object(
            client, "_safe_request", new=AsyncMock(return_value={"results": []}),
        ) as safe_request:
            await client.get("/trending/movie/week", params={"page": 1})

        call_kwargs = safe_request.await_args.kwargs
        assert "api_key" not in (call_kwargs["params"] or {})
