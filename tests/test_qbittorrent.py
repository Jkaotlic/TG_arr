"""Tests for qBittorrent integration."""

import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from bot.clients.qbittorrent import QBittorrentAuthError, QBittorrentClient, STATE_MAP
from bot.models import (
    QBittorrentStatus,
    TorrentFilter,
    TorrentInfo,
    TorrentState,
    format_bytes,
    format_speed,
)
from bot.services.notification_service import NotificationService
from bot.ui.formatters import Formatters
from bot.ui.keyboards import Keyboards, CallbackData


class TestQBittorrentClient:
    """Test qBittorrent client functionality."""

    @pytest.fixture
    def client(self):
        """Create a qBittorrent client for testing."""
        return QBittorrentClient(
            "http://localhost:8080",
            "admin",
            "password123",
        )

    def test_init(self, client):
        """Test client initialization."""
        assert client.base_url == "http://localhost:8080"
        assert client.username == "admin"
        assert client.password == "password123"
        assert client._authenticated is False

    def _mock_http_with_cookie(self, status_code: int, text: str, cookie_name: str | None = "SID"):
        """Build mock httpx client with optional session cookie in jar."""
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

    @pytest.mark.asyncio
    async def test_login_success_legacy_200_ok(self, client):
        """qBit ≤5.1.x returns 200 + 'Ok.' + Set-Cookie."""
        mock_http = self._mock_http_with_cookie(200, "Ok.", "SID")

        with patch.object(client, '_get_client', new_callable=AsyncMock, return_value=mock_http):
            result = await client.login()

            assert result is True
            assert client._authenticated is True

    @pytest.mark.asyncio
    async def test_login_success_qbit_5_2_204(self, client):
        """qBit ≥5.2.0 returns 204 No Content + Set-Cookie (no body)."""
        mock_http = self._mock_http_with_cookie(204, "", "QBT_SID_8080")

        with patch.object(client, '_get_client', new_callable=AsyncMock, return_value=mock_http):
            result = await client.login()

            assert result is True
            assert client._authenticated is True

    @pytest.mark.asyncio
    async def test_login_failure_fails_body(self, client):
        """qBit returns 200 + 'Fails.' on bad credentials."""
        mock_http = self._mock_http_with_cookie(200, "Fails.", cookie_name=None)

        with patch.object(client, '_get_client', new_callable=AsyncMock, return_value=mock_http):
            with pytest.raises(QBittorrentAuthError):
                await client.login()

    @pytest.mark.asyncio
    async def test_login_failure_no_cookie(self, client):
        """2xx response without session cookie should not be treated as success."""
        mock_http = self._mock_http_with_cookie(204, "", cookie_name=None)

        with patch.object(client, '_get_client', new_callable=AsyncMock, return_value=mock_http):
            from bot.clients.qbittorrent import QBittorrentError
            with pytest.raises(QBittorrentError):
                await client.login()
            assert client._authenticated is False

    @pytest.mark.asyncio
    async def test_add_torrent_url_success(self, client):
        """Test successful torrent addition."""
        with patch.object(client, '_request', new_callable=AsyncMock) as mock_request:
            mock_request.return_value = "Ok."

            result = await client.add_torrent_url(
                "http://example.com/torrent.torrent",
                category="test",
            )

            assert result is True
            mock_request.assert_called_once()
            call_args = mock_request.call_args
            assert call_args[0][0] == "POST"
            assert call_args[0][1] == "/api/v2/torrents/add"
            assert call_args[1]["data"]["category"] == "test"

    @pytest.mark.asyncio
    async def test_add_torrent_url_failure(self, client):
        """Test failed torrent addition."""
        with patch.object(client, '_request', new_callable=AsyncMock) as mock_request:
            mock_request.return_value = "Fails."

            result = await client.add_torrent_url(
                "http://example.com/invalid.torrent",
            )

            assert result is False
            mock_request.assert_called_once()

    def test_parse_torrent_state(self, client):
        """Test parsing torrent states via STATE_MAP."""
        assert STATE_MAP.get("downloading", TorrentState.UNKNOWN) == TorrentState.DOWNLOADING
        assert STATE_MAP.get("uploading", TorrentState.UNKNOWN) == TorrentState.SEEDING
        assert STATE_MAP.get("pausedDL", TorrentState.UNKNOWN) == TorrentState.PAUSED
        assert STATE_MAP.get("stalledDL", TorrentState.UNKNOWN) == TorrentState.STALLED
        assert STATE_MAP.get("error", TorrentState.UNKNOWN) == TorrentState.ERROR
        assert STATE_MAP.get("unknown_state", TorrentState.UNKNOWN) == TorrentState.UNKNOWN

    def test_normalize_torrent(self, client):
        """Test normalizing torrent data."""
        raw = {
            "hash": "abc123def456",
            "name": "Test.Torrent.2024.1080p",
            "total_size": 5000000000,
            "progress": 0.75,
            "dlspeed": 1500000,
            "upspeed": 500000,
            "eta": 3600,
            "state": "downloading",
            "category": "movies",
            "tags": "hd,new",
            "added_on": 1703980800,
            "completion_on": 0,
            "save_path": "/downloads/movies",
            "num_seeds": 10,
            "num_complete": 50,
            "num_leechs": 5,
            "num_incomplete": 20,
            "ratio": 1.5,
            "uploaded": 7500000000,
            "downloaded": 5000000000,
            "tracker": "http://tracker.example.com",
        }

        torrent = client._parse_torrent(raw)

        assert torrent is not None
        assert torrent.hash == "abc123def456"
        assert torrent.name == "Test.Torrent.2024.1080p"
        assert torrent.progress == 0.75
        assert torrent.download_speed == 1500000
        assert torrent.state == TorrentState.DOWNLOADING
        assert torrent.category == "movies"
        assert "hd" in torrent.tags
        assert "new" in torrent.tags

    def test_normalize_torrent_minimal(self, client):
        """Test normalizing minimal torrent data."""
        raw = {
            "hash": "abc123",
            "name": "Minimal Torrent",
        }

        torrent = client._parse_torrent(raw)

        assert torrent is not None
        assert torrent.hash == "abc123"
        assert torrent.name == "Minimal Torrent"
        assert torrent.progress == 0.0
        assert torrent.state == TorrentState.UNKNOWN

    # ------------------------------------------------------------------
    # BUG-09: concurrent first callers must not create multiple clients or
    # perform multiple logins.
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_get_client_concurrent_first_callers_create_one_client(self, client):
        """Two concurrent first calls to _get_client must share one AsyncClient."""
        import asyncio

        results = await asyncio.gather(*(client._get_client() for _ in range(5)))

        assert all(r is results[0] for r in results)

    @pytest.mark.asyncio
    async def test_ensure_authenticated_concurrent_callers_login_once(self, client):
        """Concurrent _ensure_authenticated callers must only trigger one login()."""
        import asyncio

        login_calls = 0
        login_started = asyncio.Event()
        release_login = asyncio.Event()

        async def fake_login():
            nonlocal login_calls
            login_calls += 1
            login_started.set()
            await release_login.wait()
            client._authenticated = True

        with patch.object(client, "login", side_effect=fake_login) as mock_login:
            task1 = asyncio.create_task(client._ensure_authenticated())
            await login_started.wait()
            task2 = asyncio.create_task(client._ensure_authenticated())
            # give task2 a chance to reach the lock/double-check
            await asyncio.sleep(0)
            release_login.set()
            await asyncio.gather(task1, task2)

        assert mock_login.call_count == 1
        assert login_calls == 1

    def test_client_has_dedicated_locks(self, client):
        """BUG-09: guard locks exist (base.py pattern) instead of unguarded checks."""
        import asyncio

        assert isinstance(client._client_lock, asyncio.Lock)
        assert isinstance(client._auth_lock, asyncio.Lock)

    # ------------------------------------------------------------------
    # PERF-06a: keepalive-friendly httpx.Limits (base.py already does this;
    # qBittorrent's client previously used bare defaults -> 5s keepalive).
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_get_client_sets_keepalive_limits(self, client):
        """PERF-06a: the created AsyncClient must carry non-default keepalive limits."""
        http_client = await client._get_client()
        try:
            pool = http_client._transport._pool
            assert pool._max_keepalive_connections == 4
            assert pool._max_connections == 10
            assert pool._keepalive_expiry == 300.0
        finally:
            await client.close()

    # ------------------------------------------------------------------
    # PERF-05: get_torrent uses the server-side hashes filter (targeted,
    # single-row) rather than a full-list scan.
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_get_torrent_full_hash_uses_hashes_param(self, client):
        full_hash = "d" * 40
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = [{"hash": full_hash, "name": "X"}]

            result = await client.get_torrent(full_hash)

            assert result is not None
            assert result.hash == full_hash
            mock_request.assert_awaited_once()
            call_args = mock_request.call_args
            assert call_args[1]["params"] == {"hashes": full_hash}

    def test_get_torrent_by_short_hash_docstring_mentions_16_chars(self, client):
        """LOGIC-22: docstring must reflect the actual 16-char legacy truncation."""
        doc = client.get_torrent_by_short_hash.__doc__ or ""
        assert "16" in doc
        assert "first 8 chars" not in doc


