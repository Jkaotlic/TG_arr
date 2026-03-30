"""Download management handlers for qBittorrent integration."""

import structlog
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot.clients.qbittorrent import QBittorrentError
from bot.clients.registry import get_qbittorrent
from bot.models import TorrentFilter, User, format_speed
from bot.ui.formatters import Formatters
from bot.ui.keyboards import Keyboards

logger = structlog.get_logger()
router = Router()

# Per-page limit for torrent list
TORRENTS_PER_PAGE = 5

# Russian menu button texts
MENU_DOWNLOADS = "📥 Загрузки"
MENU_QSTATUS = "📊 qBit"


async def check_qbt_enabled(message_or_callback: Message | CallbackQuery) -> bool:
    """Check if qBittorrent is enabled and send message if not."""
    if await get_qbittorrent() is None:
        text = "⚠️ Интеграция с qBittorrent не настроена.\n\nУстановите <code>QBITTORRENT_URL</code> и <code>QBITTORRENT_PASSWORD</code> в переменных окружения."
        if isinstance(message_or_callback, Message):
            await message_or_callback.answer(text)
        elif isinstance(message_or_callback, CallbackQuery):
            await message_or_callback.answer("qBittorrent не настроен", show_alert=True)
        return False
    return True


# ============================================================================
# Commands
# ============================================================================


@router.message(F.text == MENU_DOWNLOADS)
@router.message(Command("downloads", "dl"))
async def cmd_downloads(message: Message, db_user: User) -> None:
    """Handle /downloads command - show active downloads."""
    if not await check_qbt_enabled(message):
        return

    qbt = await get_qbittorrent()
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
        logger.error("qBittorrent error", error=str(e))
        await message.answer(f"❌ Ошибка qBittorrent: {e.message}")
    except Exception as e:
        logger.error("Failed to get downloads", error=str(e))
        await message.answer(f"❌ Ошибка: {str(e)}")


@router.message(F.text == MENU_QSTATUS)
@router.message(Command("qstatus"))
async def cmd_qstatus(message: Message, db_user: User) -> None:
    """Handle /qstatus command - show qBittorrent status."""
    if not await check_qbt_enabled(message):
        return

    qbt = await get_qbittorrent()
    if not qbt:
        return

    try:
        status_msg = await message.answer("🔄 Загружаю статус qBittorrent...")

        status = await qbt.get_status()
        text = Formatters.format_qbittorrent_status(status)

        await status_msg.edit_text(text, parse_mode="HTML")

    except QBittorrentError as e:
        logger.error("qBittorrent error", error=str(e))
        await message.answer(f"❌ Ошибка qBittorrent: {e.message}")
    except Exception as e:
        logger.error("Failed to get qBittorrent status", error=str(e))
        await message.answer(f"❌ Ошибка: {str(e)}")


@router.message(Command("pause"))
async def cmd_pause(message: Message, db_user: User) -> None:
    """Handle /pause command - pause torrents."""
    if not await check_qbt_enabled(message):
        return

    qbt = await get_qbittorrent()
    if not qbt:
        return

    try:
        args = message.text.replace("/pause", "").strip() if message.text else ""

        if args.lower() == "all" or not args:
            await qbt.pause("all")
            await message.answer("⏸️ Все торренты приостановлены.")
        else:
            # Try to find torrent by partial hash
            torrent = await qbt.get_torrent_by_short_hash(args)
            if torrent:
                await qbt.pause([torrent.hash])
                await message.answer(f"⏸️ Приостановлен: {torrent.name}")
            else:
                await message.answer(f"❌ Торрент не найден: {args}")

    except QBittorrentError as e:
        await message.answer(f"❌ Ошибка: {e.message}")


@router.message(Command("resume"))
async def cmd_resume(message: Message, db_user: User) -> None:
    """Handle /resume command - resume torrents."""
    if not await check_qbt_enabled(message):
        return

    qbt = await get_qbittorrent()
    if not qbt:
        return

    try:
        args = message.text.replace("/resume", "").strip() if message.text else ""

        if args.lower() == "all" or not args:
            await qbt.resume("all")
            await message.answer("▶️ Все торренты возобновлены.")
        else:
            torrent = await qbt.get_torrent_by_short_hash(args)
            if torrent:
                await qbt.resume([torrent.hash])
                await message.answer(f"▶️ Возобновлён: {torrent.name}")
            else:
                await message.answer(f"❌ Торрент не найден: {args}")

    except QBittorrentError as e:
        await message.answer(f"❌ Ошибка: {e.message}")


