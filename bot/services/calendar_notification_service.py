"""Calendar notification service for upcoming release alerts."""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

import structlog

from bot.clients.registry import get_radarr, get_sonarr
from bot.config import get_settings
from bot.db import Database
from bot.models import CalendarEvent, CalendarEventType, ContentType
from bot.ui.formatters import Formatters

logger = structlog.get_logger()


class CalendarNotificationService:
    """Service for sending notifications about upcoming releases."""

    # Check every hour
    CHECK_INTERVAL = 3600

    def __init__(self, send_notification: callable):
        """
        Initialize calendar notification service.

        Args:
            send_notification: Async callable(user_id: int, message: str) to send notifications
        """
        self.send_notification = send_notification
        self.settings = get_settings()

        # Background task reference
        self._monitor_task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self) -> None:
        """Start the notification monitoring loop."""
        if self._running:
            logger.warning("Calendar notification service already running")
            return

        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info(
            "Calendar notification service started",
            check_interval=self.CHECK_INTERVAL,
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

        logger.info("Calendar notification service stopped")

    async def _monitor_loop(self) -> None:
        """Main monitoring loop that checks for upcoming releases."""
        # Wait a bit before first check
        await asyncio.sleep(30)

        while self._running:
            try:
                await self._check_and_notify()
                await asyncio.sleep(self.CHECK_INTERVAL)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in calendar notification monitor", error=str(e))
                await asyncio.sleep(60)

    async def _check_and_notify(self) -> None:
        """Check for upcoming releases and send notifications."""
        db = Database(self.settings.database_path)
        await db.connect()

        try:
            # Get all subscriptions
            subscriptions = await db.get_all_calendar_subscriptions()

            if not subscriptions:
                return

            # Fetch upcoming events
            events = await self._fetch_upcoming_events()

            if not events:
                return

            for sub in subscriptions:
                if not sub.enabled:
                    continue

                notify_date = datetime.now(timezone.utc).date() + timedelta(days=sub.notify_days_before)

                for event in events:
                    # Check if event matches subscription content type
                    if sub.content_type:
                        if sub.content_type == ContentType.MOVIE and event.event_type != CalendarEventType.MOVIE:
                            continue
                        if sub.content_type == ContentType.SERIES and event.event_type != CalendarEventType.EPISODE:
                            continue

                    # Check if release is on the notify date
                    event_date = event.release_date.date()
                    if event_date != notify_date:
                        continue

                    # Check if we already notified about this release
                    if await db.is_release_notified(sub.user_id, event.event_type.value, event.content_id):
                        continue

                    # Send notification
                    await self._send_release_notification(sub.user_id, event, db)

            # Cleanup old notifications (older than 30 days)
            await db.cleanup_old_notifications(30)

        finally:
            await db.close()

    async def _fetch_upcoming_events(self) -> list[CalendarEvent]:
        """Fetch upcoming calendar events from Radarr and Sonarr."""
        events: list[CalendarEvent] = []

        start_date = datetime.now(timezone.utc)
        end_date = start_date + timedelta(days=7)

        # Fetch movies from Radarr
        if self.settings.radarr_enabled:
            radarr = get_radarr()
            if radarr:
                try:
                    movies = await radarr.get_calendar(
                        start_date=start_date,
                        end_date=end_date,
                    )
                    for movie in movies:
                        release_date = None
                        for date_field in ["digitalRelease", "physicalRelease", "inCinemas"]:
                            if movie.get(date_field):
                                try:
                                    release_date = datetime.fromisoformat(
                                        movie[date_field].replace("Z", "+00:00")
                                    )
                                    break
                                except (ValueError, AttributeError):
                                    continue

                        if not release_date:
                            continue

                        events.append(CalendarEvent(
                            event_type=CalendarEventType.MOVIE,
                            title=movie.get("title", "Unknown"),
                            release_date=release_date,
                            overview=movie.get("overview"),
                            tmdb_id=movie.get("tmdbId"),
                            radarr_id=movie.get("id"),
                            year=movie.get("year"),
                            has_file=movie.get("hasFile", False),
                            is_available=movie.get("isAvailable", False),
                        ))
                except Exception as e:
                    logger.warning("Failed to fetch Radarr calendar for notifications", error=str(e))

        # Fetch episodes from Sonarr
        if self.settings.sonarr_enabled:
            sonarr = get_sonarr()
            if sonarr:
                try:
                    episodes = await sonarr.get_calendar(
                        start_date=start_date,
                        end_date=end_date,
                        include_series=True,
                    )
                    for ep in episodes:
                        air_date_str = ep.get("airDateUtc") or ep.get("airDate")
                        if not air_date_str:
                            continue

                        try:
                            release_date = datetime.fromisoformat(
                                air_date_str.replace("Z", "+00:00")
                            )
                        except (ValueError, AttributeError):
                            continue

                        series = ep.get("series", {})

                        events.append(CalendarEvent(
                            event_type=CalendarEventType.EPISODE,
                            title=series.get("title", ep.get("title", "Unknown")),
                            release_date=release_date,
                            overview=ep.get("overview"),
                            tvdb_id=series.get("tvdbId"),
                            sonarr_id=ep.get("id"),
                            series_id=ep.get("seriesId"),
                            series_title=series.get("title"),
                            season_number=ep.get("seasonNumber", 0),
                            episode_number=ep.get("episodeNumber", 0),
                            episode_title=ep.get("title"),
                        ))
                except Exception as e:
                    logger.warning("Failed to fetch Sonarr calendar for notifications", error=str(e))

        return events

    async def _send_release_notification(
        self,
        user_id: int,
        event: CalendarEvent,
        db: Database,
    ) -> None:
        """Send notification about upcoming release."""
        try:
            message = Formatters.format_release_notification(event)
            await self.send_notification(user_id, message)

            # Mark as notified
            await db.mark_release_notified(user_id, event.event_type.value, event.content_id, event.release_date)

            logger.info(
                "Sent calendar notification",
                user_id=user_id,
                content_id=event.content_id,
                title=event.display_title,
            )
        except Exception as e:
            logger.error(
                "Failed to send calendar notification",
                user_id=user_id,
                error=str(e),
            )

    def get_stats(self) -> dict:
        """Get notification service statistics."""
        return {
            "running": self._running,
            "check_interval": self.CHECK_INTERVAL,
        }
