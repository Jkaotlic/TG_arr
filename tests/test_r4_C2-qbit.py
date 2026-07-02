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
from tests.conftest import mock_http_with_cookie


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
        """The independent fetches must run concurrently, not sequentially.

        TEST-09: deterministic barrier instead of a real asyncio.sleep race.
        get_status() fans out to exactly 4 independent calls (3 via
        `_request`, 1 via `get_torrents`); each fake implementation marks
        itself "started" and then waits until *all 4* have started before
        returning. If get_status() actually ran them sequentially, the 2nd
        call would deadlock waiting for the 3rd/4th to start — so reaching
        the `await client.get_status()` return proves all 4 were in flight
        at once, with zero reliance on wall-clock timing.
        """
        total_calls = 4
        started = 0
        all_started = asyncio.Event()
        lock = asyncio.Lock()

        async def _mark_started_and_wait():
            nonlocal started
            async with lock:
                started += 1
                if started == total_calls:
                    all_started.set()
            await asyncio.wait_for(all_started.wait(), timeout=5)

        async def fake_request(method, endpoint, data=None, params=None):
            await _mark_started_and_wait()
            if endpoint == "/api/v2/app/version":
                return "4.6.0"
            if endpoint == "/api/v2/transfer/info":
                return {}
            if endpoint == "/api/v2/sync/maindata":
                return {"server_state": {}}
            raise AssertionError(f"unexpected endpoint {endpoint}")

        async def fake_get_torrents(*a, **k):
            await _mark_started_and_wait()
            return []

        with patch.object(client, "_request", side_effect=fake_request), \
                patch.object(client, "get_torrents", side_effect=fake_get_torrents):
            # If the 4 fetches were sequential, this would hang until the
            # 5s timeout inside _mark_started_and_wait and raise
            # asyncio.TimeoutError instead of completing.
            await client.get_status()

        assert started == total_calls


# ---------------------------------------------------------------------------
# OBS-01: login failure logging
# ---------------------------------------------------------------------------


class TestLoginFailureLogging:
    @pytest.mark.asyncio
    async def test_login_auth_failure_logs(self, client):
        mock_http = mock_http_with_cookie(403, "Fails.", cookie_name=None)
        with patch.object(client, "_get_client", new=AsyncMock(return_value=mock_http)), \
                patch("bot.clients.qbittorrent.logger") as mock_logger:
            bound = MagicMock()
            mock_logger.bind.return_value = bound
            with pytest.raises(QBittorrentAuthError):
                await client.login()
            assert bound.warning.called or bound.error.called

    @pytest.mark.asyncio
    async def test_login_no_cookie_logs(self, client):
        mock_http = mock_http_with_cookie(204, "", cookie_name=None)
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

    def _make_action_callback(self, action, h):
        from bot.ui.callbacks import TorrentActionCB

        return self._make_callback(TorrentActionCB(action=action, h=h).pack())

    @pytest.mark.asyncio
    async def test_pause_does_not_refetch_full_list(self):
        """PERF-01/PERF-05: with a full-hash TorrentActionCB, both the initial
        resolve and the post-pause redraw use the targeted get_torrent fetch
        (never the full-list scan / short-hash fallback)."""
        from bot.handlers import downloads
        from bot.ui.callbacks import TorrentActionCB

        qbt, torrent = self._make_qbt()
        cb = self._make_action_callback("pause", torrent.hash)

        with patch.object(downloads, "get_qbittorrent",
                          new=AsyncMock(return_value=qbt)), \
                patch.object(downloads, "_render_torrent_details",
                             new=AsyncMock()):
            await downloads.handle_torrent_action(cb, TorrentActionCB.unpack(cb.data))

        # Pause was applied to the located torrent.
        qbt.pause.assert_awaited_once_with([torrent.hash])
        # The full list is never re-listed; both the resolve and the
        # post-action redraw use the targeted single-torrent fetch.
        assert qbt.get_torrents.await_count == 0
        assert qbt.get_torrent.await_count == 2
        qbt.get_torrent.assert_awaited_with(torrent.hash)
        qbt.get_torrent_by_short_hash.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_resume_does_not_refetch_full_list(self):
        """PERF-01/PERF-05: see test_pause_does_not_refetch_full_list."""
        from bot.handlers import downloads
        from bot.ui.callbacks import TorrentActionCB

        qbt, torrent = self._make_qbt()
        cb = self._make_action_callback("resume", torrent.hash)

        with patch.object(downloads, "get_qbittorrent",
                          new=AsyncMock(return_value=qbt)), \
                patch.object(downloads, "_render_torrent_details",
                             new=AsyncMock()):
            await downloads.handle_torrent_action(cb, TorrentActionCB.unpack(cb.data))

        qbt.resume.assert_awaited_once_with([torrent.hash])
        assert qbt.get_torrents.await_count == 0
        assert qbt.get_torrent.await_count == 2
        qbt.get_torrent.assert_awaited_with(torrent.hash)
        qbt.get_torrent_by_short_hash.assert_not_awaited()


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
