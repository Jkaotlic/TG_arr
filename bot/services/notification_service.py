"""Notification service for download completion alerts."""

import asyncio
from datetime import datetime
from typing import Optional

import structlog

from bot.clients.qbittorrent import QBittorrentClient
from bot.config import get_settings
from bot.models import TorrentInfo, TorrentState
from bot.ui.formatters import Formatters

logger = structlog.get_logger()


class NotificationService:
    """Service for tracking downloads and sending completion notifications."""

    def __init__(
        self,
        qbittorrent: QBittorrentClient,
        send_notification: callable,
    ):
        """
        Initialize notification service.

        Args:
            qbittorrent: qBittorrent client instance
            send_notification: Async callable(user_id: int, message: str) to send notifications
        """
        self.qbittorrent = qbittorrent
        self.send_notification = send_notification
        self.settings = get_settings()

        # Track known torrents and their completion state
        # Format: {torrent_hash: {"completed": bool, "notified": bool, "name": str}}
        self._tracked_torrents: dict[str, dict] = {}

        # Users subscribed to notifications
        self._subscribed_users: set[int] = set()

        # Background task reference
        self._monitor_task: Optional[asyncio.Task] = None
        self._running = False

    def subscribe_user(self, user_id: int) -> None:
        """Subscribe a user to download notifications."""
        self._subscribed_users.add(user_id)
        logger.info("User subscribed to notifications", user_id=user_id)

    def unsubscribe_user(self, user_id: int) -> None:
        """Unsubscribe a user from download notifications."""
        self._subscribed_users.discard(user_id)
        logger.info("User unsubscribed from notifications", user_id=user_id)

    def get_subscribed_users(self) -> set[int]:
        """Get set of subscribed user IDs."""
        return self._subscribed_users.copy()

    async def start(self) -> None:
        """Start the notification monitoring loop."""
        if self._running:
            logger.warning("Notification service already running")
            return

        if not self.settings.notify_download_complete:
            logger.info("Download notifications disabled in settings")
            return

        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info(
            "Notification service started",
            check_interval=self.settings.notify_check_interval,
        )

    async def stop(self) -> None:
        """Stop the notification monitoring loop."""
        self._running = False

        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None

        logger.info("Notification service stopped")

    async def _monitor_loop(self) -> None:
        """Main monitoring loop that checks for completed downloads."""
        # Initial sync - mark all current torrents as known
        await self._initial_sync()

        while self._running:
            try:
                await asyncio.sleep(self.settings.notify_check_interval)

                if not self._running:
                    break

                await self._check_for_completions()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in notification monitor", error=str(e))
                # Continue monitoring despite errors
                await asyncio.sleep(10)

    async def _initial_sync(self) -> None:
        """Perform initial sync of current torrents."""
        try:
            torrents = await self.qbittorrent.get_torrents()

            for torrent in torrents:
                is_complete = torrent.progress >= 1.0 or torrent.state == TorrentState.COMPLETED
                self._tracked_torrents[torrent.hash] = {
                    "completed": is_complete,
                    "notified": True,  # Don't notify for existing torrents
                    "name": torrent.name,
                    "added_on": torrent.added_on,
                }

            logger.info(
                "Initial torrent sync completed",
                total_torrents=len(torrents),
                completed=sum(1 for t in self._tracked_torrents.values() if t["completed"]),
            )

        except Exception as e:
            logger.error("Failed to perform initial sync", error=str(e))

    async def _check_for_completions(self) -> None:
        """Check for newly completed downloads and send notifications."""
        try:
            torrents = await self.qbittorrent.get_torrents()
            current_hashes = set()

            for torrent in torrents:
                current_hashes.add(torrent.hash)
                is_complete = torrent.progress >= 1.0 or torrent.state == TorrentState.COMPLETED

                if torrent.hash in self._tracked_torrents:
                    tracked = self._tracked_torrents[torrent.hash]

                    # Check if torrent just completed
                    if is_complete and not tracked["completed"]:
                        tracked["completed"] = True

                        # Send notification if not already notified
                        if not tracked["notified"]:
                            await self._notify_completion(torrent)
                            tracked["notified"] = True
                else:
                    # New torrent appeared
                    self._tracked_torrents[torrent.hash] = {
                        "completed": is_complete,
                        "notified": is_complete,  # Only notify if it completes later
                        "name": torrent.name,
                        "added_on": torrent.added_on,
                    }

                    # If it's already complete when first seen (e.g., added as complete)
                    # don't notify - it wasn't downloaded during this session

            # Clean up removed torrents
            removed_hashes = set(self._tracked_torrents.keys()) - current_hashes
            for h in removed_hashes:
                del self._tracked_torrents[h]

        except Exception as e:
            logger.error("Error checking for completions", error=str(e))

    async def _notify_completion(self, torrent: TorrentInfo) -> None:
        """Send completion notification to all subscribed users."""
        if not self._subscribed_users:
            logger.debug("No subscribed users for notification")
            return

        message = Formatters.format_download_complete_notification(torrent)

        for user_id in self._subscribed_users:
            try:
                await self.send_notification(user_id, message)
                logger.info(
                    "Sent completion notification",
                    user_id=user_id,
                    torrent_name=torrent.name,
                )
            except Exception as e:
                logger.error(
                    "Failed to send notification",
                    user_id=user_id,
                    error=str(e),
                )

    async def force_check(self) -> list[TorrentInfo]:
        """
        Force an immediate check for completed downloads.

        Returns:
            List of newly completed torrents.
        """
        newly_completed = []

        try:
            torrents = await self.qbittorrent.get_torrents()

            for torrent in torrents:
                is_complete = torrent.progress >= 1.0 or torrent.state == TorrentState.COMPLETED

                if torrent.hash in self._tracked_torrents:
                    tracked = self._tracked_torrents[torrent.hash]

                    if is_complete and not tracked["completed"]:
                        tracked["completed"] = True
                        tracked["notified"] = True
                        newly_completed.append(torrent)
                else:
                    self._tracked_torrents[torrent.hash] = {
                        "completed": is_complete,
                        "notified": True,
                        "name": torrent.name,
                        "added_on": torrent.added_on,
                    }

        except Exception as e:
            logger.error("Error in force check", error=str(e))

        return newly_completed

    def get_stats(self) -> dict:
        """Get notification service statistics."""
        return {
            "running": self._running,
            "subscribed_users": len(self._subscribed_users),
            "tracked_torrents": len(self._tracked_torrents),
            "completed_torrents": sum(
                1 for t in self._tracked_torrents.values() if t["completed"]
            ),
            "check_interval": self.settings.notify_check_interval,
        }
