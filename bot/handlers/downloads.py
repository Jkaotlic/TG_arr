"""Download management handlers for qBittorrent integration."""

import asyncio
import html
from collections.abc import Awaitable, Callable
from typing import Optional

import structlog
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot.clients.qbittorrent import QBittorrentClient, QBittorrentError
from bot.clients.registry import get_qbittorrent, get_slskd
from bot.handlers._cache import remember_lru
from bot.handlers.common import accessible_message, safe_edit, strip_command, swallow_not_modified
from bot.models import QBittorrentStatus, TorrentFilter, TorrentInfo, User
from bot.ui.callbacks import TorrentActionCB, TorrentMoreCB, TorrentPageCB
from bot.ui.formatters import Formatters
from bot.ui.formatters.torrent import format_limit_ru
from bot.ui.keyboards import CallbackData, Keyboards
from bot.ui.keyboards._constants import MSG_OLD_BUTTON
from bot.ui.menu import MENU_DOWNLOADS, MENU_QSTATUS

logger = structlog.get_logger()
router = Router()

MSG_ADMIN_ONLY = "⛔ Только для администратора"

# Per-page limit for torrent list
TORRENTS_PER_PAGE = 5

# Audit UI-09: the list view (filter, page) each user last looked at. The
# card, the filter menu and the speed menu draw their "◀️ Назад" from it, and
# mutating actions redraw the list there — instead of "all, page 1".
_views: dict[int, tuple[TorrentFilter, int]] = {}
_MAX_VIEWS = 100


def _remember_view(user_id: int, filter_type: TorrentFilter, page: int) -> None:
    remember_lru(_views, user_id, (filter_type, page), _MAX_VIEWS)


def _view_of(user_id: int) -> tuple[TorrentFilter, int]:
    return _views.get(user_id, (TorrentFilter.ALL, 0))


async def _list_status(qbt: QBittorrentClient) -> Optional[QBittorrentStatus]:
    """qBittorrent's summary for the list header (it replaced the "📊 qBit"
    menu button, 2026-09-24). Best-effort: the list must not fail with it."""
    try:
        status = await qbt.get_status()
    except Exception as e:
        logger.warning("qbit_summary_failed", error=str(e))
        return None
    return status if isinstance(status, QBittorrentStatus) else None


async def _torrents_and_status(
    qbt: QBittorrentClient, filter_type: TorrentFilter,
) -> tuple[list[TorrentInfo], Optional[QBittorrentStatus]]:
    """The (filtered) torrent list and the header summary, fetched together."""
    torrents, status = await asyncio.gather(
        qbt.get_torrents(filter_type=filter_type), _list_status(qbt),
    )
    return torrents, status


def _user_id(event: Message | CallbackQuery) -> int:
    user = getattr(event, "from_user", None)
    uid = getattr(user, "id", 0)
    return uid if isinstance(uid, int) else 0


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


async def _soulseek_section() -> str:
    """Render the Soulseek transfers block appended to /downloads.

    Music downloads never appear in qBittorrent — slskd is a separate download
    client — so without this the user has no way to see whether an album they
    queued is actually moving. Best-effort: a slskd outage must not break the
    torrent list, which is the main content of this command.
    """
    try:
        slskd = await get_slskd()
        if slskd is None:
            return ""
        transfers = await slskd.get_active_transfers()
    except Exception as e:
        logger.warning("slskd_transfers_failed", error=str(e))
        return "\n\n🎵 <i>Soulseek недоступен</i>"

    if not transfers:
        return ""

    lines = ["", "", f"🎵 <b>Soulseek ({len(transfers)})</b>"]
    for transfer in transfers[:5]:
        icon = "❌" if transfer.is_errored else "⬇️"
        name = html.escape(transfer.filename[:45])
        lines.append(
            f"{icon} {name} — {transfer.progress_percent}% · {transfer.speed_formatted}"
        )
    if len(transfers) > 5:
        lines.append(f"<i>…и ещё {len(transfers) - 5}</i>")
    return "\n".join(lines)


