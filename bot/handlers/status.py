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
    get_navidrome,
    get_qbittorrent,
    get_scryer,
    get_slskd,
    get_torrserver,
)
from bot.models import (
    ContentType,
    QBittorrentStatus,
    ScryerHealth,
    SystemStatus,
    VIDEO_CONTENT_TYPES,
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
    scryer_health: "ScryerHealth | None" = None,
) -> str:
    """Feature #7: render the /health dashboard from already-gathered data.

    Pure function (no I/O) so it is unit-testable; the handler does the fetching.
    """
    lines = ["🩺 <b>Состояние системы</b>", ""]
    for s in statuses:
        icon = "✅" if s.available else "❌"
        ver = f" <code>{html.escape(s.version)}</code>" if s.version else ""
        lines.append(f"{icon} {html.escape(s.service)}{ver}")

    if scryer_health is not None:
        lines.append("")
        lines.append("🗂 <b>Каталог Scryer</b>")
        lines.append(
            f"  🎬 {scryer_health.titles_movie} · 📺 {scryer_health.titles_series} · "
            f"🎌 {scryer_health.titles_anime} (следим за {scryer_health.monitored_titles})"
        )
        if scryer_health.indexers:
            lines.append("")
            lines.append("🔎 <b>Индексеры (24ч)</b>")
            for stat in scryer_health.indexers:
                # A tracker that fails most queries is the usual root cause of
                # "почему ничего не находится" — surface it rather than hide it.
                icon = "⚠️" if stat.failure_rate > 0.5 else "✅"
                lines.append(
                    f"  {icon} {html.escape(stat.name)}: "
                    f"{stat.successful_24h}/{stat.queries_24h} ок, {stat.failed_24h} ошибок"
                )

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
    scryer = await get_scryer()
    lidarr = await get_lidarr()
    slskd = await get_slskd()
    navidrome = await get_navidrome()
    qbittorrent = await get_qbittorrent()
    emby = await get_emby()

    checks = [check_service(scryer, "Scryer")]
    if lidarr:
        checks.append(check_service(lidarr, "Lidarr"))
    if slskd:
        checks.append(check_service(slskd, "slskd"))
    if navidrome:
        checks.append(check_service(navidrome, "Navidrome"))
    if qbittorrent:
        checks.append(check_service(qbittorrent, "qBittorrent"))
    if emby:
        checks.append(check_service(emby, "Emby"))
    torrserver = await get_torrserver()
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


async def _gather_disks(scryer, lidarr) -> list[tuple[str, int | None]]:
    """Collect (root_folder_path, free_space), de-duped by path.

    Scryer exposes root folders per facet (movie/series/anime libraries live on
    different roots), so all three are queried; Lidarr adds the music root.
    Scryer's `RootFolderPayload` carries no free-space figure, so those entries
    render as "N/A" — the number is still available from qBittorrent below.
    """
    seen: dict[str, int | None] = {}

    async def add_scryer(content_type: ContentType) -> None:
        try:
            for folder in await scryer.get_root_folders(content_type):
                seen.setdefault(folder.path, folder.free_space)
        except Exception as e:
            logger.warning("root_folders_fetch_failed", service="scryer", error=str(e))

    async def add_lidarr() -> None:
        if lidarr is None:
            return
        try:
            for folder in await lidarr.get_root_folders():
                seen.setdefault(folder.path, folder.free_space)
        except Exception as e:
            logger.warning("root_folders_fetch_failed", service="lidarr", error=str(e))

    await asyncio.gather(
        *(add_scryer(ct) for ct in VIDEO_CONTENT_TYPES),
        add_lidarr(),
    )
    return list(seen.items())


@router.message(Command("health"))
async def cmd_health(message: Message) -> None:
    """Feature #7: one-glance dashboard — service reachability + disk free + qBit.

    Deliberately does not include Deezer in `_collect_statuses` (see docstring
    there) — this dashboard focuses on infra that affects grabs/downloads.
    """
    status_msg = await message.answer("🩺 Собираю состояние...")

    scryer = await get_scryer()
    lidarr = await get_lidarr()
    qbittorrent = await get_qbittorrent()

    try:
        statuses = await _collect_statuses(include_deezer=False)

        disks = await _gather_disks(scryer, lidarr)

        scryer_health: ScryerHealth | None = None
        try:
            scryer_health = await scryer.system_health()
        except Exception as e:
            logger.warning("scryer_health_failed", error=str(e))

        qbit: QBittorrentStatus | None = None
        if qbittorrent:
            try:
                qbit = await qbittorrent.get_status()
            except Exception as e:
                logger.warning("qbit_status_failed_for_health", error=str(e))

        await status_msg.edit_text(
            _format_health(statuses, disks, qbit, scryer_health), parse_mode="HTML"
        )

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




@router.message(Command("wanted"))
async def cmd_wanted(message: Message) -> None:
    """Show what Scryer is still hunting for.

    Added after the 2026-07-29 incident: 102 Paw Patrol episodes had been in
    the wanted queue for months, burning the indexers' whole daily quota every
    pass, and the only way to see that was raw GraphQL.
    """
    status_msg = await message.answer("📋 Собираю очередь поиска...")

    try:
        scryer = await get_scryer()
        items, total, _has_more = await scryer.get_wanted("MISSING", limit=200)
    except Exception as e:
        logger.error("wanted_fetch_failed", error=str(e), exc_info=True)
        await status_msg.edit_text(Formatters.format_error("Не удалось получить очередь поиска"))
        return

    if not items:
        await status_msg.edit_text("✅ Очередь поиска пуста — всё найдено.")
        return

    # Group by title: 102 separate Paw Patrol lines would be unreadable, and
    # the per-title count is exactly the number that matters.
    grouped: dict[str, list] = {}
    for item in items:
        grouped.setdefault(item.title_name, []).append(item)

    lines = [f"📋 <b>Ищется: {total} позиций</b>\n"]
    for title, entries in sorted(grouped.items(), key=lambda kv: -len(kv[1]))[:15]:
        seasons = sorted(
            {str(e.season_number) for e in entries if e.season_number is not None},
            key=lambda s: int(s) if s.isdigit() else 0,
        )
        season_str = f" · сезоны {', '.join(seasons)}" if seasons else ""
        lines.append(f"• <b>{html.escape(title)}</b> — {len(entries)} эп.{season_str}")

    if len(grouped) > 15:
        lines.append(f"\n<i>…и ещё {len(grouped) - 15} тайтлов</i>")

    if total > 60:
        lines.append(
            "\n⚠️ Очередь большая: Scryer не успеет обойти её за сутки и выжжет "
            "лимиты трекеров. Стоит снять мониторинг с того, чего всё равно нет."
        )

    await status_msg.edit_text("\n".join(lines), parse_mode="HTML")
