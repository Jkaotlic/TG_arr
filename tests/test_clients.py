"""Tests for API clients."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from bot.clients.base import BaseAPIClient, ServiceConnectionError
from bot.clients.tmdb import TMDbClient


@pytest.mark.asyncio
async def test_emby_retries_transient_timeouts_before_returning_response(monkeypatch):
    """BUG-03: the retry decorator must see the transient connection error."""
    from bot.clients.emby import EmbyClient

    client = EmbyClient("http://emby.invalid", "test-key")
    transport = AsyncMock()
    transport.request = AsyncMock(
        side_effect=[httpx.TimeoutException("one"), httpx.TimeoutException("two"), httpx.Response(200, json={})]
    )
    monkeypatch.setattr(client, "_get_client", AsyncMock(return_value=transport))

    assert await client._request("GET", "/System/Info") == {}
    assert transport.request.await_count == 3


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


# Removed with the Scryer migration (2026-07-28): TestProwlarrClient,
# TestRadarrClient and TestSonarrClient tested clients that no longer exist.
# Their Scryer replacements are covered by tests/test_scryer_client.py
# (transport, re-login, GraphQL error handling, profile/root-folder caching).

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


@pytest.mark.asyncio
async def test_arr_clients_are_singletons_and_scryer_is_gone():
    """One instance per process — the connection pool is the point."""
    import bot.clients.registry as registry

    registry._radarr = registry._sonarr = registry._prowlarr = None

    radarr_a = await registry.get_radarr()
    radarr_b = await registry.get_radarr()
    assert radarr_a is radarr_b
    assert radarr_a.base_url == "http://localhost:7878"

    sonarr = await registry.get_sonarr()
    assert sonarr.service_name == "Sonarr"

    prowlarr = await registry.get_prowlarr()
    assert prowlarr.service_name == "Prowlarr"

    # get_scryer is a temporary bridge that raises to signal callers to convert
    with pytest.raises(RuntimeError, match="Scryer was removed"):
        await registry.get_scryer()

    await registry.close_all()
