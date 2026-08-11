"""Status command handler."""

import asyncio
import html
from typing import Any

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
    get_torrserver,
)
from bot.models import (
    QBittorrentStatus,
    SystemStatus,
    format_bytes,
    format_speed,
)
from bot.ui.formatters import Formatters
from bot.ui.menu import MENU_STATUS

logger = structlog.get_logger()
router = Router()


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

    Radarr, Sonarr and Prowlarr are non-optional (registry.get_radarr/
    get_sonarr/get_prowlarr always return a client) and are always checked;
    Lidarr/qBittorrent/Emby/TorrServer are checked only when configured.

    Rollback 2026-08-10: Lidarr and Prowlarr both answer on `/api/v1`
    (`LidarrClient._api_prefix`), Radarr/Sonarr on `/api/v3` — a v3 probe
    against a healthy Lidarr reports "API DOWN" on a perfectly reachable
    service. `check_service` below just calls each client's own
    `check_connection()`, so it never has to know this itself; the client
    picks its own prefix.

    Note: `include_deezer` is only ever True from cmd_status — /health
    deliberately omits Deezer (it doesn't affect grab/download health and
    keeps the dashboard focused on infra the user acts on).
    """
    radarr = await get_radarr()
    sonarr = await get_sonarr()
    prowlarr = await get_prowlarr()
    lidarr = await get_lidarr()
    qbittorrent = await get_qbittorrent()
    emby = await get_emby()
    torrserver = await get_torrserver()

    checks = [
        check_service(radarr, "Radarr"),
        check_service(sonarr, "Sonarr"),
        check_service(prowlarr, "Prowlarr"),
    ]
    if lidarr:
        checks.append(check_service(lidarr, "Lidarr"))
    if qbittorrent:
        checks.append(check_service(qbittorrent, "qBittorrent"))
    if emby:
        checks.append(check_service(emby, "Emby"))
    if torrserver:
        checks.append(check_service(torrserver, "TorrServer"))
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
    """Collect (root_folder_path, free_space) across *arr clients, de-duped
    by path. `None` clients (e.g. Lidarr not configured) are skipped.
    """
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


def _group_wanted_episodes(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group Sonarr's flat wanted/missing episode records by series title.

    Review fix round 1 (2026-08-10, task-13 re-review): this used to say
    Sonarr's `/wanted/missing` embeds a `series` object "only when the
    caller asks for it", then didn't ask — every real record came back with
    neither `series` nor `seriesTitle`, and every episode fell through to
    "Неизвестный сериал" (live-verified against the real instance).
    `SonarrClient.get_wanted_episodes` now sends `includeSeries=true` (same
    flag `get_calendar` already used), so `rec["series"]["title"]` is
    populated in practice. The `seriesTitle`/"Неизвестный сериал" fallbacks
    stay as defence in depth for any caller that doesn't request the flag.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        series = rec.get("series") if isinstance(rec.get("series"), dict) else {}
        title = series.get("title") or rec.get("seriesTitle") or "Неизвестный сериал"
        grouped.setdefault(title, []).append(rec)
    return grouped


@router.message(Command("wanted"))
async def cmd_wanted(message: Message) -> None:
    """Show what Radarr/Sonarr are still hunting for.

    Added after the 2026-07-29 incident: 102 Paw Patrol episodes had been in
    the wanted queue for months, burning the indexers' whole daily quota
    every pass, and the only way to see that was raw GraphQL. Rollback
    2026-08-10: there is no single shared catalog query any more — Radarr
    and Sonarr each paginate their own `/wanted/missing`.
    """
    status_msg = await message.answer("📋 Собираю очередь поиска...")

    try:
        radarr = await get_radarr()
        sonarr = await get_sonarr()
        movies, episodes = await asyncio.gather(
            radarr.get_wanted_movies(page_size=200),
            sonarr.get_wanted_episodes(page_size=200),
        )
    except Exception as e:
        logger.error("wanted_fetch_failed", error=str(e), exc_info=True)
        await status_msg.edit_text(Formatters.format_error("Не удалось получить очередь поиска"))
        return

    total = len(movies) + len(episodes)
    if total == 0:
        await status_msg.edit_text("✅ Очередь поиска пуста — всё найдено.")
        return

    lines = [f"📋 <b>Ищется: {total} позиций</b>\n"]

    if movies:
        lines.append(f"🎬 <b>Фильмы ({len(movies)})</b>")
        for rec in movies[:15]:
            title = rec.get("title") or "Unknown"
            year = rec.get("year")
            year_str = f" ({year})" if year else ""
            lines.append(f"• <b>{html.escape(str(title))}</b>{year_str}")
        if len(movies) > 15:
            lines.append(f"<i>…и ещё {len(movies) - 15} фильмов</i>")

    if episodes:
        grouped = _group_wanted_episodes(episodes)
        if movies:
            lines.append("")
        lines.append(f"📺 <b>Сериалы ({len(grouped)} тайтлов, {len(episodes)} эп.)</b>")
        for title, entries in sorted(grouped.items(), key=lambda kv: -len(kv[1]))[:15]:
            seasons = sorted(
                {str(e.get("seasonNumber")) for e in entries if e.get("seasonNumber") is not None},
                key=lambda s: int(s) if s.isdigit() else 0,
            )
            season_str = f" · сезоны {', '.join(seasons)}" if seasons else ""
            lines.append(f"• <b>{html.escape(str(title))}</b> — {len(entries)} эп.{season_str}")
        if len(grouped) > 15:
            lines.append(f"<i>…и ещё {len(grouped) - 15} тайтлов</i>")

    if total > 60:
        lines.append(
            "\n⚠️ Очередь большая: Radarr/Sonarr не успеют обойти её за сутки и "
            "выжгут лимиты трекеров. Стоит снять мониторинг с того, чего всё равно нет."
        )

    await status_msg.edit_text("\n".join(lines), parse_mode="HTML")
