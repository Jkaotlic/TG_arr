"""Status command handler."""

import asyncio
import html

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
        ver = f" <code>{html.escape(s.version)}</code>" if s.version else ""
        lines.append(f"{icon} {html.escape(s.service)}{ver}")

    if disks:
        lines.append("")
        lines.append("💽 <b>Диск (свободно)</b>")
        for path, free in disks:
            free_str = format_bytes(free) if free is not None else "N/A"
            lines.append(f"  <code>{html.escape(path)}</code>: {free_str}")

    if qbit is not None:
        lines.append("")
        lines.append("📊 <b>qBittorrent</b>")
        lines.append(
            f"  ⬇️ активных: {qbit.active_downloads} · {format_speed(qbit.download_speed)}"
        )
        lines.append(f"  💾 свободно: {format_bytes(qbit.free_space)}")

    return "\n".join(lines)


async def _collect_statuses(include_deezer: bool) -> list[SystemStatus]:
    """LOGIC-17: shared service-check fan-out for cmd_status/cmd_health.

    Note: `include_deezer` is only ever True from cmd_status — /health
    deliberately omits Deezer (it doesn't affect grab/download health and
    keeps the dashboard focused on infra the user acts on).
    """
    prowlarr = await get_prowlarr()
    radarr = await get_radarr()
    sonarr = await get_sonarr()
    lidarr = await get_lidarr()
    qbittorrent = await get_qbittorrent()
    emby = await get_emby()

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
    if include_deezer:
        deezer = await get_deezer()
        if deezer:
            checks.append(check_service(deezer, "Deezer"))

    statuses: list[SystemStatus] = []
    for result in await asyncio.gather(*checks, return_exceptions=True):
        if isinstance(result, SystemStatus):
            statuses.append(result)
        else:
            statuses.append(SystemStatus(service="Unknown", available=False, error=str(result)))
    return statuses


@router.message(F.text == MENU_STATUS)
@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    """Handle /status command - check all services status."""
    status_msg = await message.answer("🔍 Проверяю статус сервисов...")

    try:
        statuses = await _collect_statuses(include_deezer=True)
        text = Formatters.format_system_status(statuses)
        await status_msg.edit_text(text, parse_mode="HTML")

    except Exception as e:
        logger.error("Status check failed", error=str(e), exc_info=True)
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
    """Feature #7: one-glance dashboard — service reachability + disk free + qBit.

    Deliberately does not include Deezer in `_collect_statuses` (see docstring
    there) — this dashboard focuses on infra that affects grabs/downloads.
    """
    status_msg = await message.answer("🩺 Собираю состояние...")

    radarr = await get_radarr()
    sonarr = await get_sonarr()
    lidarr = await get_lidarr()
    qbittorrent = await get_qbittorrent()

    try:
        statuses = await _collect_statuses(include_deezer=False)

        disks = await _gather_disks(radarr, sonarr, lidarr)

        qbit: QBittorrentStatus | None = None
        if qbittorrent:
            try:
                qbit = await qbittorrent.get_status()
            except Exception as e:
                logger.warning("qbit_status_failed_for_health", error=str(e))

        await status_msg.edit_text(_format_health(statuses, disks, qbit), parse_mode="HTML")

    except Exception as e:
        logger.error("Health check failed", error=str(e), exc_info=True)
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
        logger.warning("health_check_failed", service=name, error=str(e))
        return SystemStatus(
            service=name,
            available=False,
            error=str(e)[:100],
        )


