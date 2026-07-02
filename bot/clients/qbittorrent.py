"""qBittorrent Web API client."""

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from bot.models import (
    QBittorrentStatus,
    TorrentFilter,
    TorrentInfo,
    TorrentState,
)

logger = structlog.get_logger()

# State mapping from qBittorrent API to our enum
# BUG-29: qBittorrent v5 renamed pausedDL/UP → stoppedDL/UP and added "running"
STATE_MAP = {
    "allocating": TorrentState.CHECKING,
    "checkingDL": TorrentState.CHECKING,
    "checkingResumeData": TorrentState.CHECKING,
    "checkingUP": TorrentState.CHECKING,
    "downloading": TorrentState.DOWNLOADING,
    "error": TorrentState.ERROR,
    "forcedDL": TorrentState.DOWNLOADING,
    "forcedMetaDL": TorrentState.DOWNLOADING,
    "forcedUP": TorrentState.SEEDING,
    "metaDL": TorrentState.DOWNLOADING,
    "missingFiles": TorrentState.ERROR,
    "moving": TorrentState.MOVING,
    "pausedDL": TorrentState.PAUSED,
    "pausedUP": TorrentState.PAUSED,
    "queuedDL": TorrentState.QUEUED,
    "queuedUP": TorrentState.QUEUED,
    "stalledDL": TorrentState.STALLED,
    "stalledUP": TorrentState.SEEDING,
    "stoppedDL": TorrentState.PAUSED,
    "stoppedUP": TorrentState.PAUSED,
    "running": TorrentState.DOWNLOADING,
    "uploading": TorrentState.SEEDING,
    "unknown": TorrentState.UNKNOWN,
}