# ============================================================================
# Callback handlers
# ============================================================================


@router.callback_query(F.data == "t_refresh")
async def handle_refresh(callback: CallbackQuery) -> None:
    """Refresh torrent list."""
    if not callback.message:
        return

    qbt = await get_qbittorrent()
    if not qbt:
        await callback.answer("qBittorrent не настроен", show_alert=True)
        return

    try:
        await callback.answer("Обновляю...")

        # Single API call
        all_torrents = await qbt.get_torrents()
        total = len(all_torrents)
        total_pages = max(1, (total + TORRENTS_PER_PAGE - 1) // TORRENTS_PER_PAGE)
        torrents = all_torrents[:TORRENTS_PER_PAGE]

        text = Formatters.format_torrent_list(torrents, 0, total_pages, TorrentFilter.ALL, total)

        try:
            await callback.message.edit_text(
                text,
                reply_markup=Keyboards.torrent_list(torrents, 0, total_pages, TorrentFilter.ALL),
                parse_mode="HTML",
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise

    except Exception as e:
        logger.error("Failed to refresh", error=str(e))
        await callback.answer(f"Ошибка: {str(e)[:50]}", show_alert=True)


@router.callback_query(F.data.startswith("t_page:"))
async def handle_page(callback: CallbackQuery) -> None:
    """Handle pagination."""
    if not callback.message or not callback.data:
        return

    qbt = await get_qbittorrent()
    if not qbt:
        return

    try:
        page = int(callback.data.replace("t_page:", ""))

        all_torrents = await qbt.get_torrents()
        total = len(all_torrents)
        total_pages = max(1, (total + TORRENTS_PER_PAGE - 1) // TORRENTS_PER_PAGE)

        if page < 0 or page >= total_pages:
            await callback.answer("Неверная страница", show_alert=True)
            return

        offset = page * TORRENTS_PER_PAGE
        torrents = all_torrents[offset:offset + TORRENTS_PER_PAGE]

        text = Formatters.format_torrent_list(torrents, page, total_pages, TorrentFilter.ALL, total)

        try:
            await callback.message.edit_text(
                text,
                reply_markup=Keyboards.torrent_list(torrents, page, total_pages, TorrentFilter.ALL),
                parse_mode="HTML",
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise
        await callback.answer()

    except Exception as e:
        logger.error("Pagination error", error=str(e))
        await callback.answer(f"Ошибка: {str(e)[:50]}", show_alert=True)


@router.callback_query(F.data.startswith("t:"))
async def handle_torrent_details(callback: CallbackQuery) -> None:
    """Show torrent details."""
    if not callback.message or not callback.data:
        return

    qbt = await get_qbittorrent()
    if not qbt:
        return

    try:
        short_hash = callback.data.replace("t:", "")
        torrent = await qbt.get_torrent_by_short_hash(short_hash)

        if not torrent:
            await callback.answer("Торрент не найден", show_alert=True)
            return

        text = Formatters.format_torrent_details(torrent)

        try:
            await callback.message.edit_text(
                text,
                reply_markup=Keyboards.torrent_details(torrent),
                parse_mode="HTML",
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise
        await callback.answer()

    except Exception as e:
        logger.error("Failed to get torrent details", error=str(e))
        await callback.answer(f"Ошибка: {str(e)[:50]}", show_alert=True)


@router.callback_query(F.data.startswith("t_pause:"))
async def handle_pause_torrent(callback: CallbackQuery) -> None:
    """Pause a torrent."""
    if not callback.data:
        return

    qbt = await get_qbittorrent()
    if not qbt:
        return

    try:
        short_hash = callback.data.replace("t_pause:", "")
        torrent = await qbt.get_torrent_by_short_hash(short_hash)

        if not torrent:
            await callback.answer("Торрент не найден", show_alert=True)
            return

        await qbt.pause([torrent.hash])
        await callback.answer(f"⏸️ Приостановлен: {torrent.name[:30]}")

        # Refresh the view
        await handle_torrent_details(callback)

    except Exception as e:
        logger.error("Failed to pause", error=str(e))
        await callback.answer(f"Ошибка: {str(e)[:50]}", show_alert=True)


@router.callback_query(F.data.startswith("t_resume:"))
async def handle_resume_torrent(callback: CallbackQuery) -> None:
    """Resume a torrent."""
    if not callback.data:
        return

    qbt = await get_qbittorrent()
    if not qbt:
        return

    try:
        short_hash = callback.data.replace("t_resume:", "")
        torrent = await qbt.get_torrent_by_short_hash(short_hash)

        if not torrent:
            await callback.answer("Торрент не найден", show_alert=True)
            return

        await qbt.resume([torrent.hash])
        await callback.answer(f"▶️ Возобновлён: {torrent.name[:30]}")

        # Refresh the view
        await handle_torrent_details(callback)

    except Exception as e:
        logger.error("Failed to resume", error=str(e))
        await callback.answer(f"Ошибка: {str(e)[:50]}", show_alert=True)


@router.callback_query(F.data.startswith("t_delete:"))
async def handle_delete_torrent(callback: CallbackQuery) -> None:
    """Delete a torrent (keep files)."""
    if not callback.data or not callback.message:
        return

    qbt = await get_qbittorrent()
    if not qbt:
        return

    try:
        short_hash = callback.data.replace("t_delete:", "")
        torrent = await qbt.get_torrent_by_short_hash(short_hash)

        if not torrent:
            await callback.answer("Торрент не найден", show_alert=True)
            return

        await qbt.delete([torrent.hash], delete_files=False)
        await callback.answer(f"🗑️ Удалён: {torrent.name[:30]}")

        # Go back to list
        await handle_refresh(callback)

    except Exception as e:
        logger.error("Failed to delete", error=str(e))
        await callback.answer(f"Ошибка: {str(e)[:50]}", show_alert=True)


@router.callback_query(F.data.startswith("t_delf:"))
async def handle_delete_with_files(callback: CallbackQuery) -> None:
    """Delete a torrent with files."""
    if not callback.data or not callback.message:
        return

    qbt = await get_qbittorrent()
    if not qbt:
        return

    try:
        short_hash = callback.data.replace("t_delf:", "")
        torrent = await qbt.get_torrent_by_short_hash(short_hash)

        if not torrent:
            await callback.answer("Торрент не найден", show_alert=True)
            return

        await qbt.delete([torrent.hash], delete_files=True)
        await callback.answer(f"🗑️💾 Удалён с файлами: {torrent.name[:25]}")

        # Go back to list
        await handle_refresh(callback)

    except Exception as e:
        logger.error("Failed to delete", error=str(e))
        await callback.answer(f"Ошибка: {str(e)[:50]}", show_alert=True)


@router.callback_query(F.data.startswith("t_recheck:"))
async def handle_recheck(callback: CallbackQuery) -> None:
    """Force recheck a torrent."""
    if not callback.data:
        return

    qbt = await get_qbittorrent()
    if not qbt:
        return

    try:
        short_hash = callback.data.replace("t_recheck:", "")
        torrent = await qbt.get_torrent_by_short_hash(short_hash)

        if not torrent:
            await callback.answer("Торрент не найден", show_alert=True)
            return

        await qbt.recheck([torrent.hash])
        await callback.answer(f"🔍 Перепроверка: {torrent.name[:30]}")

    except Exception as e:
        logger.error("Failed to recheck", error=str(e))
        await callback.answer(f"Ошибка: {str(e)[:50]}", show_alert=True)


@router.callback_query(F.data.startswith("t_prio:"))
async def handle_priority(callback: CallbackQuery) -> None:
    """Change torrent priority."""
    if not callback.data:
        return

    qbt = await get_qbittorrent()
    if not qbt:
        return

    try:
        parts = callback.data.replace("t_prio:", "").split(":")
        if len(parts) != 2:
            return

        short_hash, priority = parts
        torrent = await qbt.get_torrent_by_short_hash(short_hash)

        if not torrent:
            await callback.answer("Торрент не найден", show_alert=True)
            return

        if priority == "max":
            await qbt.set_priority_top([torrent.hash])
            await callback.answer(f"⬆️ Макс. приоритет: {torrent.name[:25]}")
        elif priority == "min":
            await qbt.set_priority_bottom([torrent.hash])
            await callback.answer(f"⬇️ Мин. приоритет: {torrent.name[:25]}")
        else:
            await callback.answer("Неверный приоритет", show_alert=True)
            return

    except Exception as e:
        logger.error("Failed to set priority", error=str(e))
        await callback.answer(f"Ошибка: {str(e)[:50]}", show_alert=True)


@router.callback_query(F.data == "t_pause_all")
async def handle_pause_all(callback: CallbackQuery) -> None:
    """Pause all torrents."""
    qbt = await get_qbittorrent()
    if not qbt:
        return

    try:
        await qbt.pause("all")
        await callback.answer("⏸️ Все торренты приостановлены")
        await handle_refresh(callback)

    except Exception as e:
        logger.error("Failed to pause all", error=str(e))
        await callback.answer(f"Ошибка: {str(e)[:50]}", show_alert=True)


@router.callback_query(F.data == "t_resume_all")
async def handle_resume_all(callback: CallbackQuery) -> None:
    """Resume all torrents."""
    qbt = await get_qbittorrent()
    if not qbt:
        return

    try:
        await qbt.resume("all")
        await callback.answer("▶️ Все торренты возобновлены")
        await handle_refresh(callback)

    except Exception as e:
        logger.error("Failed to resume all", error=str(e))
        await callback.answer(f"Ошибка: {str(e)[:50]}", show_alert=True)


@router.callback_query(F.data == "t_back")
async def handle_back_to_list(callback: CallbackQuery) -> None:
    """Go back to torrent list."""
    await handle_refresh(callback)


@router.callback_query(F.data == "t_close")
async def handle_close(callback: CallbackQuery) -> None:
    """Close torrent list message."""
    if callback.message:
        await callback.message.delete()
    await callback.answer()


@router.callback_query(F.data == "t_filter:menu")
async def handle_filter_menu(callback: CallbackQuery) -> None:
    """Show filter selection menu."""
    if not callback.message:
        return

    try:
        await callback.message.edit_text(
            "<b>Выберите фильтр:</b>",
            reply_markup=Keyboards.torrent_filters(),
            parse_mode="HTML",
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    await callback.answer()


@router.callback_query(F.data.startswith("t_filter:"))
async def handle_filter_select(callback: CallbackQuery) -> None:
    """Apply filter to torrent list."""
    if not callback.message or not callback.data:
        return

    qbt = await get_qbittorrent()
    if not qbt:
        return

    try:
        filter_value = callback.data.replace("t_filter:", "")
        if filter_value == "menu":
            await callback.answer()
            return

        try:
            filter_type = TorrentFilter(filter_value)
        except ValueError:
            filter_type = TorrentFilter.ALL

        # Single API call
        all_filtered = await qbt.get_torrents(filter_type=filter_type)
        total = len(all_filtered)
        total_pages = max(1, (total + TORRENTS_PER_PAGE - 1) // TORRENTS_PER_PAGE)
        torrents = all_filtered[:TORRENTS_PER_PAGE]

        text = Formatters.format_torrent_list(torrents, 0, total_pages, filter_type, total)

        try:
            await callback.message.edit_text(
                text,
                reply_markup=Keyboards.torrent_list(torrents, 0, total_pages, filter_type),
                parse_mode="HTML",
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise
        await callback.answer()

    except Exception as e:
        logger.error("Filter error", error=str(e))
        await callback.answer(f"Ошибка: {str(e)[:50]}", show_alert=True)


@router.callback_query(F.data == "speed_menu")
async def handle_speed_menu(callback: CallbackQuery) -> None:
    """Show speed limits menu."""
    if not callback.message:
        return

    qbt = await get_qbittorrent()
    if not qbt:
        return

    try:
        status = await qbt.get_status()

        current_dl = "Без лимита" if status.download_limit == 0 else format_speed(status.download_limit)
        current_ul = "Без лимита" if status.upload_limit == 0 else format_speed(status.upload_limit)

        text = (
            f"<b>Лимиты скорости</b>\n\n"
            f"Текущие:\n"
            f"⬇️ Загрузка: {current_dl}\n"
            f"⬆️ Отдача: {current_ul}\n\n"
            f"Выберите новый лимит:"
        )

        try:
            await callback.message.edit_text(
                text,
                reply_markup=Keyboards.speed_limits_menu(),
                parse_mode="HTML",
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise
        await callback.answer()

    except Exception as e:
        logger.error("Speed menu error", error=str(e))
        await callback.answer(f"Ошибка: {str(e)[:50]}", show_alert=True)


@router.callback_query(F.data.startswith("speed:"))
async def handle_speed_set(callback: CallbackQuery) -> None:
    """Set speed limits (download or upload)."""
    if not callback.data:
        return

    qbt = await get_qbittorrent()
    if not qbt:
        return

    try:
        # Format: speed:dl:1024 or speed:ul:1024
        parts = callback.data.replace("speed:", "").split(":")
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

        # Refresh speed menu
        await handle_speed_menu(callback)

    except Exception as e:
        logger.error("Speed set error", error=str(e))
        await callback.answer(f"Ошибка: {str(e)[:50]}", show_alert=True)
