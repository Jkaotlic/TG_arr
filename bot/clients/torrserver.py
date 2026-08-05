"""TorrServer API client — the streaming contour ("watch now").

TorrServer speaks a small POST-with-an-`action`-field API rather than REST, and
authenticates with Basic auth instead of the `X-Api-Key` every other backend
here uses. Everything else (retries, pooling, slow-call logging, the TTL cache)
comes from BaseAPIClient unchanged.

All contracts below were taken from the live server on 2026-08-05, including a
probe torrent that was added and removed again — see the spec for the raw
responses.
"""

import base64
import json
import time
from typing import Any, Optional

import structlog

from bot.clients.base import APIError, AuthenticationError, BaseAPIClient, ServiceConnectionError
from bot.models import (
    TorrServerFile,
    TorrServerStats,
    TorrServerTorrent,
    parse_torrserver_size,
)

logger = structlog.get_logger()


class TorrServerError(Exception):
    """TorrServer API error, already phrased for the user."""


class TorrServerClient(BaseAPIClient):
    """Client for the TorrServer HTTP API."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        timeout: float = 30.0,
        search_timeout: float = 60.0,
    ):
        # BaseAPIClient's api_key is the X-Api-Key value, which TorrServer has
        # no concept of — the credentials live in the Authorization header
        # built by _get_headers() below.
        super().__init__(base_url, api_key="", service_name="TorrServer")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.search_timeout = search_timeout

    def _get_headers(self) -> dict[str, str]:
        token = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
        return {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "TG_arr-bot/1.0",
        }

    async def _torrents(self, payload: dict[str, Any]) -> Any:
        """POST /torrents, translating transport/auth errors into TorrServerError."""
        try:
            return await self.post("/torrents", json_data=payload, timeout=self.timeout)
        except AuthenticationError as e:
            raise TorrServerError("Неверный логин или пароль TorrServer") from e
        except ServiceConnectionError as e:
            raise TorrServerError("TorrServer недоступен") from e
        except APIError as e:
            raise TorrServerError(f"Ошибка TorrServer: {e.message}") from e

    @staticmethod
    def _files_from_payload(item: dict[str, Any]) -> list[TorrServerFile]:
        """File list of a torrent.

        `file_stats` is populated only while a torrent is active; for the rest
        the composition lives in the `data` blob as a JSON *string*. A release
        whose blob doesn't parse still belongs in the list — it just has no
        known files.
        """
        raw_files = item.get("file_stats")
        if not raw_files:
            blob = item.get("data") or ""
            try:
                raw_files = (json.loads(blob).get("TorrServer") or {}).get("Files") or []
            except (ValueError, TypeError, AttributeError):
                logger.debug("torrserver_unparseable_data", torrent_hash=item.get("hash"))
                raw_files = []

        files = []
        for entry in raw_files:
            try:
                files.append(TorrServerFile(
                    id=int(entry["id"]),
                    path=str(entry.get("path", "")),
                    length=int(entry.get("length", 0)),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        return files

    @classmethod
    def _to_torrent(cls, item: dict[str, Any]) -> TorrServerTorrent:
        return TorrServerTorrent(
            hash=str(item.get("hash", "")),
            title=str(item.get("title") or item.get("name") or "Без названия"),
            category=str(item.get("category") or ""),
            poster=str(item.get("poster") or ""),
            size=parse_torrserver_size(item.get("torrent_size")),
            stat=int(item.get("stat") or 0),
            stat_string=str(item.get("stat_string") or ""),
            files=cls._files_from_payload(item),
        )

    async def get_version(self) -> str:
        """Server version from /echo (the one endpoint open without auth)."""
        client = await self._get_client()
        try:
            response = await client.get("/echo", timeout=self.timeout)
        except Exception as e:
            raise TorrServerError("TorrServer недоступен") from e
        if response.status_code >= 400:
            raise TorrServerError(f"TorrServer вернул {response.status_code}")
        return (response.text or "").strip() or "unknown"

    async def list_torrents(self) -> list[TorrServerTorrent]:
        """All torrents known to the server."""
        result = await self._torrents({"action": "list"})
        if not isinstance(result, list):
            return []
        return [self._to_torrent(item) for item in result if isinstance(item, dict)]

    async def get_torrent(self, torrent_hash: str) -> Optional[TorrServerTorrent]:
        """One torrent by hash, or None if the server doesn't know it."""
        result = await self._torrents({"action": "get", "hash": torrent_hash})
        if not isinstance(result, dict) or not result.get("hash"):
            return None
        return self._to_torrent(result)

    async def get_server_settings(self) -> dict[str, Any]:
        """Raw settings object (cache size, torznab sources, ...)."""
        try:
            result = await self.post("/settings", json_data={"action": "get"}, timeout=self.timeout)
        except AuthenticationError as e:
            raise TorrServerError("Неверный логин или пароль TorrServer") from e
        except (ServiceConnectionError, APIError) as e:
            raise TorrServerError("TorrServer недоступен") from e
        return result if isinstance(result, dict) else {}

    async def get_stats(self) -> TorrServerStats:
        """Everything the status card shows, in one call site."""
        version = await self.get_version()
        settings = await self.get_server_settings()
        torrents = await self.list_torrents()
        return TorrServerStats(
            version=version,
            torrent_count=len(torrents),
            total_size=sum(t.size for t in torrents),
            cache_size=int(settings.get("CacheSize") or 0),
            use_disk=bool(settings.get("UseDisk")),
            source_count=len(settings.get("TorznabUrls") or []),
        )

    async def check_connection(self) -> tuple[bool, Optional[str], Optional[float]]:
        """Health probe for /status and the health monitor."""
        start_time = time.monotonic()
        try:
            version = await self.get_version()
            return True, version, round((time.monotonic() - start_time) * 1000, 2)
        except Exception as e:
            logger.warning("health_check_failed", service="TorrServer", error=str(e))
            return False, None, round((time.monotonic() - start_time) * 1000, 2)
