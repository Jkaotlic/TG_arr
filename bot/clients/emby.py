"""Emby Media Server API client."""

import time
from dataclasses import dataclass
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


class EmbyError(Exception):
    """Emby API error."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        self.message = message
        self.status_code = status_code
        super().__init__(self.message)


class EmbyAuthError(EmbyError):
    """Authentication error."""

    pass


class EmbyConnectionError(EmbyError):
    """Transient transport failure; safe for the request retry policy."""

    pass


class EmbyClient:
    """Client for Emby Media Server API."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None
        self._server_id: Optional[str] = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout),
                follow_redirects=True,
                # PERF-06b: default httpx keepalive_expiry is 5s — the
                # notification/status poll cadence (~60s) means every request
                # re-handshakes TCP. Match BaseAPIClient's pool settings.
                limits=httpx.Limits(
                    max_keepalive_connections=4,
                    max_connections=10,
                    keepalive_expiry=300.0,
                ),
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
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.ConnectError, EmbyConnectionError)),
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
                raise EmbyAuthError("Неверный API ключ", status_code=401)

            if response.status_code >= 400:
                raise EmbyError(
                    f"Ошибка API: {response.status_code}",
                    status_code=response.status_code,
                )

            # Many endpoints return empty response on success
            if not response.text or response.status_code == 204:
                return None

            try:
                return response.json()
            except Exception:
                return response.text

        except httpx.TimeoutException as exc:
            raise EmbyConnectionError("Таймаут соединения с Emby") from exc
        except httpx.ConnectError as exc:
            raise EmbyConnectionError(f"Не удалось подключиться к Emby ({self.base_url})") from exc

    async def get_server_info(self) -> EmbyServerInfo:
        """Get server information."""
        result = await self._request("GET", "/System/Info")

        if not isinstance(result, dict):
            raise EmbyError("Неверный ответ сервера")

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
                # VirtualFolders endpoint does not provide real item counts
                item_count=0,
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

    async def install_update(self) -> None:
        """Install available update (if supported)."""
        info = await self.get_server_info()

        if not info.can_self_update:
            raise EmbyError("Сервер не поддерживает автообновление")

        if not info.has_update_available:
            raise EmbyError("Обновление недоступно")

        await self._request("POST", "/System/Update")
        logger.info("Update installation started")

    async def restart_server(self) -> None:
        """Restart the Emby server."""
        info = await self.get_server_info()

        if not info.can_self_restart:
            raise EmbyError("Сервер не поддерживает перезагрузку")

        await self._request("POST", "/System/Restart")
        logger.info("Server restart initiated")

    async def get_sessions(self) -> list[dict]:
        """Get active sessions (who is watching/playing)."""
        result = await self._request("GET", "/Sessions")
        return result if isinstance(result, list) else []

    async def item_exists(
        self,
        name: str,
        year: Optional[int] = None,
        item_type: str = "Movie",
    ) -> bool:
        """Feature #4: is a title already present in the Emby library?

        Best-effort: any error (or a name match with a mismatched year) returns
        False so this can never block a grab — it is a soft dedup hint only.
        """
        if not name:
            return False
        params = {
            "Recursive": "true",
            "SearchTerm": name,
            "IncludeItemTypes": item_type,
            "Fields": "ProductionYear",
            "Limit": 10,
        }
        try:
            result = await self._request("GET", "/Items", params=params)
        except Exception as e:
            logger.warning("emby_item_exists_failed", name=name, error=str(e))
            return False

        items = result.get("Items", []) if isinstance(result, dict) else []
        if not items:
            return False
        if year is None:
            return True
        for it in items:
            py = it.get("ProductionYear")
            try:
                if py and abs(int(py) - int(year)) <= 1:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    async def find_item_id(self, provider_id: str) -> Optional[str]:
        """E2 (improvements round 2): the Emby item for ``tmdb.<id>`` /
        ``tvdb.<id>``, or None.

        Asks with both spellings of the provider name, then checks the
        returned item's own ``ProviderIds`` — should Emby ignore the filter,
        "the first item of the library" must never become the link. Any error
        reads as "not found" (the button is optional).
        """
        name, _, value = provider_id.partition(".")
        if not name or not value:
            return None
        params = {
            "Recursive": "true",
            "AnyProviderIdEquals": f"{name.lower()}.{value},{name.capitalize()}.{value}",
            "IncludeItemTypes": "Movie,Series",
            "Fields": "ProviderIds",
            "Limit": 5,
        }
        try:
            result = await self._request("GET", "/Items", params=params)
        except Exception as e:
            logger.debug("emby_find_item_failed", provider=provider_id, error=str(e))
            return None
        items = result.get("Items", []) if isinstance(result, dict) else []
        for item in items:
            ids = item.get("ProviderIds") if isinstance(item, dict) else None
            if not isinstance(ids, dict):
                continue
            if any(k.lower() == name.lower() and str(v) == value for k, v in ids.items()):
                item_id = item.get("Id")
                return str(item_id) if item_id else None
        return None

    async def get_server_id(self) -> Optional[str]:
        """The server's id (``/System/Info/Public``), cached once known."""
        if self._server_id:
            return self._server_id
        try:
            result = await self._request("GET", "/System/Info/Public")
        except Exception as e:
            logger.debug("emby_server_id_failed", error=str(e))
            return None
        server_id = result.get("Id") if isinstance(result, dict) else None
        if server_id:
            self._server_id = str(server_id)
            return self._server_id
        return None

    async def watch_url(self, provider_id: str, *, public_base: str) -> Optional[str]:
        """``<public_base>/web/index.html#!/item?id=<item>&serverId=<server>``
        for the item, or None when it isn't in Emby (yet)."""
        item_id = await self.find_item_id(provider_id)
        if item_id is None:
            return None
        server_id = await self.get_server_id()
        if server_id is None:
            return None
        base = public_base.rstrip("/")
        return f"{base}/web/index.html#!/item?id={item_id}&serverId={server_id}"

    async def check_connection(self) -> tuple[bool, Optional[str], Optional[float]]:
        """Check if Emby is available."""
        start_time = time.monotonic()
        try:
            info = await self.get_server_info()
            elapsed = (time.monotonic() - start_time) * 1000
            return True, info.version, round(elapsed, 2)
        except Exception as e:
            elapsed = (time.monotonic() - start_time) * 1000
            logger.warning("health_check_failed", service="Emby", error=str(e))
            return False, None, round(elapsed, 2)
