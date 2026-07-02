"""Download management handlers for qBittorrent integration."""

import html
from typing import Optional

import structlog
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot.clients.qbittorrent import QBittorrentClient, QBittorrentError
from bot.clients.registry import get_qbittorrent
from bot.handlers.common import safe_edit, strip_command
from bot.models import TorrentFilter, TorrentInfo, User, format_speed
from bot.ui.callbacks import TorrentPageCB
from bot.ui.formatters import Formatters
from bot.ui.keyboards import CallbackData, Keyboards
from bot.ui.menu import MENU_DOWNLOADS, MENU_QSTATUS

logger = structlog.get_logger()
router = Router()

# Per-page limit for torrent list
TORRENTS_PER_PAGE = 5


async def check_qbt_enabled(message_or_callback: Message | CallbackQuery) -> Optional[QBittorrentClient]:
    """Return the qBittorrent client if configured, else notify and return None.

    LOGIC-16: returning the client itself (instead of a bool) lets callers do
    a single check-and-use instead of ``check_qbt_enabled()`` followed by a
    second, redundant ``get_qbittorrent()`` call.
    """
    qbt = await get_qbittorrent()
    if qbt is None:
        text = "⚠️ Интеграция с qBittorrent не настроена.\n\nУстановите <code>QBITTORRENT_URL</code> и <code>QBITTORRENT_PASSWORD</code> в переменных окружения."
        if isinstance(message_or_callback, Message):
            await message_or_callback.answer(text)
        elif isinstance(message_or_callback, CallbackQuery):
            await message_or_callback.answer("qBittorrent не настроен", show_alert=True)
        return None
    return qbt


def _parse_filter(value: str) -> TorrentFilter:
    """Parse a filter string, defaulting to ALL for unknown/legacy values (LOGIC-01)."""
    try:
        return TorrentFilter(value)
    except ValueError:
        return TorrentFilter.ALL


# ============================================================================
# Commands
# ============================================================================


