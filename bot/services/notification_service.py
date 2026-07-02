"""Notification service for download completion alerts."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Optional

import structlog

from bot.clients.qbittorrent import QBittorrentClient
from bot.config import get_settings
from bot.models import TorrentFilter, TorrentInfo, TorrentState
from bot.ui.formatters import Formatters

logger = structlog.get_logger()


class NotificationService:
    """Service for tracking downloads and sending completion notifications."""

    # PERF-02: idle interval used once no torrent is actively downloading —
    # cuts the 1440 req/day baseline poll load when the queue is empty.
    _idle_check_interval = 300

    def __init__(
        self,
        qbittorrent: QBittorrentClient,
        send_notification: Callable[[int, str], Awaitable[bool] | Awaitable[None]],
    ):
        """
        Initialize notification service.

        Args:
            qbittorrent: qBittorrent client instance
            send_notification: Async callable(user_id: int, message: str) to send
                notifications. OBS-03: should return True on confirmed delivery
                and False on failure (swallowing its own exceptions) so this
                service never logs a false "sent" for a message that never
                arrived. A callable returning None (legacy) is treated as
                success for backward compatibility.
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

        # PERF-02: completed+notified torrents are dropped from
        # _tracked_torrents immediately (no further state to track) rather
        # than kept forever, so get_stats()/OBS-09 needs a separate running
        # counter to report a meaningful "completed this run" figure.
        self._completed_count = 0

    def subscribe_user(self, user_id: int) -> None:
        """Subscribe a user to download notifications."""
        self._subscribed_users.add(user_id)
        logger.info("User subscribed to notifications", user_id=user_id)

    def unsubscribe_user(self, user_id: int) -> None:
        """Unsubscribe a user from download notifications."""
        self._subscribed_users.discard(user_id)
        logger.info("User unsubscribed from notifications", user_id=user_id)

    def get_subscribed_users(self) -> frozenset[int]:
        """Get snapshot of subscribed user IDs (safe for iteration)."""
        return frozenset(self._subscribed_users)

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
        # OBS-06: tag every log line from this background task so it's
        # distinguishable from request-scoped (contextvars-bound) logs.
        structlog.contextvars.bind_contextvars(component="notification_service")
        consecutive_failures = 0
        try:
            # Initial sync - mark all current torrents as known
            await self._initial_sync()

            while self._running:
                try:
                    await self._check_for_completions()

                    if consecutive_failures:
                        logger.info("notification_monitor_recovered", after_failures=consecutive_failures)
                        consecutive_failures = 0

                    interval = await self._get_poll_interval()
                    await asyncio.sleep(interval)

                    if not self._running:
                        break

                except asyncio.CancelledError:
                    break
                except Exception as e:
                    consecutive_failures += 1
                    # OBS-06: avoid ERROR-spam every 10s on a sustained qBit
                    # outage — loud on the first failure and every 10th,
                    # DEBUG in between; always log the eventual recovery.
                    if consecutive_failures == 1 or consecutive_failures % 10 == 0:
                        logger.error(
                            "notification_monitor_failed",
                            error=str(e),
                            consecutive_failures=consecutive_failures,
                            exc_info=True,
                        )
                    else:
                        logger.debug(
                            "notification_monitor_failed",
                            error=str(e),
                            consecutive_failures=consecutive_failures,
                        )
                    # Continue monitoring despite errors
                    await asyncio.sleep(10)
        finally:
            structlog.contextvars.unbind_contextvars("component")

    async def _get_poll_interval(self) -> int:
        """PERF-02: poll faster while something is actively downloading,
        back off to `_idle_check_interval` when the queue is empty — avoids
        polling qBittorrent every notify_check_interval (default 60s) around
        the clock when nothing is happening.
        """
        try:
            active = await self.qbittorrent.get_torrents(filter_type=TorrentFilter.DOWNLOADING)
        except Exception:
            return self.settings.notify_check_interval
        return self.settings.notify_check_interval if active else self._idle_check_interval

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
            logger.error("Failed to perform initial sync", error=str(e), exc_info=True)

    async def _check_for_completions(self) -> None:
        """Check for newly completed downloads and send notifications.

        PERF-02 (minimal): instead of pulling and parsing the *entire*
        torrent list every cycle (most of which is idle seeding torrents on
        a Pi), only the DOWNLOADING filter is polled. A previously-tracked,
        not-yet-completed torrent that no longer shows up as downloading is
        looked up individually via ``get_torrent(hash)`` — either it just
        completed (notify), it's in some other non-downloading state like
        paused/stalled (update, don't notify), or it's gone entirely (drop
        from tracking). Full sync/maindata delta-protocol is deferred (see
        fix-plan Refactoring section) — this is the minimal fix.
        """
        try:
            downloading = await self.qbittorrent.get_torrents(filter_type=TorrentFilter.DOWNLOADING)
            downloading_hashes = {t.hash for t in downloading}

            for torrent in downloading:
                if torrent.hash not in self._tracked_torrents:
                    # New torrent appeared while downloading — track it,
                    # don't notify (it wasn't observed completing).
                    self._tracked_torrents[torrent.hash] = {
                        "completed": False,
                        "notified": False,
                        "name": torrent.name,
                        "added_on": torrent.added_on,
                    }

            # Torrents we were tracking as "not yet complete" that dropped out
            # of the DOWNLOADING filter: resolve what happened to each.
            pending_hashes = {
                h for h, t in self._tracked_torrents.items()
                if not t["completed"] and h not in downloading_hashes
            }
            for h in pending_hashes:
                tracked = self._tracked_torrents[h]
                try:
                    torrent = await self.qbittorrent.get_torrent(h)
                except Exception as e:
                    logger.debug("completion_recheck_failed", torrent_hash=h, error=str(e))
                    continue

                if torrent is None:
                    # Removed from qBittorrent entirely (deleted/moved out).
                    del self._tracked_torrents[h]
                    continue

                is_complete = torrent.progress >= 1.0 or torrent.state == TorrentState.COMPLETED
                if is_complete:
                    if not tracked["notified"]:
                        await self._notify_completion(torrent)
                        self._completed_count += 1
                    # Once complete+notified there is no further state to
                    # track (it won't re-enter the DOWNLOADING filter) — drop
                    # it now instead of keeping a growing dict of finished
                    # torrents alive for the lifetime of the process.
                    del self._tracked_torrents[h]
                # else: paused/stalled/errored etc — leave tracked, re-check
                # next cycle since it's still not in the downloading filter.

        except Exception as e:
            logger.error("notification_check_failed", error=str(e), exc_info=True)

    async def _notify_completion(self, torrent: TorrentInfo) -> None:
        """Send completion notification to all subscribed users.

        OBS-03: ``send_notification`` is expected to swallow its own
        delivery errors and report success via its return value, so a
        per-user failure here is only ever a bug in that contract — not the
        expected path. We still guard it so one bad callable can't wedge
        the loop, but the INFO "sent" log now only fires on a confirmed True.
        """
        if not self._subscribed_users:
            logger.debug("No subscribed users for notification")
            return

        message = Formatters.format_download_complete_notification(torrent)

        for user_id in self.get_subscribed_users():
            try:
                result = await self.send_notification(user_id, message)
            except Exception as e:
                logger.error(
                    "notification_send_raised",
                    user_id=user_id,
                    torrent_name=torrent.name,
                    error=str(e),
                    exc_info=True,
                )
                continue

            # Legacy callables return None; treat that as success for
            # backward compatibility. A callable that explicitly returns
            # False means "did not deliver" and must not be logged as sent.
            sent = result is not False
            if sent:
                logger.info(
                    "Sent completion notification",
                    user_id=user_id,
                    torrent_name=torrent.name,
                )
            else:
                logger.warning(
                    "notification_not_delivered",
                    user_id=user_id,
                    torrent_name=torrent.name,
                )

    def get_stats(self) -> dict:
        """Get notification service statistics."""
        return {
            "running": self._running,
            "subscribed_users": len(self._subscribed_users),
            "tracked_torrents": len(self._tracked_torrents),
            "completed_torrents": self._completed_count,
            "check_interval": self.settings.notify_check_interval,
        }
