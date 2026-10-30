"""Notification service for download completion alerts."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import structlog

from bot.clients.qbittorrent import DIRECT_TAG, STATE_MAP, QBittorrentClient
from bot.config import get_settings
from bot.logging_utils import describe_error, log_background_task_end
from bot.models import TorrentInfo, TorrentState
from bot.ui.formatters import Formatters

logger = structlog.get_logger()

#: Часы qBittorrent и бота могут расходиться: допуск к «завершён после прошлого
#: опроса». Повтора он не даст: завершённые хэши помнит `_completed_hashes`.
_CLOCK_SKEW_MARGIN = timedelta(seconds=120)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class NotificationService:
    """Service for tracking downloads and sending completion notifications."""

    # PERF-02: idle interval used once no torrent is actively downloading —
    # cuts the 1440 req/day baseline poll load when the queue is empty.
    _idle_check_interval = 300

    def __init__(
        self,
        qbittorrent: QBittorrentClient,
        send_notification: Callable[[int, str], Awaitable[bool] | Awaitable[None]],
        exclude_chats: Optional[Callable[[str], Awaitable[set[int]]]] = None,
        claim_direct_chats: Optional[Callable[[TorrentInfo], Awaitable[set[int]]]] = None,
        clock: Callable[[], datetime] = _utcnow,
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
        #: E1 (improvements round 2): chats whose living grab message already
        #: follows this torrent (``LiveStatusService.owner_chats``) — they
        #: get no separate "download complete" message.
        self._exclude_chats = exclude_chats
        #: LOGIC-09: живое сообщение, ещё ищущее свой прямой торрент, забирает его
        #: (``LiveStatusService.claim_direct_chats``). Магнет, докачанный до привязки,
        #: не должен дать хозяину второе сообщение.
        self._claim_direct_chats = claim_direct_chats
        self._clock = clock
        self.settings = get_settings()

        # Track known torrents and their completion state
        # Format: {torrent_hash: {"completed": bool, "notified": bool, "name": str}}
        self._tracked_torrents: dict[str, dict] = {}

        # PERF-02 full: local mirror of qBittorrent's torrent table, fed by
        # the sync/maindata delta protocol. Each entry is the *raw* API dict
        # for that hash (same shape _parse_torrent expects), kept up to date
        # by merging partial per-field updates on top of it — see
        # ``_merge_maindata``. ``_rid`` is the response-id qBittorrent uses to
        # compute the next delta; 0 forces a full snapshot (first poll, or
        # after a full_update resync).
        self._torrents_raw: dict[str, dict[str, Any]] = {}
        self._rid = 0

        # LOGIC-09: время прошлого успешного чтения qBit (None до первого) и хэши,
        # уже виденные завершёнными. Новый завершённый торрент объявляется, только
        # если докачан после прошлого опроса; при первом чтении не объявляется
        # ничего, иначе каждый рестарт рассылал бы всю раздачу.
        self._last_sync_at: Optional[datetime] = None
        self._completed_hashes: set[str] = set()

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
        self._monitor_task = asyncio.create_task(self._monitor_loop(), name="notification_monitor")
        self._monitor_task.add_done_callback(log_background_task_end)
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
                            error=describe_error(e),
                            consecutive_failures=consecutive_failures,
                            exc_info=True,
                        )
                    else:
                        logger.debug(
                            "notification_monitor_failed",
                            error=describe_error(e),
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

        Uses the local maindata-fed view (``_torrents_raw``) instead of an
        extra API call — the view is already kept current by
        ``_check_for_completions`` every cycle, so no network round-trip is
        needed just to decide the next sleep duration.
        """
        active = any(
            STATE_MAP.get(raw.get("state", "unknown"), TorrentState.UNKNOWN)
            == TorrentState.DOWNLOADING
            for raw in self._torrents_raw.values()
        )
        return self.settings.notify_check_interval if active else self._idle_check_interval

    def _merge_maindata(self, maindata: dict) -> None:
        """Merge a sync/maindata response into the local torrent-table mirror.

        PERF-02 full: qBittorrent's delta protocol sends only the fields that
        changed since ``rid`` for each torrent (not a full row), so a naive
        replace would silently drop every field the server didn't repeat this
        cycle. Instead each hash's partial dict is merged field-by-field on
        top of whatever we already know about that hash. ``full_update=True``
        (first poll, or after the server drops our rid, e.g. a WebUI restart)
        means ``torrents`` is a complete snapshot — the mirror is replaced
        outright rather than merged, so stale fields from a previous torrent
        that reused a hash can't linger.
        """
        if maindata.get("full_update"):
            self._torrents_raw = {
                h: dict(raw) for h, raw in maindata.get("torrents", {}).items()
            }
        else:
            for h, partial in maindata.get("torrents", {}).items():
                existing = self._torrents_raw.setdefault(h, {})
                existing.update(partial)

        for h in maindata.get("torrents_removed", []):
            self._torrents_raw.pop(h, None)

        self._rid = maindata.get("rid", self._rid)

    def _torrent_view(self, torrent_hash: str) -> Optional[TorrentInfo]:
        """Parse the merged raw dict for one hash into a ``TorrentInfo``.

        Calls ``QBittorrentClient._parse_torrent`` on the class rather than
        ``self.qbittorrent._parse_torrent`` — the latter would resolve
        through the (normally mocked, in tests) client instance, which for
        an ``AsyncMock`` turns a plain sync helper into an unawaited
        coroutine. ``_parse_torrent`` is a ``@staticmethod`` that only
        depends on its ``item`` argument, so calling it via the class is
        equivalent and mock-safe.
        """
        raw = self._torrents_raw.get(torrent_hash)
        if raw is None:
            return None
        # Merged partial updates may still lack "hash"/"name" if the first
        # delta this process ever saw for that hash arrived before a full
        # snapshot (shouldn't happen with rid=0 on the first poll, but stay
        # defensive rather than let _parse_torrent blow up on a KeyError).
        raw = {"hash": torrent_hash, **raw}
        return QBittorrentClient._parse_torrent(raw)

    async def _initial_sync(self) -> None:
        """Perform initial sync of current torrents.

        PERF-02 full: forces a full maindata snapshot (``rid=0``) rather than
        ``get_torrents()`` — same information, fetched via the delta protocol
        so the local mirror (``_torrents_raw``) and ``_rid`` are primed for
        ``_check_for_completions`` to take over with incremental polls.
        """
        try:
            maindata = await self.qbittorrent.get_maindata(0)
            self._torrents_raw = {}
            self._merge_maindata(maindata)

            completed = 0
            for torrent_hash in self._torrents_raw:
                torrent = self._torrent_view(torrent_hash)
                if torrent is None:
                    continue
                is_complete = torrent.progress >= 1.0 or torrent.state == TorrentState.COMPLETED
                if is_complete:
                    # Nothing left to watch — not tracked at all (NOTIF-10).
                    completed += 1
                    self._completed_hashes.add(torrent_hash)
                    continue
                # LOGIC-09: качался во время рестарта или деплоя. Он докачается уже
                # при нас и получит своё уведомление, как любой другой.
                self._tracked_torrents[torrent.hash] = {
                    "completed": False,
                    "notified": False,
                    "name": torrent.name,
                    "added_on": torrent.added_on,
                }
            self._last_sync_at = self._clock()

            logger.info(
                "Initial torrent sync completed",
                total_torrents=len(self._torrents_raw),
                completed=completed,
            )

        except Exception as e:
            logger.error("Failed to perform initial sync", error=describe_error(e), exc_info=True)

    async def _check_for_completions(self) -> None:
        """Check for newly completed downloads and send notifications.

        PERF-02 full: polls qBittorrent's ``sync/maindata`` delta protocol
        (``rid``-based) instead of the DOWNLOADING filter. The response is
        merged into the local mirror (``_torrents_raw``); completion is then
        determined the same way as before — progress >= 1.0 or
        state == COMPLETED — just read from the local view instead of a
        fresh per-torrent fetch. Torrents reported in ``torrents_removed``
        are dropped from tracking (deleted/moved out of qBittorrent).

        Only hashes that actually changed this cycle (``maindata["torrents"]``
        keys — or every hash on a ``full_update`` resync) are re-parsed and
        re-evaluated; an idle cycle with an empty delta does no per-torrent
        work at all, which is the whole point of the delta protocol on a Pi.
        """
        maindata = await self.qbittorrent.get_maindata(self._rid)
        seen_before = set(self._torrents_raw)
        self._merge_maindata(maindata)
        previous_sync, self._last_sync_at = self._last_sync_at, self._clock()
        resync = bool(maindata.get("full_update"))

        for h in maindata.get("torrents_removed", []):
            self._tracked_torrents.pop(h, None)
            self._completed_hashes.discard(h)

        if resync:
            changed_hashes = list(self._torrents_raw)
        else:
            changed_hashes = list(maindata.get("torrents", {}))

        for torrent_hash in changed_hashes:
            torrent = self._torrent_view(torrent_hash)
            if torrent is None:
                continue

            is_complete = torrent.progress >= 1.0 or torrent.state == TorrentState.COMPLETED
            tracked = self._tracked_torrents.get(torrent_hash)

            if tracked is None:
                # New torrent observed mid-run. Already complete the moment we
                # see it: it finished between two polls (LOGIC-09, the idle
                # interval is 5 minutes) or is old news — `completion_on` tells
                # which. Either way it is not tracked: a seeding torrent shows up
                # in every delta, and keeping it would grow the dict for nothing
                # (audit 2026-09-23, NOTIF-10 "prune completed entries").
                if not is_complete:
                    self._completed_hashes.discard(torrent_hash)
                    self._tracked_torrents[torrent_hash] = {
                        "completed": False,
                        "notified": False,
                        "name": torrent.name,
                        "added_on": torrent.added_on,
                    }
                elif torrent_hash not in self._completed_hashes:
                    self._completed_hashes.add(torrent_hash)
                    if self._finished_since(
                        torrent, previous_sync, new=not resync and torrent_hash not in seen_before,
                    ):
                        await self._completion_reached(torrent)
                continue

            if tracked["completed"]:
                # Already resolved (shouldn't normally still be tracked —
                # see the drop below — but guard against a stray entry).
                continue

            if is_complete:
                self._completed_hashes.add(torrent_hash)
                if not tracked["notified"]:
                    await self._completion_reached(torrent)
                # Once complete+notified there is no further state to
                # track — drop it now instead of keeping a growing dict
                # of finished torrents alive for the process lifetime.
                del self._tracked_torrents[torrent_hash]
            # else: still downloading/paused/stalled/errored — leave
            # tracked, re-check next cycle.

    @staticmethod
    def _finished_since(torrent: TorrentInfo, previous_sync: Optional[datetime], *, new: bool) -> bool:
        """Докачан после прошлого опроса. До первого чтения qBit — никогда.

        ``new``: дельта принесла хэш, которого не было в прошлом чтении. Он новый по
        определению, и часы qBit, отстающие сколько угодно, этого не отменяют. При
        полном чтении (resync) такого знания нет: там решает время, иначе вся раздача
        ушла бы рассылкой.
        """
        if previous_sync is None or torrent.completion_on is None:
            return False
        return new or torrent.completion_on > previous_sync - _CLOCK_SKEW_MARGIN

    async def _completion_reached(self, torrent: TorrentInfo) -> None:
        if (
            (torrent.category or "") in self.settings.arr_torrent_category_set
            # Final review 2026-09-24: the bot pushed this one
            # straight into qBit — no *arr grabbed it, so
            # library_watch never will announce it.
            and DIRECT_TAG not in torrent.tags
        ):
            # NOTIF-07 (audit 2026-09-23): Radarr/Sonarr
            # import this torrent and library_watch announces
            # it once it is in the library — a qBit message
            # here would be the second one for one film.
            logger.debug(
                "qbit_completion_left_to_library_watch",
                torrent_name=torrent.name,
                category=torrent.category,
            )
        else:
            await self._notify_completion(torrent)
        self._completed_count += 1

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

        skip: set[int] = set()
        if self._exclude_chats is not None:
            try:
                skip = set(await self._exclude_chats(torrent.hash))
                if self._claim_direct_chats is not None and DIRECT_TAG in torrent.tags:
                    skip |= await self._claim_direct_chats(torrent)
            except Exception as e:
                logger.warning("notification_exclude_failed", error=describe_error(e))

        for user_id in self.get_subscribed_users():
            if user_id in skip:
                logger.debug("completion_left_to_living_message", user_id=user_id)
                continue
            try:
                result = await self.send_notification(user_id, message)
            except Exception as e:
                logger.error(
                    "notification_send_raised",
                    user_id=user_id,
                    torrent_name=torrent.name,
                    error=describe_error(e),
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
