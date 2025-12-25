"""Main entry point for the TG_arr Telegram bot."""

import asyncio
import logging
import os
import sys
from datetime import datetime
from typing import Optional

import structlog
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bot.clients.qbittorrent import QBittorrentClient
from bot.config import get_settings
from bot.db import Database
from bot.handlers import setup_routers
from bot.middleware.auth import AuthMiddleware, LoggingMiddleware
from bot.services.notification_service import NotificationService


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

    # Ensure database is connected and tables exist
    await db.connect()

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
        for user_id in settings.allowed_tg_ids:
            notification_service.subscribe_user(user_id)
        for user_id in settings.admin_tg_ids:
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

    # Close qBittorrent client
    if qbittorrent:
        await qbittorrent.close()

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

    # Initialize bot
    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
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
        )

        # Create notification sender function
        async def send_notification(user_id: int, message: str) -> None:
            try:
                await bot.send_message(user_id, message, parse_mode=ParseMode.MARKDOWN)
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

    # Setup middleware
    dp.message.middleware(LoggingMiddleware())
    dp.callback_query.middleware(LoggingMiddleware())
    dp.message.middleware(AuthMiddleware(db))
    dp.callback_query.middleware(AuthMiddleware(db))

    # Setup routers
    main_router = setup_routers()
    dp.include_router(main_router)

    # Register startup/shutdown handlers
    dp.startup.register(lambda: on_startup(bot, db, notification_service))
    dp.shutdown.register(lambda: on_shutdown(bot, db, notification_service, qbittorrent))

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
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