@router.message(F.text == MENU_DOWNLOADS)
@router.message(Command("downloads", "dl"))
async def cmd_downloads(message: Message, db_user: User, is_admin: bool = False) -> None:
    """Handle /downloads command - show active downloads."""
    qbt = await check_qbt_enabled(message)
    if not qbt:
        return

    try:
        status_msg = await message.answer("🔄 Загружаю список...")

        # One list call (sliced locally) + the qBit summary for the header.
        all_torrents, status = await _torrents_and_status(qbt, TorrentFilter.ALL)
        total = len(all_torrents)

        soulseek = await _soulseek_section()

        if not all_torrents:
            # The qBit summary lives in the list header; an empty list must
            # not lose it (final review 2026-09-24).
            summary = f"\n{Formatters.format_qbit_summary(status)}" if status is not None else ""
            await status_msg.edit_text(
                "📭 Торренты не найдены." + summary + soulseek,
                parse_mode="HTML",
            )
            return

        total_pages = max(1, (total + TORRENTS_PER_PAGE - 1) // TORRENTS_PER_PAGE)
        torrents = all_torrents[:TORRENTS_PER_PAGE]
        _remember_view(_user_id(message), TorrentFilter.ALL, 0)

        text = Formatters.format_torrent_list(torrents, 0, total_pages, TorrentFilter.ALL, total, status=status)

        await status_msg.edit_text(
            text + soulseek,
            reply_markup=Keyboards.torrent_list(torrents, 0, total_pages, TorrentFilter.ALL, is_admin=is_admin),
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
    user_id: int | None = None,
    is_admin: bool = False,
) -> None:
    """Render a (optionally filtered) page of the torrent list into ``message``.

    ``user_id`` given → that user's list view is remembered (audit UI-09).

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
    all_torrents, status = await _torrents_and_status(qbt, filter_type)
    total = len(all_torrents)
    total_pages = max(1, (total + TORRENTS_PER_PAGE - 1) // TORRENTS_PER_PAGE)
    clamped_page = max(0, min(page, total_pages - 1))
    offset = clamped_page * TORRENTS_PER_PAGE
    torrents = all_torrents[offset:offset + TORRENTS_PER_PAGE]

    text = Formatters.format_torrent_list(torrents, clamped_page, total_pages, filter_type, total, status=status)
    if user_id is not None:
        _remember_view(user_id, filter_type, clamped_page)
    await safe_edit(
        message,
        text,
        reply_markup=Keyboards.torrent_list(torrents, clamped_page, total_pages, filter_type, is_admin=is_admin),
        parse_mode="HTML",
    )


@router.callback_query(TorrentPageCB.filter())
async def handle_torrent_page(
    callback: CallbackQuery, callback_data: TorrentPageCB, is_admin: bool = False,
) -> None:
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
    message = accessible_message(callback)
    if message is None:
        return

    qbt = await check_qbt_enabled(callback)
    if not qbt:
        return

    filter_type = _parse_filter(callback_data.flt)
    requested_page = callback_data.page

    try:
        all_torrents, status = await _torrents_and_status(qbt, filter_type)
        total = len(all_torrents)
        total_pages = max(1, (total + TORRENTS_PER_PAGE - 1) // TORRENTS_PER_PAGE)

        if requested_page < 0 or requested_page >= total_pages:
            await callback.answer("Неверная страница", show_alert=True)
            return

        offset = requested_page * TORRENTS_PER_PAGE
        torrents = all_torrents[offset:offset + TORRENTS_PER_PAGE]
        text = Formatters.format_torrent_list(
            torrents, requested_page, total_pages, filter_type, total, status=status,
        )
        _remember_view(_user_id(callback), filter_type, requested_page)

        await safe_edit(
            message,
            text,
            reply_markup=Keyboards.torrent_list(
                torrents, requested_page, total_pages, filter_type, is_admin=is_admin,
            ),
            parse_mode="HTML",
        )
        await callback.answer()

    except Exception as e:
        logger.error("Pagination error", error=str(e), exc_info=True)
        await callback.answer("Ошибка операции", show_alert=True)


@router.callback_query(TorrentMoreCB.filter())
async def handle_torrent_more(
    callback: CallbackQuery, callback_data: TorrentMoreCB, is_admin: bool = False,
) -> None:
    """Open the "⋯ Ещё" panel on the same message (round 2 / D2).

    Only the keyboard is swapped — the list text stays. The view is
    remembered so pause-all / speed opened from here return to it.
    """
    message = accessible_message(callback)
    if message is None:
        await callback.answer()
        return
    filter_type = _parse_filter(callback_data.flt)
    page = max(0, callback_data.page)
    _remember_view(_user_id(callback), filter_type, page)
    try:
        await swallow_not_modified(message.edit_reply_markup(
            reply_markup=Keyboards.torrent_more(filter_type, page, is_admin=is_admin),
        ))
    except Exception as e:
        logger.error("Torrent more panel error", error=str(e), exc_info=True)
        await callback.answer("Ошибка операции", show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith(CallbackData.TORRENT_PAGE))
async def handle_legacy_page(callback: CallbackQuery) -> None:
    """Legacy ``t_page:N`` buttons from messages sent before the TorrentPageCB
    migration (LOGIC-01/TEST-08a) — these carry no filter information at all,
    so they cannot be redrawn correctly. Tell the user to refresh instead of
    silently rendering an unfiltered list.
    """
    await callback.answer(MSG_OLD_BUTTON, show_alert=True)


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
    page: int = 0,
    is_admin: bool = False,
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
        reply_markup=Keyboards.torrent_details(torrent, current_filter, page, is_admin=is_admin),
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


async def _do_view(
    callback: CallbackQuery, qbt: QBittorrentClient, torrent: TorrentInfo, _h: str, is_admin: bool = False,
) -> None:
    """Show torrent details (was ``t:<hash>``)."""
    message = accessible_message(callback)
    if message is None:
        await callback.answer()
        return
    await _render_torrent_details(message, torrent, *_view_of(_user_id(callback)), is_admin=is_admin)
    await callback.answer()


async def _do_pause(
    callback: CallbackQuery, qbt: QBittorrentClient, torrent: TorrentInfo, h: str, is_admin: bool = False,
) -> None:
    """Pause a torrent (was ``t_pause:<hash>``)."""
    await qbt.pause([torrent.hash])
    await callback.answer(f"⏸️ Приостановлен: {torrent.name[:30]}")

    # BUG-15: redraw details directly — do NOT call the view action, which
    # would ack the callback a second time.
    if (message := accessible_message(callback)) is not None:
        # PERF-01: re-fetch only this torrent (server-side ``hashes`` filter)
        # to show the updated state (speed=0, state=paused) instead of
        # pulling and parsing the whole list again. Fall back to a
        # short-hash lookup if the targeted fetch returns nothing.
        refreshed = await _refetch_one(qbt, torrent, h)
        await _render_torrent_details(
            message, refreshed or torrent, *_view_of(_user_id(callback)), is_admin=is_admin,
        )


async def _do_resume(
    callback: CallbackQuery, qbt: QBittorrentClient, torrent: TorrentInfo, h: str, is_admin: bool = False,
) -> None:
    """Resume a torrent (was ``t_resume:<hash>``)."""
    await qbt.resume([torrent.hash])
    await callback.answer(f"▶️ Возобновлён: {torrent.name[:30]}")

    # BUG-15: redraw details directly — do NOT call the view action.
    if (message := accessible_message(callback)) is not None:
        # PERF-01: targeted single-torrent re-fetch (see _refetch_one).
        refreshed = await _refetch_one(qbt, torrent, h)
        await _render_torrent_details(
            message, refreshed or torrent, *_view_of(_user_id(callback)), is_admin=is_admin,
        )


async def _do_delete(
    callback: CallbackQuery, qbt: QBittorrentClient, torrent: TorrentInfo, _h: str, is_admin: bool = False,
) -> None:
    """Ask before deleting a torrent, keeping files (audit UI-16: "🗑 Удалить"
    used to act on the first tap). The delete itself is ``_do_delc``."""
    message = accessible_message(callback)
    if message is None:
        await callback.answer()
        return
    await safe_edit(
        message,
        f"🗑 Удалить торрент?\n\n{html.escape(torrent.name)}\n\nФайлы на диске останутся.",
        reply_markup=Keyboards.confirm_delete_torrent(torrent.hash, with_files=False),
        parse_mode="HTML",
    )
    await callback.answer()


async def _do_delc(
    callback: CallbackQuery, qbt: QBittorrentClient, torrent: TorrentInfo, _h: str, is_admin: bool = False,
) -> None:
    """Confirmed delete of a torrent, keeping files."""
    await qbt.delete([torrent.hash], delete_files=False)
    await callback.answer(f"🗑️ Удалён: {torrent.name[:30]}")

    # BUG-15: redraw list directly — do NOT call a callback handler.
    if (message := accessible_message(callback)) is not None:
        await _render_torrent_list(
            message, qbt, *_view_of(_user_id(callback)), user_id=_user_id(callback), is_admin=is_admin,
        )


async def _do_delf(
    callback: CallbackQuery, qbt: QBittorrentClient, torrent: TorrentInfo, _h: str, is_admin: bool = False,
) -> None:
    """Ask for confirmation before deleting a torrent with its files (was
    ``t_delf:<hash>``).

    BUG-14/DEAD-03: deleting with files is irreversible, so this shows
    ``Keyboards.confirm_delete_torrent(hash, with_files=True)`` instead of
    deleting immediately. The actual delete happens in ``_do_delfc``.
    """
    message = accessible_message(callback)
    if message is None:
        return
    await safe_edit(
        message,
        f"⚠️ Удалить торрент <b>С файлами</b>?\n\n{html.escape(torrent.name)}\n\n"
        f"Это действие необратимо.",
        reply_markup=Keyboards.confirm_delete_torrent(torrent.hash, with_files=True),
        parse_mode="HTML",
    )
    await callback.answer()


async def _do_delfc(
    callback: CallbackQuery, qbt: QBittorrentClient, torrent: TorrentInfo, _h: str, is_admin: bool = False,
) -> None:
    """Confirmed deletion of a torrent with its files (was ``t_delfc:<hash>``,
    BUG-14/DEAD-03)."""
    await qbt.delete([torrent.hash], delete_files=True)
    await callback.answer(f"🗑️💾 Удалён с файлами: {torrent.name[:25]}")

    # BUG-15: redraw list directly — do NOT call a callback handler.
    if (message := accessible_message(callback)) is not None:
        await _render_torrent_list(
            message, qbt, *_view_of(_user_id(callback)), user_id=_user_id(callback), is_admin=is_admin,
        )


# r5: was six separate ``F.data.startswith(CallbackData.TORRENT_*)`` handlers
# parsing "<prefix><hash>" by hand (LOGIC-12) — now one TorrentActionCB
# dispatch table. ``delete``/``delf``/``delfc`` stay admin-gated exactly as
# before; ``view``/``pause``/``resume`` remain open to all allowed users.
_TORRENT_ACTIONS: dict[str, Callable[..., Awaitable[None]]] = {
    "view": _do_view,
    "pause": _do_pause,
    "resume": _do_resume,
    "delete": _do_delete,
    "delc": _do_delc,
    "delf": _do_delf,
    "delfc": _do_delfc,
}
_ADMIN_ONLY_ACTIONS = frozenset({"delete", "delc", "delf", "delfc"})


@router.callback_query(TorrentActionCB.filter())
async def handle_torrent_action(
    callback: CallbackQuery, callback_data: TorrentActionCB, is_admin: bool = False
) -> None:
    """Dispatch a per-torrent action by ``callback_data.action`` (r5/LOGIC-12)."""
    action = callback_data.action
    handler = _TORRENT_ACTIONS.get(action)
    if handler is None:
        await callback.answer("Неизвестное действие", show_alert=True)
        return

    if action in _ADMIN_ONLY_ACTIONS and not is_admin:
        await callback.answer("Недостаточно прав для удаления", show_alert=True)
        return

    qbt = await check_qbt_enabled(callback)
    if not qbt:
        return

    try:
        h = callback_data.h
        torrent = await _resolve_torrent(qbt, h)

        if not torrent:
            await callback.answer("Торрент не найден", show_alert=True)
            return

        await handler(callback, qbt, torrent, h, is_admin=is_admin)

    except Exception as e:
        logger.error("Torrent action failed", action=action, error=str(e), exc_info=True)
        await callback.answer("Ошибка операции", show_alert=True)


@router.callback_query(F.data.startswith(CallbackData.TORRENT_PAUSE))
@router.callback_query(F.data.startswith(CallbackData.TORRENT_RESUME))
@router.callback_query(F.data.startswith(CallbackData.TORRENT_DELETE_FILES_CONFIRM))
@router.callback_query(F.data.startswith(CallbackData.TORRENT_DELETE_FILES))
@router.callback_query(F.data.startswith(CallbackData.TORRENT_DELETE))
@router.callback_query(F.data.startswith(CallbackData.TORRENT))
async def handle_legacy_torrent_action(callback: CallbackQuery) -> None:
    """r5: legacy ``t:``/``t_pause:``/``t_resume:``/``t_delete:``/``t_delf:``/
    ``t_delfc:`` string buttons from messages sent before the TorrentActionCB
    migration — surface an explicit alert instead of falling through
    unhandled. Registered last (and most-specific-first among these six
    string prefixes, since e.g. "t_delete:" also starts with "t:"'s shorter
    cousins would not match here — "t:" is checked last) so it only catches
    what the typed ``TorrentActionCB.filter()`` handler above didn't already
    claim.
    """
    await callback.answer(MSG_OLD_BUTTON, show_alert=True)


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
        if (message := accessible_message(callback)) is not None:
            await _render_torrent_list(
                message, qbt, *_view_of(_user_id(callback)), user_id=_user_id(callback), is_admin=True,
            )

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
        if (message := accessible_message(callback)) is not None:
            await _render_torrent_list(
                message, qbt, *_view_of(_user_id(callback)), user_id=_user_id(callback), is_admin=True,
            )

    except Exception as e:
        logger.error("Failed to resume all", error=str(e), exc_info=True)
        await callback.answer("Ошибка операции", show_alert=True)


@router.callback_query(F.data == CallbackData.TORRENT_BACK)
@router.callback_query(F.data == CallbackData.TORRENT_REFRESH)  # audit UI-19: had no handler
async def handle_back_to_list(callback: CallbackQuery, is_admin: bool = False) -> None:
    """Legacy plain "back to list" callback (``torrent_filters``/
    ``speed_limits_menu`` "Назад" buttons sent before they carried the view) —
    renders the user's last list view (audit UI-09).
    """
    message = accessible_message(callback)
    if message is None:
        return

    qbt = await check_qbt_enabled(callback)
    if not qbt:
        return

    try:
        await callback.answer()
        await _render_torrent_list(
            message, qbt, *_view_of(_user_id(callback)), user_id=_user_id(callback), is_admin=is_admin,
        )
    except Exception as e:
        logger.error("Failed to render torrent list", error=str(e), exc_info=True)
        await callback.answer("Ошибка операции", show_alert=True)


@router.callback_query(F.data == CallbackData.TORRENT_CLOSE)
async def handle_close(callback: CallbackQuery) -> None:
    """Close torrent list message."""
    if (message := accessible_message(callback)) is not None:
        await message.delete()
    await callback.answer()


@router.callback_query(F.data == f"{CallbackData.TORRENT_FILTER}menu")
async def handle_filter_menu(callback: CallbackQuery) -> None:
    """Show filter selection menu."""
    message = accessible_message(callback)
    if message is None:
        return

    await safe_edit(
        message,
        "<b>Выберите фильтр:</b>",
        reply_markup=Keyboards.torrent_filters(*_view_of(_user_id(callback))),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith(CallbackData.TORRENT_FILTER))
async def handle_filter_select(callback: CallbackQuery, is_admin: bool = False) -> None:
    """Apply filter to torrent list."""
    message = accessible_message(callback)
    if message is None or not callback.data:
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
        await _render_torrent_list(message, qbt, filter_type, 0, user_id=_user_id(callback), is_admin=is_admin)

    except Exception as e:
        logger.error("Filter error", error=str(e), exc_info=True)
        await callback.answer("Ошибка операции", show_alert=True)


@router.callback_query(F.data == CallbackData.SPEED_MENU)
async def handle_speed_menu(callback: CallbackQuery, is_admin: bool = False) -> None:
    """Show the speed screen (admin-only: the limits are global qBit settings).

    Round 2 / D1: the screen is now the turtle toggle; the exact preset grid
    lives behind "⋯ Точные лимиты" (``handle_speed_exact``).
    """
    if not is_admin:
        await callback.answer(MSG_ADMIN_ONLY, show_alert=True)
        return
    message = accessible_message(callback)
    if message is None:
        return

    qbt = await check_qbt_enabled(callback)
    if not qbt:
        return

    try:
        await _render_speed_screen(message, qbt, _user_id(callback))
        await callback.answer()

    except Exception as e:
        logger.error("Speed menu error", error=str(e), exc_info=True)
        await callback.answer("Ошибка операции", show_alert=True)


async def _render_speed_screen(message: Message, qbt: QBittorrentClient, user_id: int = 0) -> None:
    """Render the speed screen: current transfer speeds, the turtle limits and
    whether turtle mode is on — three independent reads fetched together.

    Does NOT answer the callback — the caller owns the single ack (BUG-15).
    """
    transfer, turtle_on, (alt_dl, alt_ul) = await asyncio.gather(
        qbt.get_transfer_info(), qbt.get_speed_limits_mode(), qbt.get_alt_speed_limits(),
    )
    text = Formatters.format_speed_screen(
        dl_speed=int(transfer.get("dl_info_speed", 0) or 0),
        ul_speed=int(transfer.get("up_info_speed", 0) or 0),
        alt_dl=alt_dl,
        alt_ul=alt_ul,
    )
    await safe_edit(
        message,
        text,
        reply_markup=Keyboards.speed_screen(turtle_on, *_view_of(user_id)),
        parse_mode="HTML",
    )


#: qBittorrent's out-of-the-box turtle limits (10 KiB/s both ways) — they all
#: but stop traffic, and nobody chose them (fix round 1). Bytes/s.
_FACTORY_ALT_LIMITS = (10240, 10240)
#: What the bot sets instead the first time the turtle is switched on.
_DEFAULT_ALT_LIMITS = (2 * 1024 * 1024, 512 * 1024)


@router.callback_query(F.data == CallbackData.SPEED_TURTLE)
async def handle_speed_turtle(callback: CallbackQuery, is_admin: bool = False) -> None:
    """Toggle qBit's alternative ("turtle") speed limits and redraw the screen
    with the state qBit reports afterwards (not an assumed flip).

    Fix round 1: turning it ON with qBit's factory 10 KiB/s limits first sets
    usable ones (2 МБ/с · 512 КБ/с) and says so; custom limits are kept, and
    turning it OFF never touches them. The callback is answered exactly once.
    """
    if not is_admin:
        await callback.answer(MSG_ADMIN_ONLY, show_alert=True)
        return

    qbt = await check_qbt_enabled(callback)
    if not qbt:
        return

    try:
        was_on, alt = await asyncio.gather(qbt.get_speed_limits_mode(), qbt.get_alt_speed_limits())
        if not was_on and tuple(alt) == _FACTORY_ALT_LIMITS:
            await qbt.set_alt_speed_limits(*_DEFAULT_ALT_LIMITS)
            alt = _DEFAULT_ALT_LIMITS
        await qbt.toggle_speed_limits_mode()
        turtle_on = await qbt.get_speed_limits_mode()
    except Exception as e:
        logger.error("Speed turtle toggle error", error=str(e), exc_info=True)
        await callback.answer("Ошибка операции", show_alert=True)
        return

    if turtle_on:
        answer = f"🐢 Черепаха включена: ⬇️ {format_limit_ru(alt[0])} · ⬆️ {format_limit_ru(alt[1])}"
    else:
        answer = "🐢 Черепаха выключена"
    await callback.answer(answer)

    if (message := accessible_message(callback)) is not None:
        try:
            await _render_speed_screen(message, qbt, _user_id(callback))
        except Exception as e:
            # Already answered — a failed redraw is only logged (no 2nd ack).
            logger.error("Speed screen redraw error", error=str(e), exc_info=True)


async def _turtle_on(qbt: QBittorrentClient) -> bool:
    """Best-effort turtle state for the exact-limits grid note."""
    try:
        return (await qbt.get_speed_limits_mode()) is True
    except Exception as e:
        logger.warning("speed_limits_mode_failed", error=str(e))
        return False


@router.callback_query(F.data == CallbackData.SPEED_EXACT)
async def handle_speed_exact(callback: CallbackQuery, is_admin: bool = False) -> None:
    """Show the exact speed-limit preset grid (behaviour unchanged, admin-only)."""
    if not is_admin:
        await callback.answer(MSG_ADMIN_ONLY, show_alert=True)
        return
    message = accessible_message(callback)
    if message is None:
        return

    qbt = await check_qbt_enabled(callback)
    if not qbt:
        return

    try:
        status, turtle_on = await asyncio.gather(qbt.get_status(), _turtle_on(qbt))
        await _render_speed_menu(message, status, turtle_on)
        await callback.answer()

    except Exception as e:
        logger.error("Speed limits grid error", error=str(e), exc_info=True)
        await callback.answer("Ошибка операции", show_alert=True)


async def _render_speed_menu(message: Message, status, turtle_on: bool = False) -> None:
    """Render the exact speed-limits preset grid into ``message``.

    LOGIC-03: passes the qBittorrent-reported current limits into
    ``Keyboards.speed_limits_menu`` so the "✓" marker reflects the real
    setting instead of always defaulting to "unlimited".

    BUG-04a/LOGIC-02: extracted so ``handle_speed_set`` can redraw the menu
    after its own ``callback.answer`` without triggering a second one.

    Fix round 1: with the turtle on, these (global) limits wait for it to be
    switched off — the grid says so.
    """
    turtle_note = "🐢 включена — эти лимиты действуют после её выключения\n" if turtle_on else ""
    text = (
        f"<b>Лимиты скорости</b>\n\n"
        f"{turtle_note}"
        f"Текущие:\n"
        f"⬇️ Загрузка: {format_limit_ru(status.download_limit)}\n"
        f"⬆️ Отдача: {format_limit_ru(status.upload_limit)}\n\n"
        f"Выберите новый лимит:"
    )

    await safe_edit(
        message,
        text,
        reply_markup=Keyboards.speed_limits_menu(status.download_limit, status.upload_limit),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith(CallbackData.SPEED_LIMIT))
async def handle_speed_set(callback: CallbackQuery, is_admin: bool = False) -> None:
    """Set speed limits (download or upload). Admin-only, like the menu."""
    if not is_admin:
        await callback.answer(MSG_ADMIN_ONLY, show_alert=True)
        return
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
        if (message := accessible_message(callback)) is not None:
            status, turtle_on = await asyncio.gather(qbt.get_status(), _turtle_on(qbt))
            await _render_speed_menu(message, status, turtle_on)

    except Exception as e:
        logger.error("Speed set error", error=str(e), exc_info=True)
        await callback.answer("Ошибка операции", show_alert=True)