class TestTorrentInfo:
    """Test TorrentInfo model."""

    @pytest.fixture
    def torrent(self):
        """Create a sample torrent for testing."""
        return TorrentInfo(
            hash="abc123def456",
            name="Test.Movie.2024.1080p.BluRay",
            size=5000000000,
            progress=0.5,
            download_speed=1500000,
            upload_speed=500000,
            eta=3600,
            state=TorrentState.DOWNLOADING,
            seeds=10,
            seeds_total=50,
            peers=5,
            peers_total=20,
            ratio=0.5,
            save_path="/downloads/movies",
        )

    def test_progress_percent(self, torrent):
        """Test progress percentage calculation."""
        assert torrent.progress_percent == 50

        torrent.progress = 1.0
        assert torrent.progress_percent == 100

        torrent.progress = 0.0
        assert torrent.progress_percent == 0

    def test_eta_formatted(self, torrent):
        """Test ETA formatting."""
        torrent.eta = 3600
        assert torrent.eta_formatted == "1h 0m"

        torrent.eta = 90
        assert torrent.eta_formatted == "1m 30s"

        torrent.eta = 45
        assert torrent.eta_formatted == "45s"

        torrent.eta = 90000  # 25 hours
        assert "d" in torrent.eta_formatted

        torrent.eta = -1
        assert torrent.eta_formatted == "∞"

        torrent.eta = None
        assert torrent.eta_formatted == "∞"

    def test_size_formatted(self, torrent):
        """Test size formatting."""
        torrent.size = 5000000000
        assert "GB" in torrent.size_formatted

        torrent.size = 500000
        assert "KB" in torrent.size_formatted

        torrent.size = 0
        assert torrent.size_formatted == "0 B"

    def test_state_emoji(self, torrent):
        """Test state emoji."""
        torrent.state = TorrentState.DOWNLOADING
        assert torrent.state_emoji == "⬇️"

        torrent.state = TorrentState.SEEDING
        assert torrent.state_emoji == "⬆️"

        torrent.state = TorrentState.PAUSED
        assert torrent.state_emoji == "⏸️"

        torrent.state = TorrentState.ERROR
        assert torrent.state_emoji == "❌"


