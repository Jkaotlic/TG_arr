"""Client for the forced Emby sync hook running next to TorrServer.

The bot's container has neither ssh nor curl, so the `.strm` sync on Homeserver
is reached over plain HTTP. Every failure mode here is a *degradation*: the
torrent has already been added, and the scheduled `TorrServer-EmbySync` task
will publish it within ten minutes anyway. That is why nothing in this module
raises.
"""

from typing import Optional

import httpx
import structlog

from bot.models import SyncHookResult

logger = structlog.get_logger()


class EmbySyncHookClient:
    """Triggers `Sync-TorrServerToEmby.py --apply` on Homeserver."""

    def __init__(self, base_url: str, token: str, timeout: float = 90.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout),
                limits=httpx.Limits(max_keepalive_connections=1, max_connections=2),
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def trigger_sync(self) -> SyncHookResult:
        """Ask the hook to publish `.strm` files and refresh Emby now."""
        try:
            client = await self._get_client()
            response = await client.post("/sync", headers={"X-Token": self.token})
        except httpx.InvalidURL as e:
            # Raised by the httpx.AsyncClient constructor itself when base_url
            # is malformed (e.g. a bad port from an operator typo in
            # EMBY_SYNC_HOOK_URL) — httpx.InvalidURL is a plain Exception, not
            # an httpx.HTTPError, so it needs its own clause; construction was
            # therefore folded into this try instead of running unguarded.
            logger.warning("emby_sync_hook", status="bad_url", error=str(e))
            return SyncHookResult(status="failed", error="некорректный адрес хука синхронизации")
        except httpx.TimeoutException:
            logger.warning("emby_sync_hook", status="timeout")
            return SyncHookResult(status="failed", error="таймаут хука синхронизации")
        except httpx.HTTPError as e:
            logger.warning("emby_sync_hook", status="unreachable", error=str(e))
            return SyncHookResult(status="failed", error="хук синхронизации недоступен")

        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        if response.status_code == 202:
            logger.info("emby_sync_hook", status="already_running")
            return SyncHookResult(status="already_running")

        if response.status_code >= 400:
            logger.warning("emby_sync_hook", status="rejected", status_code=response.status_code)
            return SyncHookResult(
                status="failed", error=f"хук ответил {response.status_code}",
            )

        duration = payload.get("duration_s")
        logger.info("emby_sync_hook", status="ok", duration_s=duration)
        return SyncHookResult(
            status="ok",
            duration_s=float(duration) if isinstance(duration, (int, float)) else None,
        )
