"""Emby Media Server API client."""

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import httpx
import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = structlog.get_logger()


@dataclass
class EmbyServerInfo:
    """Emby server information."""

    server_name: str
    version: str
    operating_system: str
    has_pending_restart: bool
    has_update_available: bool
    can_self_restart: bool
    can_self_update: bool
    local_address: Optional[str] = None
    wan_address: Optional[str] = None


@dataclass
class EmbyLibrary:
    """Emby library information."""

    id: str
    name: str
    collection_type: str  # movies, tvshows, music, etc.
    item_count: int = 0


@dataclass
class EmbyUpdateInfo:
    """Emby update information."""

    version: str
    changelog: Optional[str] = None
    is_available: bool = False


class EmbyError(Exception):
    """Emby API error."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        self.message = message
        self.status_code = status_code
        super().__init__(self.message)


class EmbyAuthError(EmbyError):
    """Authentication error."""

    pass


class EmbyClient:
    """Client for Emby Media Server API."""

    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(30.0),
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    def _get_headers(self) -> dict[str, str]:
        """Get headers with API key."""
        return {
            "X-Emby-Token": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    @retry(
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.ConnectError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[dict] = None,
        json_data: Optional[dict] = None,
    ) -> Any:
        """Make authenticated request to Emby API."""
        client = await self._get_client()

        try:
            response = await client.request(
                method=method,
                url=endpoint,
                headers=self._get_headers(),
                params=params,
                json=json_data,
            )

            if response.status_code == 401:
                raise EmbyAuthError("Invalid API key", status_code=401)

            if response.status_code >= 400:
                raise EmbyError(
                    f"API error: {response.status_code} - {response.text[:200]}",
                    status_code=response.status_code,
                )

            # Many endpoints return empty response on success
            if not response.text or response.status_code == 204:
                return None

            try:
                return response.json()
            except Exception:
                return response.text

        except httpx.TimeoutException as e:
            raise EmbyError(f"Request timeout: {e}")
        except httpx.ConnectError as e:
            raise EmbyError(f"Connection error: {e}")

    async def get_server_info(self) -> EmbyServerInfo:
        """Get server information."""
        result = await self._request("GET", "/System/Info")

        if not isinstance(result, dict):
            raise EmbyError("Invalid server info response")

        return EmbyServerInfo(
            server_name=result.get("ServerName", "Unknown"),
            version=result.get("Version", "Unknown"),
            operating_system=result.get("OperatingSystemDisplayName", result.get("OperatingSystem", "Unknown")),
            has_pending_restart=result.get("HasPendingRestart", False),
            has_update_available=result.get("HasUpdateAvailable", False),
            can_self_restart=result.get("CanSelfRestart", False),
            can_self_update=result.get("CanSelfUpdate", False),
            local_address=result.get("LocalAddress"),
            wan_address=result.get("WanAddress"),
        )

    async def get_public_info(self) -> dict:
        """Get public server info (no auth required)."""
        client = await self._get_client()
        response = await client.get("/System/Info/Public")
        return response.json() if response.status_code == 200 else {}

    async def get_libraries(self) -> list[EmbyLibrary]:
        """Get all media libraries."""
        result = await self._request("GET", "/Library/VirtualFolders")

        if not isinstance(result, list):
            return []

        libraries = []
        for item in result:
            libraries.append(EmbyLibrary(
                id=item.get("ItemId", ""),
                name=item.get("Name", "Unknown"),
                collection_type=item.get("CollectionType", "unknown"),
                item_count=item.get("LibraryOptions", {}).get("EnabledMetadataFetchersCount", 0),
            ))

        return libraries

    async def refresh_library(self, library_id: Optional[str] = None) -> None:
        """
        Refresh media library.

        Args:
            library_id: Specific library ID to refresh, or None for all libraries
        """
        if library_id:
            await self._request("POST", f"/Items/{library_id}/Refresh")
            logger.info("Library refresh started", library_id=library_id)
        else:
            await self._request("POST", "/Library/Refresh")
            logger.info("Full library refresh started")

    async def scan_library(self) -> None:
        """Trigger a full library scan."""
        await self._request("POST", "/Library/Refresh")
        logger.info("Library scan triggered")

    async def get_scheduled_tasks(self) -> list[dict]:
        """Get list of scheduled tasks."""
        result = await self._request("GET", "/ScheduledTasks")
        return result if isinstance(result, list) else []

    async def run_scheduled_task(self, task_id: str) -> None:
        """Run a scheduled task by ID."""
        await self._request("POST", f"/ScheduledTasks/Running/{task_id}")
        logger.info("Scheduled task started", task_id=task_id)

    async def get_running_tasks(self) -> list[dict]:
        """Get currently running tasks."""
        tasks = await self.get_scheduled_tasks()
        return [t for t in tasks if t.get("State") == "Running"]

    async def check_for_updates(self) -> EmbyUpdateInfo:
        """Check if updates are available."""
        try:
            result = await self._request("GET", "/System/Info")

            if isinstance(result, dict):
                has_update = result.get("HasUpdateAvailable", False)
                return EmbyUpdateInfo(
                    version=result.get("Version", "Unknown"),
                    is_available=has_update,
                )
        except Exception as e:
            logger.warning("Failed to check for updates", error=str(e))

        return EmbyUpdateInfo(version="Unknown", is_available=False)

    async def install_update(self) -> None:
        """Install available update (if supported)."""
        info = await self.get_server_info()

        if not info.can_self_update:
            raise EmbyError("Server does not support self-update")

        if not info.has_update_available:
            raise EmbyError("No update available")

        await self._request("POST", "/System/Update")
        logger.info("Update installation started")

    async def restart_server(self) -> None:
        """Restart the Emby server."""
        info = await self.get_server_info()

        if not info.can_self_restart:
            raise EmbyError("Server does not support self-restart")

        await self._request("POST", "/System/Restart")
        logger.info("Server restart initiated")

    async def shutdown_server(self) -> None:
        """Shutdown the Emby server."""
        await self._request("POST", "/System/Shutdown")
        logger.info("Server shutdown initiated")

    async def get_activity_log(self, limit: int = 10) -> list[dict]:
        """Get recent activity log entries."""
        params = {"Limit": limit, "StartIndex": 0}
        result = await self._request("GET", "/System/ActivityLog/Entries", params=params)

        if isinstance(result, dict):
            return result.get("Items", [])
        return []

    async def get_sessions(self) -> list[dict]:
        """Get active sessions (who is watching/playing)."""
        result = await self._request("GET", "/Sessions")
        return result if isinstance(result, list) else []

    async def check_connection(self) -> tuple[bool, Optional[str], Optional[float]]:
        """Check if Emby is available."""
        start_time = time.monotonic()
        try:
            info = await self.get_server_info()
            elapsed = (time.monotonic() - start_time) * 1000
            return True, info.version, round(elapsed, 2)
        except Exception as e:
            elapsed = (time.monotonic() - start_time) * 1000
            logger.warning("Emby health check failed", error=str(e))
            return False, None, round(elapsed, 2)
