"""Main entry point for the TG_arr Telegram bot."""

import asyncio
import faulthandler
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import structlog


# SEC-03: mask Telegram bot tokens in any log event value
_TOKEN_PATTERN = re.compile(r'bot\d+:[A-Za-z0-9_-]{30,}')


def _mask_tokens(logger, method_name, event_dict):
    """Structlog processor that redacts bot tokens from log values."""
    for k, v in list(event_dict.items()):
        if isinstance(v, str):
            event_dict[k] = _TOKEN_PATTERN.sub('bot***:***', v)
    return event_dict
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bot.clients.qbittorrent import QBittorrentClient
from bot.clients.registry import close_all as close_all_clients
from bot.config import get_settings
from bot.db import Database
from bot.handlers import setup_routers
from bot.middleware.auth import AuthMiddleware, LoggingMiddleware, RateLimitMiddleware
from bot.services.notification_service import NotificationService


def _liveness_watchdog(
    alive_path: str = "/tmp/tgarr-alive",
    max_silence_seconds: int = 120,
    poll_interval_seconds: int = 15,
) -> None:
    # DEPLOY-05: OS-thread watchdog — kills the process when the asyncio loop
    # stalls. Must run outside the event loop: a hung loop would silence any
    # asyncio task (including _liveness_touch), so only an independent thread
    # can escape. On stall we dump all thread stacks to stderr (captured by
    # `docker logs`) before os._exit(1); `restart: unless-stopped` revives us.
    time.sleep(max_silence_seconds)
    p = Path(alive_path)
    while True:
        try:
            age = time.time() - p.stat().st_mtime
        except FileNotFoundError:
            age = max_silence_seconds + 1
        if age > max_silence_seconds:
            sys.stderr.write(
                f"[watchdog] liveness file stale for {age:.0f}s — "
                f"dumping threads and exiting\n"
            )
            sys.stderr.flush()
            faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
            os._exit(1)
        time.sleep(poll_interval_seconds)


def setup_logging(log_level: str) -> None:
    """Configure structured logging."""
    # Configure standard logging
    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, log_level.upper(), logging.INFO),
        stream=sys.stdout,
    )

    # Configure structlog
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            _mask_tokens,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer() if log_level.upper() == "DEBUG" else structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, log_level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


async def on_startup(
    bot: Bot,
    db: Database,
    notification_service: Optional[NotificationService],
) -> None:
    """Startup handler."""
    logger = structlog.get_logger()

    # Clean up old sessions
    cleaned_sessions = await db.cleanup_old_sessions(hours=24)
    if cleaned_sessions > 0:
        logger.info("Cleaned up old sessions", count=cleaned_sessions)

    # Clean up old searches
    cleaned_searches = await db.cleanup_old_searches(days=7)
    if cleaned_searches > 0:
        logger.info("Cleaned up old searches", count=cleaned_searches)

    # Start notification service if configured
    if notification_service:
        # Subscribe all allowed users to notifications
        settings = get_settings()
        for user_id in set(settings.allowed_tg_ids) | set(settings.admin_tg_ids or []):
            notification_service.subscribe_user(user_id)

        await notification_service.start()
        logger.info("Notification service started")

    # Get bot info
    bot_info = await bot.get_me()
    logger.info(
        "Bot started",
        username=bot_info.username,
        id=bot_info.id,
    )


async def on_shutdown(
    bot: Bot,
    db: Database,
    notification_service: Optional[NotificationService],
    qbittorrent: Optional[QBittorrentClient],
) -> None:
    """Shutdown handler."""
    logger = structlog.get_logger()
    logger.info("Bot shutting down...")

    # Stop notification service
    if notification_service:
        await notification_service.stop()
        logger.info("Notification service stopped")

    # Close qBittorrent client (for notification service)
    if qbittorrent:
        await qbittorrent.close()

    # Close all singleton clients from registry
    await close_all_clients()

    await db.close()


async def main() -> None:
    """Main function to run the bot."""
    # Load settings
    settings = get_settings()

    # Setup logging
    setup_logging(settings.log_level)

    logger = structlog.get_logger()
    logger.info("Starting TG_arr bot...", timezone=settings.timezone)

    # Validate settings
    if not settings.allowed_tg_ids and not settings.admin_tg_ids:
        logger.error(
            "No allowed users configured. Set ALLOWED_TG_IDS or ADMIN_TG_IDS environment variable.",
            allowed_tg_ids_raw=os.getenv("ALLOWED_TG_IDS"),
            admin_tg_ids_raw=os.getenv("ADMIN_TG_IDS"),
        )
        sys.exit(1)

    # Initialize database
    db = Database(settings.database_path)
    await db.connect()

    # Initialize bot
    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    # Initialize qBittorrent client and notification service if configured
    qbittorrent: Optional[QBittorrentClient] = None
    notification_service: Optional[NotificationService] = None

    if settings.qbittorrent_enabled:
        logger.info("qBittorrent integration enabled", url=settings.qbittorrent_url)
        qbittorrent = QBittorrentClient(
            settings.qbittorrent_url,
            settings.qbittorrent_username,
            settings.qbittorrent_password,
            timeout=settings.qbittorrent_timeout,
        )

        # Create notification sender function
        async def send_notification(user_id: int, message: str) -> None:
            try:
                await bot.send_message(user_id, message, parse_mode=ParseMode.HTML)
            except Exception as e:
                logger.warning(
                    "Failed to send notification",
                    user_id=user_id,
                    error=str(e),
                )

        notification_service = NotificationService(qbittorrent, send_notification)
    else:
        logger.info("qBittorrent integration disabled")

    # Initialize dispatcher
    dp = Dispatcher()

    # Setup middleware (order matters: logging -> rate limit -> auth)
    dp.message.middleware(LoggingMiddleware())
    dp.callback_query.middleware(LoggingMiddleware())
    rate_limiter = RateLimitMiddleware()
    dp.message.middleware(rate_limiter)
    dp.callback_query.middleware(rate_limiter)
    dp.message.middleware(AuthMiddleware(db))
    dp.callback_query.middleware(AuthMiddleware(db))

    # Setup routers
    main_router = setup_routers()
    dp.include_router(main_router)

    # Register startup/shutdown handlers
    async def _on_startup(*_: object, **__: object) -> None:
        await on_startup(bot, db, notification_service)

    async def _on_shutdown(*_: object, **__: object) -> None:
        await on_shutdown(bot, db, notification_service, qbittorrent)

    dp.startup.register(_on_startup)
    dp.shutdown.register(_on_shutdown)

    # SEC-14 / DEPLOY-04: liveness touch-file for Docker HEALTHCHECK.
    # The Dockerfile healthcheck looks for /tmp/tgarr-alive modified within
    # the last 2 minutes; we refresh it every 30s while the event loop is
    # healthy. Verify manually with: `docker kill --signal=SIGSTOP <cid>` →
    # container must flip to `unhealthy` within ~2 min.
    async def _liveness_touch() -> None:
        while True:
            try:
                Path("/tmp/tgarr-alive").touch()
            except Exception:
                # swallow — touch failure must not kill the bot
                pass
            await asyncio.sleep(30)

    liveness_task = asyncio.create_task(_liveness_touch())

    faulthandler.enable()
    threading.Thread(
        target=_liveness_watchdog,
        daemon=True,
        name="liveness-watchdog",
    ).start()

    # Start polling
    try:
        logger.info("Starting polling...")
        await dp.start_polling(
            bot,
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=True,
        )
    except Exception as e:
        logger.error("Bot crashed", error=str(e))
        raise
    finally:
        liveness_task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
