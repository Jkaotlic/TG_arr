"""Main entry point for the TG_arr Telegram bot."""

import asyncio
import faulthandler
import html
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import structlog
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bot.clients.registry import close_all as close_all_clients
from bot.clients.registry import get_qbittorrent
from bot.config import get_settings
from bot.db import Database
from bot.handlers import setup_routers
from bot.middleware.auth import AuthMiddleware, LoggingMiddleware, RateLimitMiddleware
from bot.services.notification_service import NotificationService


# SEC-03: mask Telegram bot tokens in any log event value
_TOKEN_PATTERN = re.compile(r'bot\d+:[A-Za-z0-9_-]{30,}')
# SEC-05: mask basic-auth-style credentials embedded in URLs (e.g. a proxy
# URL like http://user:pass@host leaking into an httpx exception message).
_USERINFO_PATTERN = re.compile(r'://[^/\s@]+:[^/\s@]+@')


def _mask_value(v: str) -> str:
    v = _TOKEN_PATTERN.sub('bot***:***', v)
    v = _USERINFO_PATTERN.sub('://***:***@', v)
    return v


def _mask_recursive(value):
    """SEC-05: recurse into dict/list/tuple values so secrets nested inside
    structured kv values (not just top-level strings) get masked too."""
    if isinstance(value, str):
        return _mask_value(value)
    if isinstance(value, dict):
        return {k: _mask_recursive(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_recursive(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_mask_recursive(v) for v in value)
    return value


def _mask_tokens(logger, method_name, event_dict):
    """Structlog processor that redacts secrets from log values (recursive)."""
    for k, v in list(event_dict.items()):
        event_dict[k] = _mask_recursive(v)
    return event_dict


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


# OBS-01: these stdlib loggers are extremely chatty at INFO (one bare line per
# HTTP call / per Telegram update / per webhook access) and bypass structlog's
# JSON+mask pipeline entirely when left on logging.basicConfig's default
# formatter. Silencing them to WARNING keeps signal-to-noise sane; routing
# them through ProcessorFormatter below keeps anything that DOES get through
# (e.g. an aiogram network error containing a URL) JSON-formatted and masked.
_NOISY_STDLIB_LOGGERS = ("httpx", "httpcore", "aiogram.event", "aiohttp.access")


def setup_logging(log_level: str, log_format: str = "json") -> None:
    """Configure structured logging.

    OBS-13: ``log_format`` (json|console) is independent of ``log_level`` —
    switching to DEBUG for diagnostics must not silently swap the prod log
    stream to a non-JSON ConsoleRenderer.
    """
    level = getattr(logging, log_level.upper(), logging.INFO)
    renderer = (
        structlog.dev.ConsoleRenderer()
        if log_format == "console"
        else structlog.processors.JSONRenderer()
    )

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        _mask_tokens,
        structlog.processors.TimeStamper(fmt="iso"),
    ]

    # OBS-01: stdlib logging (aiogram/httpx/aiohttp/etc.) is routed through a
    # structlog ProcessorFormatter so it lands on the same JSON+mask pipeline
    # as structlog-native events instead of bare unstructured lines.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)

    for name in _NOISY_STDLIB_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    # Configure structlog
    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
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
        # Subscribe all allowed users to notifications: env allowlist (#6:
        # DB-04/BUG-15/LOGIC-08) UNION runtime-granted DB allowlist, so users
        # added via /adduser also receive download-completion notifications.
        settings = get_settings()
        db_user_ids = await db.list_allowed_users()
        for user_id in set(settings.allowed_tg_ids) | set(settings.admin_tg_ids) | set(db_user_ids):
            notification_service.subscribe_user(user_id)

        # OBS-07: notification_service.start() already logs "Notification
        # service started" — don't duplicate it here.
        await notification_service.start()

    # PERF-08: warm up the clients so the first user search doesn't pay
    # DNS+TCP+TLS handshake (extra 0.5–1.5s on rpie4 over Wi-Fi).
    warmup = await _warm_up_clients(logger)

    # Get bot info
    bot_info = await bot.get_me()
    logger.info(
        "Bot started",
        username=bot_info.username,
        id=bot_info.id,
    )

    # Tell the admins the bot is up and which backends answered — the warm-up
    # already probed them, so this costs nothing extra.
    settings = get_settings()
    db_user_ids = await db.list_allowed_users()
    await notify_admins_on_start(
        bot,
        admin_ids=sorted(set(settings.admin_tg_ids)),
        allowed_ids=sorted(set(settings.allowed_tg_ids) | set(db_user_ids)),
        warmup=warmup,
    )


#: Display names for the warm-up probe keys, in the order they're shown.
_BACKEND_LABELS = (
    ("scryer", "🗂 Scryer"),
    ("lidarr", "🎵 Lidarr"),
    ("slskd", "🎧 slskd"),
)


def _format_backend_line(label: str, outcome) -> str:
    """One "service: state" line for the startup card.

    `outcome` mirrors `_warm_up_clients`: None = not configured, ("error", msg)
    = the probe raised, (ok, elapsed_ms) otherwise. A probe error goes through
    the same masking as the logs (it can carry a URL with embedded
    credentials — the TMDb proxy URL does) and is escaped, because the message
    is sent with parse_mode=HTML.
    """
    if outcome is None:
        return f"{label}: ⚪ не настроен"
    state, detail, *rest = outcome
    if state == "error":
        return f"{label}: ❌ {html.escape(_mask_value(str(detail)))[:80]}"
    if state:
        version = rest[0] if rest else None
        suffix = f" v{html.escape(str(version))}" if version else ""
        return f"{label}: ✅{suffix} · {detail:.0f} мс"
    return f"{label}: ❌ не отвечает"


def build_startup_message(
    *, warmup: dict, admin_ids: list, allowed_ids: list, version: Optional[str] = None
) -> str:
    """Render the "bot is up" card sent to admins.

    `version` is accepted for callers that know it out-of-band; normally each
    backend reports its own in the warm-up outcome and is rendered inline.
    """
    lines = [
        "🚀 <b>TG_arr запущен!</b>",
        "",
        f"⏰ Время: {time.strftime('%H:%M:%S %d.%m.%Y')}",
        f"👑 Администраторов: {len(admin_ids)}",
        f"👥 Разрешённых пользователей: {len(allowed_ids)}",
    ]
    backend_lines = [
        _format_backend_line(label, warmup.get(key))
        for key, label in _BACKEND_LABELS
        if key in warmup
    ]
    if backend_lines:
        lines.append("")
        lines.extend(backend_lines)

    lines.append("")
    lines.append("✅ Бот готов к работе!")
    return "\n".join(lines)


async def notify_admins_on_start(bot, *, admin_ids: list, allowed_ids: list, warmup: dict) -> None:
    """Send the startup card to every admin.

    Best-effort per admin: one who blocked the bot must not stop the others
    from being told, and a delivery failure must never abort startup.
    """
    if not get_settings().notify_admins_on_start or not admin_ids:
        return

    text = build_startup_message(warmup=warmup, admin_ids=admin_ids, allowed_ids=allowed_ids)

    async def _send(admin_id: int) -> None:
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML")
        except Exception as e:
            logger = structlog.get_logger()
            logger.warning("startup_notify_failed", user_id=admin_id, error=str(e))

    await asyncio.gather(*(_send(admin_id) for admin_id in admin_ids))


async def _warm_up_clients(logger) -> dict:
    """Run health checks in parallel to prime singleton HTTP clients.

    Returns the per-backend outcome map, which the startup notification
    reuses so the probe isn't repeated.
    """
    from bot.clients.registry import get_lidarr, get_scryer, get_slskd

    structlog.contextvars.bind_contextvars(component="warmup")
    try:
        async def _check(name, factory):
            try:
                client = await factory()
                if client is None:
                    return name, None
                # Scryer's probe includes the login round-trip, so give it more
                # room than a plain health endpoint would need.
                ok, version, ms = await asyncio.wait_for(client.check_connection(), timeout=15.0)
                return name, (ok, ms, version)
            except Exception as e:
                return name, ("error", str(e))

        results = await asyncio.gather(
            _check("scryer", get_scryer),
            _check("lidarr", get_lidarr),
            _check("slskd", get_slskd),
            return_exceptions=True,
        )
        summary = {name: outcome for r in results if isinstance(r, tuple) for name, outcome in [r]}
        logger.info("warmup_completed", summary=summary)
        return summary
    finally:
        structlog.contextvars.unbind_contextvars("component")


async def _health_watch(bot: Bot, admin_ids: list, logger) -> None:
    """Poll Scryer's health and alert admins when the stack degrades.

    Added after the 2026-07-29 incident: the indexers had been failing for six
    hours before anyone noticed, and every signal needed was already sitting in
    `systemHealth` + `wantedItems`. 15 minutes is frequent enough to catch a
    real outage and far too slow to add meaningful load.
    """
    from bot.clients.registry import get_scryer
    from bot.services.health_monitor import HealthMonitor

    interval = 15 * 60

    async def _notify(text: str) -> None:
        for admin_id in admin_ids:
            try:
                await bot.send_message(admin_id, text, parse_mode=ParseMode.HTML)
            except Exception as e:
                logger.warning("health_alert_send_failed", user_id=admin_id, error=str(e))

    monitor = HealthMonitor(_notify)
    structlog.contextvars.bind_contextvars(component="health_watch")
    try:
        while True:
            try:
                await asyncio.sleep(interval)
                scryer = await get_scryer()
                health = await scryer.system_health()
                _items, wanted_total, _more = await scryer.get_wanted("MISSING", limit=1)
                await monitor.evaluate(health, wanted_total)
            except asyncio.CancelledError:
                break
            except Exception as e:
                # A probe failure is itself worth knowing about, but not worth
                # a message every 15 minutes — the log carries it.
                logger.warning("health_watch_failed", error=str(e))
    finally:
        structlog.contextvars.unbind_contextvars("component")


async def _library_watch(bot: Bot, db: Database, logger) -> None:
    """Announce library imports and finished music downloads.

    qBittorrent's watcher only knows "the torrent finished"; this covers the
    part the user actually waits for — Scryer importing the file — and music,
    which never had completion notices at all (slskd is a separate client).
    """
    from bot.clients.registry import get_scryer, get_slskd
    from bot.services.library_watcher import LibraryWatcher

    settings = get_settings()
    interval = max(settings.notify_check_interval, 60)

    async def _notify(text: str) -> None:
        db_user_ids = await db.list_allowed_users()
        for uid in set(settings.allowed_tg_ids) | set(settings.admin_tg_ids) | set(db_user_ids):
            try:
                await bot.send_message(uid, text, parse_mode=ParseMode.HTML)
            except Exception as e:
                logger.warning("library_notify_send_failed", user_id=uid, error=str(e))

    watcher = LibraryWatcher(_notify, get_scryer=get_scryer, get_slskd=get_slskd)
    structlog.contextvars.bind_contextvars(component="library_watch")
    try:
        while True:
            try:
                await asyncio.sleep(interval)
                await watcher.poll()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("library_watch_failed", error=str(e))
    finally:
        structlog.contextvars.unbind_contextvars("component")


async def _periodic_cleanup(db: Database, logger, notification_service: Optional[NotificationService] = None) -> None:
    """DB-15: drop stale sessions/searches every 6h instead of only at startup.

    Task F Interface: uses ``Database.run_maintenance(backup=...)`` instead of
    the three separate cleanup_* calls; every 4th cycle (~daily) also takes a
    VACUUM INTO backup.
    OBS-09: also logs a periodic notification_stats summary so the otherwise
    dead get_stats() has an observability payoff.
    """
    interval = 6 * 3600
    structlog.contextvars.bind_contextvars(component="periodic_cleanup")
    cycle = 0
    consecutive_failures = 0
    try:
        while True:
            try:
                await asyncio.sleep(interval)
                cycle += 1
                stats = await db.run_maintenance(backup=(cycle % 4 == 0))
                if any(stats.values()):
                    logger.info("periodic_cleanup", **stats)

                if notification_service is not None:
                    logger.info("notification_stats", **notification_service.get_stats())

                if consecutive_failures:
                    logger.info("periodic_cleanup_recovered", after_failures=consecutive_failures)
                    consecutive_failures = 0
            except asyncio.CancelledError:
                break
            except Exception as e:
                consecutive_failures += 1
                # OBS-06: avoid ERROR-spam every cycle — log loudly on the
                # first failure and then every 10th, DEBUG in between.
                if consecutive_failures == 1 or consecutive_failures % 10 == 0:
                    logger.error(
                        "periodic_cleanup_failed",
                        error=str(e),
                        consecutive_failures=consecutive_failures,
                        exc_info=True,
                    )
                else:
                    logger.debug("periodic_cleanup_failed", error=str(e), consecutive_failures=consecutive_failures)
    finally:
        structlog.contextvars.unbind_contextvars("component")


async def _cancel_background_tasks(tasks) -> None:
    """BUG-R6-01: cancel background tasks and WAIT for them to unwind.

    `task.cancel()` only schedules the CancelledError — it returns before the
    task has run its `finally` blocks. Shutting down right after meant the
    liveness/cleanup tasks were still pending when the loop closed, producing
    "Task was destroyed but it is pending!" and skipping their cleanup (e.g.
    `unbind_contextvars`). Exceptions raised while unwinding are swallowed:
    shutdown must not fail because a background task misbehaved on the way out.
    """
    pending = [t for t in tasks if t is not None]
    for task in pending:
        task.cancel()
    for task in pending:
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: B014 -- explicit: nothing may escape shutdown
            pass


async def on_shutdown(
    bot: Bot,
    db: Database,
    notification_service: Optional[NotificationService],
) -> None:
    """Shutdown handler."""
    logger = structlog.get_logger()
    logger.info("Bot shutting down...")

    # Stop notification service
    if notification_service:
        await notification_service.stop()
        logger.info("Notification service stopped")

    # RACE-05: the qBittorrent client used by the notification service is the
    # registry singleton, so close_all_clients() below closes it exactly once.
    # Close all singleton clients from registry
    await close_all_clients()

    await db.close()


async def main() -> None:
    """Main function to run the bot."""
    # Load settings
    settings = get_settings()

    # Setup logging
    setup_logging(settings.log_level, settings.log_format)

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
    notification_service: Optional[NotificationService] = None

    if settings.qbittorrent_enabled:
        logger.info("qBittorrent integration enabled", url=settings.qbittorrent_url)
        # RACE-05: reuse the registry's qBittorrent singleton (the same client
        # the download handlers use) instead of constructing a second one, so
        # there is ONE qBittorrent client / auth session. Its lifecycle is owned
        # by the registry — close_all() handles shutdown.
        qbittorrent = await get_qbittorrent()

        # OBS-03: return bool instead of swallowing silently — the previous
        # version caught every exception and returned None, which made
        # NotificationService._notify_completion's "Sent completion
        # notification" INFO log fire unconditionally, even when delivery
        # failed (blocked bot, flood limit, etc).
        async def send_notification(user_id: int, message: str) -> bool:
            try:
                await bot.send_message(user_id, message, parse_mode=ParseMode.HTML)
                return True
            except Exception as e:
                logger.warning(
                    "Failed to send notification",
                    user_id=user_id,
                    error=str(e),
                )
                return False

        if qbittorrent is not None:
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
        await on_shutdown(bot, db, notification_service)

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
    logger.info("background_task_started", task="liveness")
    cleanup_task = asyncio.create_task(_periodic_cleanup(db, logger, notification_service))
    logger.info("background_task_started", task="cleanup")
    health_task = asyncio.create_task(
        _health_watch(bot, sorted(set(settings.admin_tg_ids)), logger)
    )
    logger.info("background_task_started", task="health_watch")
    library_task = asyncio.create_task(_library_watch(bot, db, logger))
    logger.info("background_task_started", task="library_watch")

    faulthandler.enable()
    threading.Thread(
        target=_liveness_watchdog,
        daemon=True,
        name="liveness-watchdog",
    ).start()
    logger.info("background_task_started", task="watchdog")

    # Start polling
    try:
        logger.info("Starting polling...")
        await dp.start_polling(
            bot,
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=True,
            # DB-04/BUG-15: exposed to handlers (bot/handlers/users.py) as the
            # `notification_service` kwarg so /adduser and /deluser can
            # subscribe/unsubscribe at runtime, matching on_startup's env+DB
            # subscription. None when qBittorrent isn't configured — handlers
            # must treat that as "no-op, not an error".
            notification_service=notification_service,
        )
    except Exception as e:
        logger.error("Bot crashed", error=str(e), exc_info=True)
        raise
    finally:
        # BUG-R6-01: await the cancellation instead of firing and forgetting.
        await _cancel_background_tasks(
            [liveness_task, cleanup_task, health_task, library_task]
        )
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