class TestQBittorrentStatus:
    """Test QBittorrentStatus model."""

    @pytest.fixture
    def status(self):
        """Create a sample status for testing."""
        return QBittorrentStatus(
            version="4.6.0",
            connection_status="connected",
            download_speed=5000000,
            upload_speed=1000000,
            download_limit=10000000,
            upload_limit=0,
            free_space=500000000000,
            active_downloads=3,
            active_uploads=5,
            total_torrents=25,
            paused_torrents=2,
            dht_nodes=500,
        )

    def test_download_speed_formatted(self, status):
        """Test download speed formatting."""
        assert "MB/s" in status.download_speed_formatted

    def test_upload_speed_formatted(self, status):
        """Test upload speed formatting."""
        assert "KB/s" in status.upload_speed_formatted or "MB/s" in status.upload_speed_formatted

    def test_free_space_formatted(self, status):
        """Test free space formatting."""
        assert "GB" in status.free_space_formatted


class TestFormatters:
    """Test torrent formatters."""

    @pytest.fixture
    def torrent(self):
        """Create a sample torrent for testing."""
        return TorrentInfo(
            hash="abc123def456",
            name="Test.Movie.2024.1080p.BluRay",
            size=5000000000,
            progress=0.75,
            download_speed=1500000,
            upload_speed=500000,
            eta=3600,
            state=TorrentState.DOWNLOADING,
            seeds=10,
            seeds_total=50,
            peers=5,
            peers_total=20,
            ratio=0.5,
            save_path="/downloads/movies",
            added_on=datetime(2024, 1, 1, 12, 0, 0),
        )

    @pytest.fixture
    def status(self):
        """Create a sample status for testing."""
        return QBittorrentStatus(
            version="4.6.0",
            connection_status="connected",
            download_speed=5000000,
            upload_speed=1000000,
            free_space=500000000000,
            active_downloads=3,
            active_uploads=5,
            total_torrents=25,
        )

    def test_format_qbittorrent_status(self, status):
        """Test formatting qBittorrent status."""
        result = Formatters.format_qbittorrent_status(status)

        assert "qBittorrent" in result
        assert "4.6.0" in result
        assert "подключён" in result or "Соединение" in result
        assert "Загрузка" in result or "⬇️" in result
        assert "Отдача" in result or "⬆️" in result
        assert "Торренты" in result or "Всего" in result

    def test_format_torrent_details(self, torrent):
        """Test formatting torrent details."""
        result = Formatters.format_torrent_details(torrent)

        assert torrent.name in result
        assert "75%" in result
        assert "Загрузка" in result or "⬇️" in result
        assert "Сиды" in result or "Пиры" in result
        assert torrent.save_path in result

    def test_format_torrent_compact(self, torrent):
        """Test formatting compact torrent info."""
        result = Formatters.format_torrent_compact(torrent)

        assert "75%" in result
        assert "⬇️" in result

    def test_format_no_torrents(self):
        """Test formatting no torrents message."""
        result = Formatters.format_no_torrents(TorrentFilter.ALL)
        assert "Торрентов нет" in result or "нет" in result

        result = Formatters.format_no_torrents(TorrentFilter.DOWNLOADING)
        assert "загр" in result.lower() or "нет" in result

    def test_format_speed_limit_changed(self):
        """Test formatting speed limit change message."""
        result = Formatters.format_speed_limit_changed("dl", 1024)
        assert "Загрузка" in result or "⬇️" in result
        assert "1" in result

        result = Formatters.format_speed_limit_changed("ul", 0)
        assert "Отдача" in result or "⬆️" in result
        assert "без ограничений" in result or "∞" in result

    def test_format_torrent_action(self):
        """Test formatting torrent action result."""
        result = Formatters.format_torrent_action("pause", "Test.Torrent", True)
        assert "Пауза" in result or "⏸" in result

        result = Formatters.format_torrent_action("resume", "Test.Torrent", True)
        assert "Возобновлен" in result or "▶️" in result

        result = Formatters.format_torrent_action("pause", "Test.Torrent", False)
        assert "Ошибка" in result or "❌" in result


