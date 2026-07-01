"""Status command handler."""

import asyncio

import structlog
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.clients.registry import (
    get_deezer,
    get_emby,
    get_lidarr,
    get_prowlarr,
    get_qbittorrent,
    get_radarr,
    get_sonarr,
)
from bot.models import (
    QBittorrentStatus,
    SystemStatus,
    format_bytes,
    format_speed,
)
from bot.ui.formatters import Formatters

logger = structlog.get_logger()
router = Router()

# Russian menu button text
MENU_STATUS = "🔌 Статус"


def _format_health(
    statuses: list[SystemStatus],
    disks: list[tuple[str, int | None]],
    qbit: QBittorrentStatus | None,
) -> str:
    """Feature #7: render the /health dashboard from already-gathered data.

    Pure function (no I/O) so it is unit-testable; the handler does the fetching.
    """
    lines = ["🩺 <b>Состояние системы</b>", ""]
    for s in statuses:
        icon = "✅" if s.available else "❌"
        ver = f" <code>{s.version}</code>" if s.version else ""
        lines.append(f"{icon} {s.service}{ver}")

    if disks:
        lines.append("")
        lines.append("💽 <b>Диск (свободно)</b>")
        for path, free in disks:
            free_str = format_bytes(free) if free is not None else "N/A"
            lines.append(f"  <code>{path}</code>: {free_str}")

    if qbit is not None:
        lines.append("")
        lines.append("📊 <b>qBittorrent</b>")
        lines.append(
            f"  ⬇️ активных: {qbit.active_downloads} · {format_speed(qbit.download_speed)}"
        )
        lines.append(f"  💾 свободно: {format_bytes(qbit.free_space)}")

    return "\n".join(lines)


@router.message(F.text == MENU_STATUS)
@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    """Handle /status command - check all services status."""
    status_msg = await message.answer("🔍 Проверяю статус сервисов...")

    prowlarr = await get_prowlarr()
    radarr = await get_radarr()
    sonarr = await get_sonarr()
    lidarr = await get_lidarr()
    qbittorrent = await get_qbittorrent()
    emby = await get_emby()
    deezer = await get_deezer()

    try:
        # Build list of service checks
        service_checks = [
            check_service(prowlarr, "Prowlarr"),
            check_service(radarr, "Radarr"),
            check_service(sonarr, "Sonarr"),
        ]

        if lidarr:
            service_checks.append(check_service(lidarr, "Lidarr"))

        if qbittorrent:
            service_checks.append(check_service(qbittorrent, "qBittorrent"))

        if emby:
            service_checks.append(check_service(emby, "Emby"))

        if deezer:
            service_checks.append(check_service(deezer, "Deezer"))

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
        await status_msg.edit_text(Formatters.format_error("Проверка статуса не удалась"))


async def _gather_disks(*clients) -> list[tuple[str, int | None]]:
    """Collect (root_folder_path, free_space) across *arr clients, de-duped by path."""
    seen: dict[str, int | None] = {}

    async def add(client) -> None:
        if client is None:
            return
        try:
            for folder in await client.get_root_folders():
                seen.setdefault(folder.path, folder.free_space)
        except Exception as e:
            logger.warning("root_folders_fetch_failed", error=str(e))

    await asyncio.gather(*(add(c) for c in clients))
    return list(seen.items())


@router.message(Command("health"))
async def cmd_health(message: Message) -> None:
    """Feature #7: one-glance dashboard — service reachability + disk free + qBit."""
    status_msg = await message.answer("🩺 Собираю состояние...")

    prowlarr = await get_prowlarr()
    radarr = await get_radarr()
    sonarr = await get_sonarr()
    lidarr = await get_lidarr()
    qbittorrent = await get_qbittorrent()
    emby = await get_emby()

    try:
        checks = [
            check_service(prowlarr, "Prowlarr"),
            check_service(radarr, "Radarr"),
            check_service(sonarr, "Sonarr"),
        ]
        if lidarr:
            checks.append(check_service(lidarr, "Lidarr"))
        if qbittorrent:
            checks.append(check_service(qbittorrent, "qBittorrent"))
        if emby:
            checks.append(check_service(emby, "Emby"))

        statuses: list[SystemStatus] = []
        for r in await asyncio.gather(*checks, return_exceptions=True):
            statuses.append(
                r if isinstance(r, SystemStatus)
                else SystemStatus(service="Unknown", available=False, error=str(r))
            )

        disks = await _gather_disks(radarr, sonarr, lidarr)

        qbit: QBittorrentStatus | None = None
        if qbittorrent:
            try:
                qbit = await qbittorrent.get_status()
            except Exception as e:
                logger.warning("qbit_status_failed_for_health", error=str(e))

        await status_msg.edit_text(_format_health(statuses, disks, qbit), parse_mode="HTML")

    except Exception as e:
        logger.error("Health check failed", error=str(e))
        await status_msg.edit_text(Formatters.format_error("Не удалось собрать состояние"))


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


