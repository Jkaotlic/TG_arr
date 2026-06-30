"""R4 cluster C2-qbit: perf + observability for qBittorrent client/handlers.

Covers:
- PERF-05: get_status() parallelizes its 4 API calls via asyncio.gather while
  producing the identical QBittorrentStatus.
- PERF-01: pause/resume/delete callbacks fetch the torrent list at most once and
  use a targeted single-torrent fetch for the post-action redraw.
- OBS-01: login() logs a warning/error on the failure paths before raising.
- OBS-04: pause()/resume() log when falling back to the legacy endpoint.
- OBS-05: _request logs when re-authenticating mid-request after a 403.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.clients.qbittorrent import (
    QBittorrentAuthError,
    QBittorrentClient,
    QBittorrentError,
)
from bot.models import QBittorrentStatus, TorrentInfo, TorrentState


@pytest.fixture
def client():
    return QBittorrentClient("http://localhost:8080", "admin", "password123")


# ---------------------------------------------------------------------------
# PERF-05: get_status parallelization
# ---------------------------------------------------------------------------


class TestGetStatusParallel:
    @pytest.mark.asyncio
    async def test_get_status_aggregates_identically(self, client):
        """get_status produces the same QBittorrentStatus from the 4 endpoints."""
        torrents = [
            TorrentInfo(hash="a" * 40, name="dl", progress=0.5,
                        state=TorrentState.DOWNLOADING),
            TorrentInfo(hash="b" * 40, name="up", progress=1.0,
                        state=TorrentState.SEEDING),
            TorrentInfo(hash="c" * 40, name="up2", progress=1.0,
                        state=TorrentState.SEEDING),
            TorrentInfo(hash="d" * 40, name="paused", progress=0.3,
                        state=TorrentState.PAUSED),
        ]

        async def fake_request(method, endpoint, data=None, params=None):
            if endpoint == "/api/v2/app/version":
                return "4.6.0"
            if endpoint == "/api/v2/transfer/info":
                return {
                    "connection_status": "connected",
                    "dl_info_speed": 5000000,
                    "up_info_speed": 1000000,
                    "dl_rate_limit": 10000000,
                    "up_rate_limit": 0,
                }
            if endpoint == "/api/v2/sync/maindata":
                return {"server_state": {"free_space_on_disk": 999, "dht_nodes": 321}}
            raise AssertionError(f"unexpected endpoint {endpoint}")

        with patch.object(client, "_request", side_effect=fake_request), \
                patch.object(client, "get_torrents",
                             new=AsyncMock(return_value=torrents)):
            status = await client.get_status()

        assert isinstance(status, QBittorrentStatus)
        assert status.version == "4.6.0"
        assert status.connection_status == "connected"
        assert status.download_speed == 5000000
        assert status.upload_speed == 1000000
        assert status.download_limit == 10000000
        assert status.upload_limit == 0
        assert status.free_space == 999
        assert status.dht_nodes == 321
        assert status.active_downloads == 1
        assert status.active_uploads == 2
        assert status.paused_torrents == 1
        assert status.total_torrents == 4

    @pytest.mark.asyncio
    async def test_get_status_runs_calls_concurrently(self, client):
        """The independent fetches must run concurrently, not sequentially."""
        in_flight = 0
        max_in_flight = 0

        async def fake_request(method, endpoint, data=None, params=None):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            try:
                await asyncio.sleep(0.02)
            finally:
                in_flight -= 1
            if endpoint == "/api/v2/app/version":
                return "4.6.0"
            if endpoint == "/api/v2/transfer/info":
                return {}
            if endpoint == "/api/v2/sync/maindata":
                return {"server_state": {}}
            raise AssertionError(f"unexpected endpoint {endpoint}")

        async def fake_get_torrents(*a, **k):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            try:
                await asyncio.sleep(0.02)
            finally:
                in_flight -= 1
            return []

        with patch.object(client, "_request", side_effect=fake_request), \
                patch.object(client, "get_torrents", side_effect=fake_get_torrents):
            await client.get_status()

        # 4 independent fetches should overlap -> more than one in flight at once.
        assert max_in_flight >= 2


# ---------------------------------------------------------------------------
# OBS-01: login failure logging
# ---------------------------------------------------------------------------


class TestLoginFailureLogging:
    def _mock_http(self, status_code, text, cookie_name=None):
        mock_http = AsyncMock()
        resp = MagicMock()
        resp.status_code = status_code
        resp.text = text
        mock_http.post.return_value = resp
        jar = []
        if cookie_name:
            c = MagicMock()
            c.name = cookie_name
            jar.append(c)
        mock_http.cookies.jar = jar
        return mock_http

    @pytest.mark.asyncio
    async def test_login_auth_failure_logs(self, client):
        mock_http = self._mock_http(403, "Fails.")
        with patch.object(client, "_get_client", new=AsyncMock(return_value=mock_http)), \
                patch("bot.clients.qbittorrent.logger") as mock_logger:
            bound = MagicMock()
            mock_logger.bind.return_value = bound
            with pytest.raises(QBittorrentAuthError):
                await client.login()
            assert bound.warning.called or bound.error.called

    @pytest.mark.asyncio
    async def test_login_no_cookie_logs(self, client):
        mock_http = self._mock_http(204, "", cookie_name=None)
        with patch.object(client, "_get_client", new=AsyncMock(return_value=mock_http)), \
                patch("bot.clients.qbittorrent.logger") as mock_logger:
            bound = MagicMock()
            mock_logger.bind.return_value = bound
            with pytest.raises(QBittorrentError):
                await client.login()
            assert bound.warning.called or bound.error.called


# ---------------------------------------------------------------------------
# OBS-04: pause/resume legacy-endpoint fallback logging
# ---------------------------------------------------------------------------


class TestFallbackLogging:
    @pytest.mark.asyncio
    async def test_pause_logs_on_legacy_fallback(self, client):
        calls = []

        async def fake_request(method, endpoint, data=None, params=None):
            calls.append(endpoint)
            if endpoint == "/api/v2/torrents/stop":
                raise QBittorrentError("not found", status_code=404)
            return None

        with patch.object(client, "_request", side_effect=fake_request), \
                patch("bot.clients.qbittorrent.logger") as mock_logger:
            await client.pause(["abc"])
        assert "/api/v2/torrents/pause" in calls
        assert mock_logger.info.called or mock_logger.debug.called

    @pytest.mark.asyncio
    async def test_resume_logs_on_legacy_fallback(self, client):
        calls = []

        async def fake_request(method, endpoint, data=None, params=None):
            calls.append(endpoint)
            if endpoint == "/api/v2/torrents/start":
                raise QBittorrentError("not found", status_code=404)
            return None

        with patch.object(client, "_request", side_effect=fake_request), \
                patch("bot.clients.qbittorrent.logger") as mock_logger:
            await client.resume(["abc"])
        assert "/api/v2/torrents/resume" in calls
        assert mock_logger.info.called or mock_logger.debug.called


# ---------------------------------------------------------------------------
# OBS-05: _request re-auth logging
# ---------------------------------------------------------------------------


class TestReauthLogging:
    @pytest.mark.asyncio
    async def test_request_logs_on_reauth(self, client):
        client._authenticated = True

        resp_403 = MagicMock()
        resp_403.status_code = 403
        resp_ok = MagicMock()
        resp_ok.status_code = 200
        resp_ok.text = "Ok."
        resp_ok.json.side_effect = ValueError("not json")

        mock_http = AsyncMock()
        mock_http.request.side_effect = [resp_403, resp_ok]

        async def fake_ensure():
            client._authenticated = True

        with patch.object(client, "_get_client", new=AsyncMock(return_value=mock_http)), \
                patch.object(client, "_ensure_authenticated",
                             new=AsyncMock(side_effect=fake_ensure)), \
                patch("bot.clients.qbittorrent.logger") as mock_logger:
            await client._request("GET", "/api/v2/app/version")
        assert mock_logger.info.called or mock_logger.warning.called


# ---------------------------------------------------------------------------
# PERF-01: handlers fetch the list at most once + targeted refresh
# ---------------------------------------------------------------------------


class TestHandlerSingleFetch:
    def _make_qbt(self):
        torrent = TorrentInfo(
            hash="abc123def456" + "0" * 28,
            name="Test Torrent",
            progress=0.5,
            state=TorrentState.DOWNLOADING,
        )
        qbt = AsyncMock()
        qbt.get_torrents = AsyncMock(return_value=[torrent])
        qbt.get_torrent = AsyncMock(return_value=torrent)
        qbt.get_torrent_by_short_hash = AsyncMock(return_value=torrent)
        qbt.pause = AsyncMock()
        qbt.resume = AsyncMock()
        qbt.delete = AsyncMock()
        return qbt, torrent

    def _make_callback(self, data):
        cb = AsyncMock()
        cb.data = data
        cb.message = AsyncMock()
        return cb

    @pytest.mark.asyncio
    async def test_pause_does_not_refetch_full_list(self):
        from bot.handlers import downloads

        qbt, torrent = self._make_qbt()
        cb = self._make_callback("t_pause:abc123de")

        with patch.object(downloads, "get_qbittorrent",
                          new=AsyncMock(return_value=qbt)), \
                patch.object(downloads, "_render_torrent_details",
                             new=AsyncMock()):
            await downloads.handle_pause_torrent(cb)

        # Pause was applied to the located torrent.
        qbt.pause.assert_awaited_once_with([torrent.hash])
        # The full list is never re-listed; the post-action redraw uses the
        # targeted single-torrent fetch instead.
        assert qbt.get_torrents.await_count == 0
        qbt.get_torrent.assert_awaited_once_with(torrent.hash)

    @pytest.mark.asyncio
    async def test_resume_does_not_refetch_full_list(self):
        from bot.handlers import downloads

        qbt, torrent = self._make_qbt()
        cb = self._make_callback("t_resume:abc123de")

        with patch.object(downloads, "get_qbittorrent",
                          new=AsyncMock(return_value=qbt)), \
                patch.object(downloads, "_render_torrent_details",
                             new=AsyncMock()):
            await downloads.handle_resume_torrent(cb)

        qbt.resume.assert_awaited_once_with([torrent.hash])
        assert qbt.get_torrents.await_count == 0
        qbt.get_torrent.assert_awaited_once_with(torrent.hash)


# ---------------------------------------------------------------------------
# get_torrent helper (single targeted fetch used by PERF-01)
# ---------------------------------------------------------------------------


class TestGetTorrent:
    @pytest.mark.asyncio
    async def test_get_torrent_filters_by_hash(self, client):
        raw = [{"hash": "f" * 40, "name": "x", "state": "downloading"}]
        with patch.object(client, "_request",
                          new=AsyncMock(return_value=raw)) as mock_req:
            t = await client.get_torrent("f" * 40)
        assert t is not None
        assert t.hash == "f" * 40
        # Should have passed the hash through as a server-side filter.
        _, kwargs = mock_req.call_args
        assert kwargs["params"]["hashes"] == "f" * 40

    @pytest.mark.asyncio
    async def test_get_torrent_missing_returns_none(self, client):
        with patch.object(client, "_request", new=AsyncMock(return_value=[])):
            assert await client.get_torrent("f" * 40) is None