class TestKeyboards:
    """Test torrent keyboards."""

    @pytest.fixture
    def torrents(self):
        """Create sample torrents for testing."""
        return [
            TorrentInfo(
                hash="abc123",
                name="Torrent 1",
                progress=0.5,
                state=TorrentState.DOWNLOADING,
                download_speed=1000000,
            ),
            TorrentInfo(
                hash="def456",
                name="Torrent 2",
                progress=1.0,
                state=TorrentState.SEEDING,
                upload_speed=500000,
            ),
        ]

    def test_torrent_list_keyboard(self, torrents):
        """Test torrent list keyboard creation."""
        keyboard = Keyboards.torrent_list(torrents)

        assert keyboard is not None
        assert len(keyboard.inline_keyboard) > 0

        # Check torrent buttons exist
        first_button = keyboard.inline_keyboard[0][0]
        assert "abc123" in first_button.callback_data

    def test_torrent_list_pagination(self):
        """Test torrent list pagination.

        LOGIC-01: pagination now uses the typed ``TorrentPageCB`` (prefix
        "tpg:") instead of the old plain ``t_page:N`` string, so the filter
        survives pagination round-trips. See test_feat_callbackdata-style
        assertions below.
        """
        from bot.ui.callbacks import TorrentPageCB

        torrents = [
            TorrentInfo(hash=f"hash{i}", name=f"Torrent {i}", progress=0.5)
            for i in range(10)
        ]

        keyboard = Keyboards.torrent_list(
            torrents[:5], current_page=0, total_pages=2, current_filter=TorrentFilter.DOWNLOADING
        )

        # Check pagination buttons exist and carry the active filter
        page_cbs = [
            btn.callback_data
            for row in keyboard.inline_keyboard
            for btn in row
            if btn.callback_data and btn.callback_data.startswith("tpg:")
        ]
        assert page_cbs, "no typed TorrentPageCB pagination button found"
        unpacked = [TorrentPageCB.unpack(cb) for cb in page_cbs]
        assert any(u.flt == TorrentFilter.DOWNLOADING.value for u in unpacked)

    def test_torrent_details_keyboard(self):
        """Test torrent details keyboard creation."""
        torrent = TorrentInfo(
            hash="abc123def456",
            name="Test Torrent",
            progress=0.5,
            state=TorrentState.DOWNLOADING,
        )

        keyboard = Keyboards.torrent_details(torrent)

        assert keyboard is not None
        # Should have pause button for downloading torrent
        buttons_text = [btn.text for row in keyboard.inline_keyboard for btn in row]
        assert any("пауза" in text.lower() for text in buttons_text)

    def test_torrent_details_paused(self):
        """Test torrent details keyboard for paused torrent."""
        torrent = TorrentInfo(
            hash="abc123def456",
            name="Test Torrent",
            progress=0.5,
            state=TorrentState.PAUSED,
        )

        keyboard = Keyboards.torrent_details(torrent)

        # Should have resume button for paused torrent
        buttons_text = [btn.text for row in keyboard.inline_keyboard for btn in row]
        assert any("возобновить" in text.lower() for text in buttons_text)

    def test_torrent_filters_keyboard(self):
        """Test torrent filters keyboard creation."""
        keyboard = Keyboards.torrent_filters(TorrentFilter.DOWNLOADING)

        assert keyboard is not None
        # Check filter buttons exist
        buttons_data = [btn.callback_data for row in keyboard.inline_keyboard for btn in row]
        assert any(CallbackData.TORRENT_FILTER in data for data in buttons_data if data)

    def test_speed_limits_menu(self):
        """Test speed limits menu keyboard."""
        keyboard = Keyboards.speed_limits_menu(
            current_dl_limit=1024 * 1024,
            current_ul_limit=0,
        )

        assert keyboard is not None
        # Check speed limit buttons exist
        buttons_data = [btn.callback_data for row in keyboard.inline_keyboard for btn in row]
        assert any(CallbackData.SPEED_LIMIT in data for data in buttons_data if data)

    def test_confirm_delete_torrent(self):
        """Test delete confirmation keyboard."""
        keyboard = Keyboards.confirm_delete_torrent("abc123", with_files=False)

        assert keyboard is not None
        buttons_text = [btn.text for row in keyboard.inline_keyboard for btn in row]
        assert any("удалить" in text.lower() or "отмена" in text.lower() for text in buttons_text)

        keyboard_with_files = Keyboards.confirm_delete_torrent("abc123", with_files=True)
        buttons_text = [btn.text for row in keyboard_with_files.inline_keyboard for btn in row]
        assert any("файл" in text.lower() for text in buttons_text)


