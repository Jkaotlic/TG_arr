"""Adding a torrent to TorrServer and publishing it to Emby in one go.

The whole point of the section is "watch it now", so the bot does not leave the
user waiting for the scheduled sync: it adds the torrent, waits until the
server actually knows the file list, and only then asks the hook to write the
`.strm` files and refresh Emby.

Waiting matters. Right after `add` the torrent reports `stat: 1` ("Torrent
getting info") with no files at all; syncing at that moment would publish an
empty release and the next scheduled pass would have to fix it.
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Optional

import structlog

from bot.clients.emby_sync_hook import EmbySyncHookClient
from bot.clients.torrserver import TorrServerClient, TorrServerError
from bot.models import SyncHookResult, TorrServerTorrent

logger = structlog.get_logger()


@dataclass
class AddResult:
    """Everything the answer message needs after an add."""

    torrent: TorrServerTorrent
    metadata_ready: bool
    sync: Optional[SyncHookResult] = None
    stream_url: Optional[str] = None


class TorrServerService:
    """Orchestrates add → wait for metadata → forced Emby sync."""

    def __init__(
        self,
        client: TorrServerClient,
        hook: Optional[EmbySyncHookClient] = None,
        metadata_timeout: float = 30.0,
        poll_interval: float = 2.0,
    ):
        self.client = client
        self.hook = hook
        self.metadata_timeout = metadata_timeout
        self.poll_interval = poll_interval

    async def _wait_for_files(self, torrent_hash: str) -> Optional[TorrServerTorrent]:
        """Poll until the torrent reports its files, or the budget runs out.

        The polling window is exactly when TorrServer is busiest fetching
        metadata, so a slow or 500 answer on any single poll is expected, not
        exceptional. The torrent is already on the server by this point, so a
        per-poll failure must not escape and turn a successful add into a
        reported failure — it just costs one wasted poll.
        """
        deadline = time.monotonic() + self.metadata_timeout
        while True:
            try:
                torrent = await self.client.get_torrent(torrent_hash)
            except TorrServerError as e:
                logger.warning("torrserver_poll_failed", torrent_hash=torrent_hash, error=str(e))
                torrent = None
            if torrent and torrent.files:
                return torrent
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(self.poll_interval)

    async def add_and_publish(self, link: str, title: str, poster: str = "") -> AddResult:
        """Add a release and make it visible in Emby as soon as possible."""
        added = await self.client.add_torrent(link, title, poster)
        logger.info("torrserver_add", torrent_hash=added.hash, title=added.title)

        ready = await self._wait_for_files(added.hash)
        if ready is None:
            logger.warning("torrserver_metadata_timeout", torrent_hash=added.hash)
            return AddResult(torrent=added, metadata_ready=False)

        stream_url = None
        videos = ready.video_files
        if videos:
            stream_url = self.client.stream_url(ready.hash, videos[0].id, videos[0].path)

        sync = await self.hook.trigger_sync() if self.hook else None
        return AddResult(
            torrent=ready, metadata_ready=True, sync=sync, stream_url=stream_url,
        )