@router.message(F.text == MENU_DOWNLOADS)
@router.message(Command("downloads", "dl"))
async def cmd_downloads(message: Message, db_user: User) -> None:
    """Handle /downloads command - show active downloads."""
    qbt = await check_qbt_enabled(message)
    if not qbt:
        return

    try:
        status_msg = await message.answer("🔄 Загружаю список...")

        # Single API call - get all and slice locally
        all_torrents = await qbt.get_torrents(filter_type=TorrentFilter.ALL)
        total = len(all_torrents)

        if not all_torrents:
            await status_msg.edit_text("📭 Торренты не найдены.")
            return

        total_pages = max(1, (total + TORRENTS_PER_PAGE - 1) // TORRENTS_PER_PAGE)
        torrents = all_torrents[:TORRENTS_PER_PAGE]

        text = Formatters.format_torrent_list(torrents, 0, total_pages, TorrentFilter.ALL, total)

        await status_msg.edit_text(
            text,
            reply_markup=Keyboards.torrent_list(torrents, 0, total_pages, TorrentFilter.ALL),
            parse_mode="HTML",
        )

    except QBittorrentError as e:
        logger.error("qBittorrent error", error=str(e), exc_info=True)
        await message.answer("❌ Ошибка qBittorrent. Попробуйте позже.")
    except Exception as e:
        logger.error("Failed to get downloads", error=str(e), exc_info=True)
        await message.answer("❌ Ошибка загрузки данных. Попробуйте позже.")


@router.message(F.text == MENU_QSTATUS)
@router.message(Command("qstatus"))
async def cmd_qstatus(message: Message, db_user: User) -> None:
    """Handle /qstatus command - show qBittorrent status."""
    qbt = await check_qbt_enabled(message)
    if not qbt:
        return

    try:
        status_msg = await message.answer("🔄 Загружаю статус qBittorrent...")

        status = await qbt.get_status()
        text = Formatters.format_qbittorrent_status(status)

        await status_msg.edit_text(text, parse_mode="HTML")

    except QBittorrentError as e:
        logger.error("qBittorrent error", error=str(e), exc_info=True)
        await message.answer("❌ Ошибка qBittorrent. Попробуйте позже.")
    except Exception as e:
        logger.error("Failed to get qBittorrent status", error=str(e), exc_info=True)
        await message.answer("❌ Ошибка получения статуса. Попробуйте позже.")


@router.message(Command("pause"))
async def cmd_pause(message: Message, db_user: User, is_admin: bool = False) -> None:
    """Handle /pause command - pause torrents."""
    qbt = await check_qbt_enabled(message)
    if not qbt:
        return

    try:
        args = strip_command(message.text, "/pause") if message.text else ""

        if args.lower() == "all" or not args:
            # SEC-06: pausing every torrent is a blanket action - admin only
            # (pausing a single named torrent below remains open to all
            # allowed users).
            if not is_admin:
                await message.answer("⛔ Недостаточно прав для остановки всех торрентов.")
                return
            await qbt.pause("all")
            await message.answer("⏸️ Все торренты приостановлены.")
        else:
            # Try to find torrent by partial hash
            torrent = await qbt.get_torrent_by_short_hash(args)
            if torrent:
                await qbt.pause([torrent.hash])
                await message.answer(f"⏸️ Приостановлен: {html.escape(torrent.name)}")
            else:
                await message.answer(f"❌ Торрент не найден: {html.escape(args)}")

    except QBittorrentError as e:
        # OBS-12b: this branch previously replied to the user without a log
        # trace, so qBit-side failures were invisible in the logs.
        logger.debug("qbit_command_failed", command="pause", error=e.message)
        await message.answer(f"❌ Ошибка: {e.message}")


@router.message(Command("resume"))
async def cmd_resume(message: Message, db_user: User, is_admin: bool = False) -> None:
    """Handle /resume command - resume torrents."""
    qbt = await check_qbt_enabled(message)
    if not qbt:
        return

    try:
        args = strip_command(message.text, "/resume") if message.text else ""

        if args.lower() == "all" or not args:
            # SEC-06: same admin gate as /pause all.
            if not is_admin:
                await message.answer("⛔ Недостаточно прав для запуска всех торрентов.")
                return
            await qbt.resume("all")
            await message.answer("▶️ Все торренты возобновлены.")
        else:
            torrent = await qbt.get_torrent_by_short_hash(args)
            if torrent:
                await qbt.resume([torrent.hash])
                await message.answer(f"▶️ Возобновлён: {html.escape(torrent.name)}")
            else:
                await message.answer(f"❌ Торрент не найден: {html.escape(args)}")

    except QBittorrentError as e:
        logger.debug("qbit_command_failed", command="resume", error=e.message)
        await message.answer(f"❌ Ошибка: {e.message}")


# ============================================================================
# Callback handlers
# ============================================================================


async def _render_torrent_list(
    message: Message,
    qbt: QBittorrentClient,
    filter_type: TorrentFilter = TorrentFilter.ALL,
    page: int = 0,
) -> None:
    """Render a (optionally filtered) page of the torrent list into ``message``.

    LOGIC-01: accepts ``filter_type``/``page`` so pagination, refresh, and
    "back to list" all redraw the list the user was actually looking at
    instead of silently resetting to the unfiltered first page. ``page`` is
    clamped into range (used by callers — pause-all/delete/etc. — that don't
    need to distinguish an out-of-range request from a valid one).

    BUG-15: extracted so mutating callbacks (delete/delete-with-files/pause-all
    /resume-all) can redraw the list without re-invoking a callback handler,
    which would ack the callback a second time. This helper does NOT call
    ``callback.answer`` — the caller owns the single ack per callback.
    """
    all_torrents = await qbt.get_torrents(filter_type=filter_type)
    total = len(all_torrents)
    total_pages = max(1, (total + TORRENTS_PER_PAGE - 1) // TORRENTS_PER_PAGE)
    clamped_page = max(0, min(page, total_pages - 1))
    offset = clamped_page * TORRENTS_PER_PAGE
    torrents = all_torrents[offset:offset + TORRENTS_PER_PAGE]

    text = Formatters.format_torrent_list(torrents, clamped_page, total_pages, filter_type, total)
    await safe_edit(
        message,
        text,
        reply_markup=Keyboards.torrent_list(torrents, clamped_page, total_pages, filter_type),
        parse_mode="HTML",
    )


@router.callback_query(TorrentPageCB.filter())
async def handle_torrent_page(callback: CallbackQuery, callback_data: TorrentPageCB) -> None:
    """Render the torrent list at ``callback_data.page``/``callback_data.flt``.

    LOGIC-01: replaces the old separate ``t_refresh``/``t_page:N``/``t_back``
    string handlers — refresh, pagination, and "back to list" are all just
    "redraw this filter at this page" now, so the filter can no longer be
    silently dropped on any of those three paths.

    TEST-08a: unlike ``_render_torrent_list`` (which clamps and always
    renders — used by mutating callbacks that just want "some valid page"),
    an explicit pagination request for an out-of-range page must NOT render
    a different page silently; it must alert instead. This does one
    ``get_torrents`` call and reuses the result for both the bounds check and
    the render, so the fetch happens exactly once either way.
    """
    if not callback.message:
        return

    qbt = await check_qbt_enabled(callback)
    if not qbt:
        return

    filter_type = _parse_filter(callback_data.flt)
    requested_page = callback_data.page

    try:
        all_torrents = await qbt.get_torrents(filter_type=filter_type)
        total = len(all_torrents)
        total_pages = max(1, (total + TORRENTS_PER_PAGE - 1) // TORRENTS_PER_PAGE)

        if requested_page < 0 or requested_page >= total_pages:
            await callback.answer("Неверная страница", show_alert=True)
            return

        offset = requested_page * TORRENTS_PER_PAGE
        torrents = all_torrents[offset:offset + TORRENTS_PER_PAGE]
        text = Formatters.format_torrent_list(torrents, requested_page, total_pages, filter_type, total)

        await safe_edit(
            callback.message,
            text,
            reply_markup=Keyboards.torrent_list(torrents, requested_page, total_pages, filter_type),
            parse_mode="HTML",
        )
        await callback.answer()

    except Exception as e:
        logger.error("Pagination error", error=str(e), exc_info=True)
        await callback.answer("Ошибка операции", show_alert=True)


@router.callback_query(F.data.startswith(CallbackData.TORRENT_PAGE))
async def handle_legacy_page(callback: CallbackQuery) -> None:
    """Legacy ``t_page:N`` buttons from messages sent before the TorrentPageCB
    migration (LOGIC-01/TEST-08a) — these carry no filter information at all,
    so they cannot be redrawn correctly. Tell the user to refresh instead of
    silently rendering an unfiltered list.
    """
    await callback.answer("Кнопка устарела, обновите список", show_alert=True)


async def _refetch_one(qbt: QBittorrentClient, torrent: TorrentInfo, short_hash: str):
    """Re-fetch a single torrent after a mutating action (PERF-01).

    Prefers ``qbt.get_torrent(full_hash)`` which uses qBittorrent's server-side
    ``hashes`` filter (one targeted row) instead of pulling and parsing the whole
    list. Falls back to the short-hash lookup when the targeted fetch is
    unavailable or yields a non-``TorrentInfo`` result, so the redraw always has
    a usable torrent.
    """
    get_one = getattr(qbt, "get_torrent", None)
    if get_one is not None:
        try:
            refreshed = await get_one(torrent.hash)
        except Exception as e:
            # OBS-12b: previously swallowed with no trace at all.
            logger.debug("refetch_failed", hash=short_hash, error=str(e))
            refreshed = None
        if isinstance(refreshed, TorrentInfo):
            return refreshed
    return await qbt.get_torrent_by_short_hash(short_hash)


async def _render_torrent_details(
    message: Message,
    torrent: TorrentInfo,
    current_filter: TorrentFilter = TorrentFilter.ALL,
) -> None:
    """Render torrent details into ``message``.

    LOGIC-01: ``current_filter`` is threaded through to the "back to list"
    button so a filtered list survives a details round-trip.

    BUG-15: Extracted helper so mutating callbacks (pause/resume/delete) can
    redraw the details view after acknowledging the callback, without triggering
    a recursive ``callback.answer`` via the full ``handle_torrent_details``
    path. This helper does NOT call ``callback.answer`` — the caller owns the
    single ack per callback.
    """
    text = Formatters.format_torrent_details(torrent)
    await safe_edit(
        message,
        text,
        reply_markup=Keyboards.torrent_details(torrent, current_filter),
        parse_mode="HTML",
    )


async def _resolve_torrent(qbt: QBittorrentClient, hash_or_short: str) -> Optional[TorrentInfo]:
    """Resolve a torrent from callback_data hash text (PERF-05).

    New buttons carry the full 40-hex hash and resolve via the targeted
    ``get_torrent`` (no full-list scan). Anything shorter than a full SHA-1
    hex digest (40 chars) is a legacy 16-char truncated hash from a message
    built before PERF-05 — fall back to the scan-based short-hash lookup.
    """
    if len(hash_or_short) >= 40:
        torrent = await qbt.get_torrent(hash_or_short)
        if torrent is not None:
            return torrent
    return await qbt.get_torrent_by_short_hash(hash_or_short)


@router.callback_query(F.data.startswith(CallbackData.TORRENT))
async def handle_torrent_details(callback: CallbackQuery) -> None:
    """Show torrent details."""
    if not callback.message or not callback.data:
        return

    qbt = await check_qbt_enabled(callback)
    if not qbt:
        return

    try:
        hash_arg = callback.data.replace(CallbackData.TORRENT, "")
        torrent = await _resolve_torrent(qbt, hash_arg)

        if not torrent:
            await callback.answer("Торрент не найден", show_alert=True)
            return

        await _render_torrent_details(callback.message, torrent)
        await callback.answer()

    except Exception as e:
        logger.error("Failed to get torrent details", error=str(e), exc_info=True)
        await callback.answer("Ошибка операции", show_alert=True)


@router.callback_query(F.data.startswith(CallbackData.TORRENT_PAUSE))
async def handle_pause_torrent(callback: CallbackQuery) -> None:
    """Pause a torrent."""
    if not callback.data:
        return

    qbt = await check_qbt_enabled(callback)
    if not qbt:
        return

    try:
        hash_arg = callback.data.replace(CallbackData.TORRENT_PAUSE, "")
        torrent = await _resolve_torrent(qbt, hash_arg)

        if not torrent:
            await callback.answer("Торрент не найден", show_alert=True)
            return

        await qbt.pause([torrent.hash])
        await callback.answer(f"⏸️ Приостановлен: {torrent.name[:30]}")

        # BUG-15: redraw details directly — do NOT call handle_torrent_details,
        # which would ack the callback a second time.
        if callback.message:
            # PERF-01: re-fetch only this torrent (server-side ``hashes`` filter)
            # to show the updated state (speed=0, state=paused) instead of
            # pulling and parsing the whole list again. Fall back to a
            # short-hash lookup if the targeted fetch returns nothing.
            refreshed = await _refetch_one(qbt, torrent, hash_arg)
            await _render_torrent_details(callback.message, refreshed or torrent)

    except Exception as e:
        logger.error("Failed to pause", error=str(e), exc_info=True)
        await callback.answer("Ошибка операции", show_alert=True)


@router.callback_query(F.data.startswith(CallbackData.TORRENT_RESUME))
async def handle_resume_torrent(callback: CallbackQuery) -> None:
    """Resume a torrent."""
    if not callback.data:
        return

    qbt = await check_qbt_enabled(callback)
    if not qbt:
        return

    try:
        hash_arg = callback.data.replace(CallbackData.TORRENT_RESUME, "")
        torrent = await _resolve_torrent(qbt, hash_arg)

        if not torrent:
            await callback.answer("Торрент не найден", show_alert=True)
            return

        await qbt.resume([torrent.hash])
        await callback.answer(f"▶️ Возобновлён: {torrent.name[:30]}")

        # BUG-15: redraw details directly — do NOT call handle_torrent_details.
        if callback.message:
            # PERF-01: targeted single-torrent re-fetch (see _refetch_one).
            refreshed = await _refetch_one(qbt, torrent, hash_arg)
            await _render_torrent_details(callback.message, refreshed or torrent)

    except Exception as e:
        logger.error("Failed to resume", error=str(e), exc_info=True)
        await callback.answer("Ошибка операции", show_alert=True)


@router.callback_query(F.data.startswith(CallbackData.TORRENT_DELETE))
async def handle_delete_torrent(callback: CallbackQuery, is_admin: bool = False) -> None:
    """Delete a torrent (keep files)."""
    if not is_admin:
        await callback.answer("Недостаточно прав для удаления", show_alert=True)
        return

    if not callback.data or not callback.message:
        return

    qbt = await check_qbt_enabled(callback)
    if not qbt:
        return

    try:
        hash_arg = callback.data.replace(CallbackData.TORRENT_DELETE, "")
        torrent = await _resolve_torrent(qbt, hash_arg)

        if not torrent:
            await callback.answer("Торрент не найден", show_alert=True)
            return

        await qbt.delete([torrent.hash], delete_files=False)
        await callback.answer(f"🗑️ Удалён: {torrent.name[:30]}")

        # BUG-15: redraw list directly — do NOT call a callback handler.
        if callback.message:
            await _render_torrent_list(callback.message, qbt)

    except Exception as e:
        logger.error("Failed to delete", error=str(e), exc_info=True)
        await callback.answer("Ошибка операции", show_alert=True)


@router.callback_query(F.data.startswith(CallbackData.TORRENT_DELETE_FILES))
async def handle_delete_with_files(callback: CallbackQuery, is_admin: bool = False) -> None:
    """Ask for confirmation before deleting a torrent with its files.

    BUG-14/DEAD-03: deleting with files is irreversible, so this now shows
    ``Keyboards.confirm_delete_torrent(hash, with_files=True)`` instead of
    deleting immediately. The actual delete happens in
    ``handle_delete_with_files_confirm`` (``t_delfc:``).
    """
    if not is_admin:
        await callback.answer("Недостаточно прав для удаления", show_alert=True)
        return

    if not callback.data or not callback.message:
        return

    qbt = await check_qbt_enabled(callback)
    if not qbt:
        return

    try:
        hash_arg = callback.data.replace(CallbackData.TORRENT_DELETE_FILES, "")
        torrent = await _resolve_torrent(qbt, hash_arg)

        if not torrent:
            await callback.answer("Торрент не найден", show_alert=True)
            return

        await safe_edit(
            callback.message,
            f"⚠️ Удалить торрент <b>С файлами</b>?\n\n{html.escape(torrent.name)}\n\n"
            f"Это действие необратимо.",
            reply_markup=Keyboards.confirm_delete_torrent(torrent.hash, with_files=True),
            parse_mode="HTML",
        )
        await callback.answer()

    except Exception as e:
        logger.error("Failed to prepare delete-with-files confirmation", error=str(e), exc_info=True)
        await callback.answer("Ошибка операции", show_alert=True)


@router.callback_query(F.data.startswith(CallbackData.TORRENT_DELETE_FILES_CONFIRM))
async def handle_delete_with_files_confirm(callback: CallbackQuery, is_admin: bool = False) -> None:
    """Confirmed deletion of a torrent with its files (BUG-14/DEAD-03)."""
    if not is_admin:
        await callback.answer("Недостаточно прав для удаления", show_alert=True)
        return

    if not callback.data or not callback.message:
        return

    qbt = await check_qbt_enabled(callback)
    if not qbt:
        return

    try:
        hash_arg = callback.data.replace(CallbackData.TORRENT_DELETE_FILES_CONFIRM, "")
        torrent = await _resolve_torrent(qbt, hash_arg)

        if not torrent:
            await callback.answer("Торрент не найден", show_alert=True)
            return

        await qbt.delete([torrent.hash], delete_files=True)
        await callback.answer(f"🗑️💾 Удалён с файлами: {torrent.name[:25]}")

        # BUG-15: redraw list directly — do NOT call a callback handler.
        if callback.message:
            await _render_torrent_list(callback.message, qbt)

    except Exception as e:
        logger.error("Failed to delete with files", error=str(e), exc_info=True)
        await callback.answer("Ошибка операции", show_alert=True)


@router.callback_query(F.data == CallbackData.TORRENT_PAUSE_ALL)
async def handle_pause_all(callback: CallbackQuery, is_admin: bool = False) -> None:
    """Pause all torrents."""
    # SEC-06: blanket operation - admin only.
    if not is_admin:
        await callback.answer("Недостаточно прав для этой операции", show_alert=True)
        return

    qbt = await check_qbt_enabled(callback)
    if not qbt:
        return

    try:
        await qbt.pause("all")
        await callback.answer("⏸️ Все торренты приостановлены")

        # LOGIC-02/BUG-04a: render directly — calling handle_refresh here
        # would ack the callback a second time.
        if callback.message:
            await _render_torrent_list(callback.message, qbt)

    except Exception as e:
        logger.error("Failed to pause all", error=str(e), exc_info=True)
        await callback.answer("Ошибка операции", show_alert=True)


@router.callback_query(F.data == CallbackData.TORRENT_RESUME_ALL)
async def handle_resume_all(callback: CallbackQuery, is_admin: bool = False) -> None:
    """Resume all torrents."""
    # SEC-06: blanket operation - admin only.
    if not is_admin:
        await callback.answer("Недостаточно прав для этой операции", show_alert=True)
        return

    qbt = await check_qbt_enabled(callback)
    if not qbt:
        return

    try:
        await qbt.resume("all")
        await callback.answer("▶️ Все торренты возобновлены")

        # LOGIC-02/BUG-04a: render directly — see handle_pause_all.
        if callback.message:
            await _render_torrent_list(callback.message, qbt)

    except Exception as e:
        logger.error("Failed to resume all", error=str(e), exc_info=True)
        await callback.answer("Ошибка операции", show_alert=True)


@router.callback_query(F.data == CallbackData.TORRENT_BACK)
async def handle_back_to_list(callback: CallbackQuery) -> None:
    """Legacy plain "back to list" callback (from ``torrent_filters``/
    ``speed_limits_menu`` "Назад" buttons, which don't track a filter) —
    renders the unfiltered first page.
    """
    if not callback.message:
        return

    qbt = await check_qbt_enabled(callback)
    if not qbt:
        return

    try:
        await callback.answer()
        await _render_torrent_list(callback.message, qbt)
    except Exception as e:
        logger.error("Failed to render torrent list", error=str(e), exc_info=True)
        await callback.answer("Ошибка операции", show_alert=True)


@router.callback_query(F.data == CallbackData.TORRENT_CLOSE)
async def handle_close(callback: CallbackQuery) -> None:
    """Close torrent list message."""
    if callback.message:
        await callback.message.delete()
    await callback.answer()


@router.callback_query(F.data == f"{CallbackData.TORRENT_FILTER}menu")
async def handle_filter_menu(callback: CallbackQuery) -> None:
    """Show filter selection menu."""
    if not callback.message:
        return

    await safe_edit(
        callback.message,
        "<b>Выберите фильтр:</b>",
        reply_markup=Keyboards.torrent_filters(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith(CallbackData.TORRENT_FILTER))
async def handle_filter_select(callback: CallbackQuery) -> None:
    """Apply filter to torrent list."""
    if not callback.message or not callback.data:
        return

    qbt = await check_qbt_enabled(callback)
    if not qbt:
        return

    try:
        filter_value = callback.data.replace(CallbackData.TORRENT_FILTER, "")
        # DEAD-13: "menu" is intercepted by handle_filter_menu above (which is
        # registered first and matches the exact "t_filter:menu" string), so
        # this handler — matching the broader startswith prefix — never
        # actually observes filter_value == "menu" at runtime. No dead branch
        # needed here.
        filter_type = _parse_filter(filter_value)

        await callback.answer()
        await _render_torrent_list(callback.message, qbt, filter_type, 0)

    except Exception as e:
        logger.error("Filter error", error=str(e), exc_info=True)
        await callback.answer("Ошибка операции", show_alert=True)


@router.callback_query(F.data == CallbackData.SPEED_MENU)
async def handle_speed_menu(callback: CallbackQuery) -> None:
    """Show speed limits menu."""
    if not callback.message:
        return

    qbt = await check_qbt_enabled(callback)
    if not qbt:
        return

    try:
        status = await qbt.get_status()
        await _render_speed_menu(callback.message, status)
        await callback.answer()

    except Exception as e:
        logger.error("Speed menu error", error=str(e), exc_info=True)
        await callback.answer("Ошибка операции", show_alert=True)


async def _render_speed_menu(message: Message, status) -> None:
    """Render the speed-limits menu into ``message``.

    LOGIC-03: passes the qBittorrent-reported current limits into
    ``Keyboards.speed_limits_menu`` so the "✓" marker reflects the real
    setting instead of always defaulting to "unlimited".

    BUG-04a/LOGIC-02: extracted so ``handle_speed_set`` can redraw the menu
    after its own ``callback.answer`` without triggering a second one via
    ``handle_speed_menu``.
    """
    current_dl = "Без лимита" if status.download_limit == 0 else format_speed(status.download_limit)
    current_ul = "Без лимита" if status.upload_limit == 0 else format_speed(status.upload_limit)

    text = (
        f"<b>Лимиты скорости</b>\n\n"
        f"Текущие:\n"
        f"⬇️ Загрузка: {current_dl}\n"
        f"⬆️ Отдача: {current_ul}\n\n"
        f"Выберите новый лимит:"
    )

    await safe_edit(
        message,
        text,
        reply_markup=Keyboards.speed_limits_menu(status.download_limit, status.upload_limit),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith(CallbackData.SPEED_LIMIT))
async def handle_speed_set(callback: CallbackQuery) -> None:
    """Set speed limits (download or upload)."""
    if not callback.data:
        return

    qbt = await check_qbt_enabled(callback)
    if not qbt:
        return

    try:
        # Format: speed:dl:1024 or speed:ul:1024
        parts = callback.data.removeprefix(CallbackData.SPEED_LIMIT).split(":")
        if len(parts) != 2:
            return

        limit_type = parts[0]  # "dl" or "ul"
        speed_kb = int(parts[1])
        speed_bytes = speed_kb * 1024  # Convert KB/s to B/s

        if limit_type == "dl":
            await qbt.set_download_limit(speed_bytes)
        else:
            await qbt.set_upload_limit(speed_bytes)

        await callback.answer(Formatters.format_speed_limit_changed(limit_type, speed_kb))

        # BUG-04a/LOGIC-02: render directly — do NOT call handle_speed_menu,
        # which would ack the callback a second time.
        if callback.message:
            status = await qbt.get_status()
            await _render_speed_menu(callback.message, status)

    except Exception as e:
        logger.error("Speed set error", error=str(e), exc_info=True)
        await callback.answer("Ошибка операции", show_alert=True)
