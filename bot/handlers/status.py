"""Status command handler."""

import asyncio

import structlog
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.clients.qbittorrent import QBittorrentClient
from bot.clients.registry import get_prowlarr, get_radarr, get_sonarr, get_qbittorrent, get_emby
from bot.models import SystemStatus
from bot.ui.formatters import Formatters

logger = structlog.get_logger()
router = Router()

# Russian menu button text
MENU_STATUS = "🔌 Статус"


@router.message(F.text == MENU_STATUS)
@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    """Handle /status command - check all services status."""
    status_msg = await message.answer("🔍 Проверяю статус сервисов...")

    prowlarr = get_prowlarr()
    radarr = get_radarr()
    sonarr = get_sonarr()
    qbittorrent = get_qbittorrent()
    emby = get_emby()

    try:
        # Build list of service checks
        service_checks = [
            check_service(prowlarr, "Prowlarr"),
            check_service(radarr, "Radarr"),
            check_service(sonarr, "Sonarr"),
        ]

        if qbittorrent:
            service_checks.append(check_qbittorrent(qbittorrent))

        if emby:
            service_checks.append(check_service(emby, "Emby"))

        # Check all services in parallel
        results = await asyncio.gather(*service_checks, return_exceptions=True)

        statuses = []
        for result in results:
            if isinstance(result, Exception):
                statuses.append(SystemStatus(
                    service="Unknown",
                    available=False,
                    error=str(result),
                ))
            else:
                statuses.append(result)

        text = Formatters.format_system_status(statuses)
        await status_msg.edit_text(text, parse_mode="HTML")

    except Exception as e:
        logger.error("Status check failed", error=str(e))
        await status_msg.edit_text(Formatters.format_error(f"Status check failed: {str(e)}"))


async def check_service(client, name: str) -> SystemStatus:
    """Check a single service status."""
    try:
        available, version, response_time = await client.check_connection()
        return SystemStatus(
            service=name,
            available=available,
            version=version,
            response_time_ms=response_time,
        )
    except Exception as e:
        logger.warning(f"{name} health check failed", error=str(e))
        return SystemStatus(
            service=name,
            available=False,
            error=str(e)[:100],
        )


async def check_qbittorrent(client: QBittorrentClient) -> SystemStatus:
    """Check qBittorrent status."""
    import time

    try:
        start = time.monotonic()
        logged_in = await client.login()

        if not logged_in:
            return SystemStatus(
                service="qBittorrent",
                available=False,
                error="Login failed",
            )

        status = await client.get_status()
        response_time = (time.monotonic() - start) * 1000

        return SystemStatus(
            service="qBittorrent",
            available=True,
            version=status.version,
            response_time_ms=round(response_time, 1),
        )

    except Exception as e:
        logger.warning("qBittorrent health check failed", error=str(e))
        return SystemStatus(
            service="qBittorrent",
            available=False,
            error=str(e)[:100],
        )