class TestNotificationService:
    """Test notification service."""

    @pytest.fixture
    def mock_qbittorrent(self):
        """Create mock qBittorrent client."""
        client = AsyncMock()
        client.login = AsyncMock(return_value=True)
        client.get_torrents = AsyncMock(return_value=[])
        return client

    @pytest.fixture
    def mock_sender(self):
        """Create mock notification sender."""
        return AsyncMock()

    @pytest.fixture
    def service(self, mock_qbittorrent, mock_sender):
        """Create notification service."""
        return NotificationService(mock_qbittorrent, mock_sender)

    def test_subscribe_user(self, service):
        """Test subscribing a user."""
        service.subscribe_user(12345)
        assert 12345 in service.get_subscribed_users()

    def test_unsubscribe_user(self, service):
        """Test unsubscribing a user."""
        service.subscribe_user(12345)
        service.unsubscribe_user(12345)
        assert 12345 not in service.get_subscribed_users()

    def test_get_stats(self, service):
        """Test getting service stats."""
        service.subscribe_user(12345)
        stats = service.get_stats()

        assert stats["running"] is False
        assert stats["subscribed_users"] == 1
        assert stats["tracked_torrents"] == 0

    @pytest.mark.asyncio
    async def test_force_check(self, service, mock_qbittorrent):
        """Test force check for completions."""
        # Add a completing torrent
        mock_qbittorrent.get_torrents.return_value = [
            TorrentInfo(
                hash="abc123",
                name="Test Torrent",
                progress=1.0,
                state=TorrentState.COMPLETED,
            )
        ]

        # First sync to populate tracked torrents
        service._tracked_torrents["abc123"] = {
            "completed": False,
            "notified": False,
            "name": "Test Torrent",
            "added_on": None,
        }

        newly_completed = await service.force_check()

        assert len(newly_completed) == 1
        assert newly_completed[0].hash == "abc123"


class TestUtilityFunctions:
    """Test utility functions."""

    def test_format_bytes(self):
        """Test byte formatting."""
        assert format_bytes(0) == "0 B"
        assert "KB" in format_bytes(1024)
        assert "MB" in format_bytes(1024 * 1024)
        assert "GB" in format_bytes(1024 * 1024 * 1024)
        assert "TB" in format_bytes(1024 * 1024 * 1024 * 1024)

    def test_format_speed(self):
        """Test speed formatting."""
        assert format_speed(0) == "0 B/s"
        assert "KB/s" in format_speed(1024)
        assert "MB/s" in format_speed(1024 * 1024)
        assert "GB/s" in format_speed(1024 * 1024 * 1024)