class QBittorrentError(Exception):
    """qBittorrent API error."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        self.message = message
        self.status_code = status_code
        super().__init__(self.message)


class QBittorrentAuthError(QBittorrentError):
    """Authentication error."""

    pass


class QBittorrentClient:
    """Client for qBittorrent Web API v2."""

    def __init__(self, base_url: str, username: str, password: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None
        self._authenticated = False
        # BUG-09: guard client creation and login against concurrent first
        # callers (notification loop vs. handlers can both hit an empty
        # ``_client``/``_authenticated`` at once) — same double-check pattern
        # as ``BaseAPIClient._client_lock`` in base.py.
        self._client_lock = asyncio.Lock()
        self._auth_lock = asyncio.Lock()

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        async with self._client_lock:
            if self._client is None or self._client.is_closed:
                self._client = httpx.AsyncClient(
                    base_url=self.base_url,
                    timeout=httpx.Timeout(self.timeout),
                    follow_redirects=True,
                    # PERF-06a: without explicit limits httpx defaults to a 5s
                    # keepalive_expiry — the notification loop polls every 60s,
                    # so every poll paid for a fresh TCP handshake.
                    limits=httpx.Limits(
                        max_keepalive_connections=4,
                        max_connections=10,
                        keepalive_expiry=300.0,
                    ),
                )
                self._authenticated = False
        return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
            self._authenticated = False

    async def _ensure_authenticated(self) -> None:
        """Ensure we have a valid session."""
        if self._authenticated:
            return
        async with self._auth_lock:
            # Double-check: another caller may have logged in while we waited.
            if not self._authenticated:
                await self.login()

    @retry(
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.ConnectError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def login(self) -> bool:
        """Authenticate with qBittorrent."""
        client = await self._get_client()

        log = logger.bind(url=self.base_url)
        log.debug("Logging in to qBittorrent")

        try:
            response = await client.post(
                "/api/v2/auth/login",
                data={
                    "username": self.username,
                    "password": self.password,
                },
                headers={"Referer": self.base_url},
            )

            if response.status_code == 403 or "Fails" in response.text:
                # OBS-01: surface the failure before raising so the cause is
                # visible in logs (bad credentials / banned IP).
                log.warning(
                    "qBittorrent login rejected (bad credentials or banned IP)",
                    status_code=response.status_code,
                )
                raise QBittorrentAuthError(
                    "Неверный логин или пароль",
                    status_code=response.status_code,
                )

            # qBit returns 200 + "Ok." (≤5.1.x) or 204 + Set-Cookie (≥5.2.0).
            # Treat as success only when a session cookie is actually set.
            session_cookie = any(
                c.name.startswith("SID") or c.name.startswith("QBT_SID")
                for c in client.cookies.jar
            )
            if response.status_code in (200, 204) and session_cookie:
                self._authenticated = True
                log.info("Successfully logged in to qBittorrent")
                return True

            # OBS-01: a 2xx without a session cookie (or any other unexpected
            # status) is an auth failure too — log it before raising.
            log.error(
                "qBittorrent login failed: no session cookie set",
                status_code=response.status_code,
            )
            raise QBittorrentError(
                "Ошибка авторизации в qBittorrent",
                status_code=response.status_code,
            )

        except httpx.ConnectError:
            raise  # Let tenacity handle retries for ConnectError

    async def _request(
        self,
        method: str,
        endpoint: str,
        data: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> Any:
        """Make authenticated request to qBittorrent API."""
        await self._ensure_authenticated()

        client = await self._get_client()

        try:
            response = await client.request(
                method=method,
                url=endpoint,
                data=data,
                params=params,
            )

            # Session expired
            if response.status_code == 403:
                # OBS-05: the mid-request re-auth was previously silent; log it
                # so session churn is observable.
                logger.info(
                    "qBittorrent session expired, re-authenticating",
                    endpoint=endpoint,
                )
                self._authenticated = False
                try:
                    await self._ensure_authenticated()
                except (QBittorrentAuthError, QBittorrentError):
                    raise  # Don't retry if re-auth failed
                # Only re-issue if re-auth succeeded
                try:
                    response = await client.request(
                        method=method,
                        url=endpoint,
                        data=data,
                        params=params,
                    )
                except httpx.TimeoutException:
                    raise QBittorrentError("Таймаут соединения с qBittorrent")
                except httpx.ConnectError:
                    raise QBittorrentError(f"Не удалось подключиться к qBittorrent ({self.base_url})")

            if response.status_code >= 400:
                raise QBittorrentError(
                    f"Ошибка API: {response.status_code}",
                    status_code=response.status_code,
                )

            # Many qBittorrent endpoints return empty response
            if not response.text:
                return None

            # Try to parse as JSON
            try:
                return response.json()
            except (json.JSONDecodeError, ValueError):
                return response.text

        except httpx.TimeoutException:
            raise QBittorrentError("Таймаут соединения с qBittorrent")
        except httpx.ConnectError:
            raise QBittorrentError(f"Не удалось подключиться к qBittorrent ({self.base_url})")

    async def get_version(self) -> str:
        """Get qBittorrent version."""
        result = await self._request("GET", "/api/v2/app/version")
        return str(result) if result else "unknown"

    async def get_transfer_info(self) -> dict:
        """Get global transfer info."""
        result = await self._request("GET", "/api/v2/transfer/info")
        return result if isinstance(result, dict) else {}

    async def get_status(self) -> QBittorrentStatus:
        """Get comprehensive qBittorrent status."""
        log = logger.bind()
        log.debug("Getting qBittorrent status")

        try:
            # PERF-05: the four endpoints are independent, so fetch them
            # concurrently instead of serially (4 round-trips → 1 wall-clock
            # round-trip). The aggregated result is identical.
            version, transfer, maindata, torrents = await asyncio.gather(
                self.get_version(),
                self.get_transfer_info(),
                self._request("GET", "/api/v2/sync/maindata"),
                self.get_torrents(),
            )
            server_state = maindata.get("server_state", {}) if maindata else {}

            active_downloads = sum(
                1 for t in torrents if t.state == TorrentState.DOWNLOADING
            )
            active_uploads = sum(
                1 for t in torrents if t.state == TorrentState.SEEDING
            )
            paused_count = sum(
                1 for t in torrents if t.state == TorrentState.PAUSED
            )

            return QBittorrentStatus(
                version=version,
                connection_status=transfer.get("connection_status", "unknown"),
                download_speed=transfer.get("dl_info_speed", 0),
                upload_speed=transfer.get("up_info_speed", 0),
                download_limit=transfer.get("dl_rate_limit", 0),
                upload_limit=transfer.get("up_rate_limit", 0),
                free_space=server_state.get("free_space_on_disk", 0),
                active_downloads=active_downloads,
                active_uploads=active_uploads,
                total_torrents=len(torrents),
                paused_torrents=paused_count,
                dht_nodes=server_state.get("dht_nodes", 0),
            )

        except Exception as e:
            log.error("Failed to get qBittorrent status", error=str(e), exc_info=True)
            raise

    async def get_torrents(
        self,
        filter_type: TorrentFilter = TorrentFilter.ALL,
        category: Optional[str] = None,
        sort: str = "added_on",
        reverse: bool = True,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[TorrentInfo]:
        """Get list of torrents."""
        params: dict[str, Any] = {
            "filter": filter_type.value,
            "sort": sort,
            "reverse": str(reverse).lower(),
        }

        if category is not None:
            params["category"] = category
        if limit is not None:
            params["limit"] = limit
        if offset > 0:
            params["offset"] = offset

        result = await self._request("GET", "/api/v2/torrents/info", params=params)

        if not isinstance(result, list):
            return []

        torrents = []
        for item in result:
            try:
                torrents.append(self._parse_torrent(item))
            except Exception as e:
                logger.warning("Failed to parse torrent", error=str(e), name=item.get("name"))

        return torrents

    async def get_torrent_by_short_hash(self, short_hash: str) -> Optional[TorrentInfo]:
        """Get torrent by partial hash prefix (fallback for legacy 16-char callback_data).

        PERF-05: current callback_data carries the full 40-hex hash, so new
        buttons resolve via the targeted ``get_torrent(full_hash)`` instead.
        This scan-based lookup remains only as a fallback for old inline
        keyboards still in a chat that were built with the previous
        16-character truncated hash.
        """
        torrents = await self.get_torrents()
        for t in torrents:
            if t.hash.lower().startswith(short_hash.lower()):
                return t
        return None

    async def get_torrent(self, full_hash: str) -> Optional[TorrentInfo]:
        """Fetch a single torrent by its full hash.

        PERF-01: uses qBittorrent's server-side ``hashes`` filter on
        ``/torrents/info`` so callers needing one torrent (e.g. the post-action
        redraw after pause/resume) do not pull and parse the entire list.
        """
        result = await self._request(
            "GET",
            "/api/v2/torrents/info",
            params={"hashes": full_hash},
        )

        if not isinstance(result, list) or not result:
            return None

        for item in result:
            if str(item.get("hash", "")).lower() == full_hash.lower():
                try:
                    return self._parse_torrent(item)
                except Exception as e:
                    logger.warning(
                        "Failed to parse torrent", error=str(e), name=item.get("name")
                    )
                    return None
        return None

    async def pause(self, hashes: list[str] | str = "all") -> None:
        """Pause torrent(s). Use 'all' to pause all."""
        if isinstance(hashes, list):
            hashes = "|".join(hashes)

        # Try new API (v5.0+) first, fall back to old API
        try:
            await self._request("POST", "/api/v2/torrents/stop", data={"hashes": hashes})
        except QBittorrentError as e:
            if e.status_code == 404:
                # OBS-04: make the legacy-endpoint fallback visible.
                logger.info(
                    "qBittorrent /torrents/stop unavailable (404), "
                    "falling back to legacy /torrents/pause",
                )
                await self._request("POST", "/api/v2/torrents/pause", data={"hashes": hashes})
            else:
                raise
        logger.info("Paused torrents", hashes=hashes)

    async def resume(self, hashes: list[str] | str = "all") -> None:
        """Resume torrent(s). Use 'all' to resume all."""
        if isinstance(hashes, list):
            hashes = "|".join(hashes)

        # Try new API (v5.0+) first, fall back to old API
        try:
            await self._request("POST", "/api/v2/torrents/start", data={"hashes": hashes})
        except QBittorrentError as e:
            if e.status_code == 404:
                # OBS-04: make the legacy-endpoint fallback visible.
                logger.info(
                    "qBittorrent /torrents/start unavailable (404), "
                    "falling back to legacy /torrents/resume",
                )
                await self._request("POST", "/api/v2/torrents/resume", data={"hashes": hashes})
            else:
                raise
        logger.info("Resumed torrents", hashes=hashes)

    async def delete(self, hashes: list[str] | str, delete_files: bool = False) -> None:
        """Delete torrent(s)."""
        if isinstance(hashes, list):
            hashes = "|".join(hashes)

        await self._request(
            "POST",
            "/api/v2/torrents/delete",
            data={
                "hashes": hashes,
                "deleteFiles": str(delete_files).lower(),
            },
        )
        logger.info("Deleted torrents", hashes=hashes, delete_files=delete_files)

    async def set_download_limit(self, limit: int) -> None:
        """Set global download speed limit in bytes/s. 0 = unlimited."""
        await self._request(
            "POST",
            "/api/v2/transfer/setDownloadLimit",
            data={"limit": limit},
        )
        logger.info("Set download limit", limit=limit)

    async def set_upload_limit(self, limit: int) -> None:
        """Set global upload speed limit in bytes/s. 0 = unlimited."""
        await self._request(
            "POST",
            "/api/v2/transfer/setUploadLimit",
            data={"limit": limit},
        )
        logger.info("Set upload limit", limit=limit)

    async def add_torrent_url(
        self,
        urls: list[str] | str,
        category: Optional[str] = None,
        paused: bool = False,
    ) -> bool:
        """Add torrent by URL(s) or magnet link(s).

        Returns:
            True if torrent was added successfully, False otherwise.
        """
        if isinstance(urls, list):
            urls = "\n".join(urls)

        data: dict[str, Any] = {"urls": urls}
        if category:
            data["category"] = category
        if paused:
            data["paused"] = "true"

        result = await self._request("POST", "/api/v2/torrents/add", data=data)

        # BUG-05: _request already raises on HTTP >= 400, so reaching here means a
        # 2xx. qBittorrent returns "Ok." (<=5.1.x) or an empty body (>=5.2.0) on
        # success, and "Fails." only when it actually rejects the torrent. Treat
        # anything that is not an explicit "Fails." as success.
        if isinstance(result, str) and "fails" in result.strip().lower():
            logger.error("Failed to add torrent", result=result, category=category)
            return False
        logger.info("Added torrent from URL", category=category)
        return True

    def _parse_torrent(self, item: dict) -> TorrentInfo:
        """Parse qBittorrent torrent response to TorrentInfo."""
        state_str = item.get("state", "unknown")
        state = STATE_MAP.get(state_str, TorrentState.UNKNOWN)

        # Detect completed state
        progress = item.get("progress", 0)
        if progress >= 1.0 and state not in (TorrentState.SEEDING, TorrentState.PAUSED):
            state = TorrentState.COMPLETED

        # Parse timestamps
        added_on = None
        if item.get("added_on", 0) > 0:
            added_on = datetime.fromtimestamp(item["added_on"], tz=timezone.utc)

        completion_on = None
        if item.get("completion_on", 0) > 0:
            completion_on = datetime.fromtimestamp(item["completion_on"], tz=timezone.utc)

        # Parse tags
        tags = []
        if item.get("tags"):
            tags = [t.strip() for t in item["tags"].split(",") if t.strip()]

        return TorrentInfo(
            hash=item.get("hash", ""),
            name=item.get("name", "Unknown"),
            size=item.get("total_size", 0) or item.get("size", 0),
            progress=progress,
            download_speed=item.get("dlspeed", 0),
            upload_speed=item.get("upspeed", 0),
            eta=item.get("eta"),
            state=state,
            category=item.get("category") or None,
            tags=tags,
            added_on=added_on,
            completion_on=completion_on,
            save_path=item.get("save_path", "") or item.get("content_path", ""),
            seeds=item.get("num_seeds", 0),
            seeds_total=item.get("num_complete", 0),
            peers=item.get("num_leechs", 0),
            peers_total=item.get("num_incomplete", 0),
            ratio=item.get("ratio", 0),
            uploaded=item.get("uploaded", 0),
            downloaded=item.get("downloaded", 0),
            tracker=item.get("tracker") or None,
        )

    async def check_connection(self) -> tuple[bool, Optional[str], Optional[float]]:
        """Check if qBittorrent is available."""
        start_time = time.monotonic()
        try:
            version = await self.get_version()
            elapsed = (time.monotonic() - start_time) * 1000
            return True, version, round(elapsed, 2)
        except Exception as e:
            elapsed = (time.monotonic() - start_time) * 1000
            logger.warning("health_check_failed", service="qBittorrent", error=str(e))
            return False, None, round(elapsed, 2)
