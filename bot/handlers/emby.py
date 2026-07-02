"""Emby Media Server handler."""

import asyncio

import structlog
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from bot.clients.emby import EmbyError
from bot.clients.registry import get_emby
from bot.handlers.common import safe_edit
from bot.ui.formatters import Formatters
from bot.ui.keyboards import CallbackData, Keyboards
from bot.ui.menu import MENU_EMBY

logger = structlog.get_logger()
router = Router()


async def _render_status_text() -> tuple[str, InlineKeyboardMarkup | None]:
    """LOGIC-20: fetch Emby status and build (text, keyboard) — no I/O side
    effects on Telegram, so both cmd_emby and handle_refresh can call it and
    decide for themselves whether to answer()/edit_text()/send a new message.

    `keyboard` is None for the "not configured" case (message.answer/edit_text
    both accept reply_markup=None).
    """
    emby = await get_emby()
    if not emby:
        text = "❌ Emby не настроен. Добавьте <code>EMBY_URL</code> и <code>EMBY_API_KEY</code> в конфигурацию."
        return text, None

    try:
        # PERF-04: fetch server info, libraries and sessions concurrently
        # instead of three sequential round-trips. asyncio.gather (without
        # return_exceptions) preserves the original behaviour: the first
        # failure propagates and is handled by the except blocks below.
        info, libraries, sessions = await asyncio.gather(
            emby.get_server_info(),
            emby.get_libraries(),
            emby.get_sessions(),
        )

        text = Formatters.format_emby_status(
            server_name=info.server_name,
            version=info.version,
            operating_system=info.operating_system,
            has_pending_restart=info.has_pending_restart,
            has_update_available=info.has_update_available,
            active_sessions=len(sessions),
            libraries=libraries,
        )

        keyboard = Keyboards.emby_main(
            has_update=info.has_update_available,
            can_restart=info.can_self_restart,
            can_update=info.can_self_update,
        )
        return text, keyboard

    except EmbyError as e:
        logger.error("Emby status error", error=str(e.message), exc_info=True)
        return Formatters.format_error("Не удалось получить статус Emby"), None

    except Exception as e:
        logger.error("Failed to get Emby status", error=str(e), exc_info=True)
        return "❌ Ошибка получения статуса Emby", None


async def _edit_status(callback: CallbackQuery) -> None:
    """BUG-04c: re-render the status card in place WITHOUT calling
    callback.answer() — callers that already answered (e.g. with a
    "✅ Сканирование запущено" toast) must not answer a second time.
    """
    if not callback.message:
        return
    text, keyboard = await _render_status_text()
    await safe_edit(callback.message, text, reply_markup=keyboard, parse_mode="HTML")


async def show_emby_status(message_or_callback, edit: bool = False) -> None:
    """Show Emby server status.

    Kept as a thin wrapper around `_render_status_text` for callers that
    don't need fine control over `callback.answer()` (e.g. handle_restart_confirm's
    error-path re-render). New call sites should prefer calling
    `_render_status_text()` directly to keep exactly one `answer()` per callback
    (BUG-04c).
    """
    is_callback = isinstance(message_or_callback, CallbackQuery)
    text, keyboard = await _render_status_text()

    if edit and is_callback:
        await safe_edit(message_or_callback.message, text, reply_markup=keyboard, parse_mode="HTML")
    else:
        await message_or_callback.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.message(F.text == MENU_EMBY)
@router.message(Command("emby"))
async def cmd_emby(message: Message) -> None:
    """Handle /emby command."""
    text, keyboard = await _render_status_text()
    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data == CallbackData.EMBY_REFRESH)
async def handle_refresh(callback: CallbackQuery) -> None:
    """Refresh Emby status. BUG-04c: exactly one callback.answer()."""
    text, keyboard = await _render_status_text()
    if callback.message:
        await safe_edit(callback.message, text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == CallbackData.EMBY_CLOSE)
async def handle_close(callback: CallbackQuery) -> None:
    """Close Emby message."""
    if callback.message:
        await callback.message.delete()
    await callback.answer()


@router.callback_query(F.data == CallbackData.EMBY_SCAN_ALL)
async def handle_scan_all(callback: CallbackQuery) -> None:
    """Scan all libraries."""
    emby = await get_emby()
    if not emby:
        await callback.answer("Emby не настроен", show_alert=True)
        return

    try:
        await emby.scan_library()
        await callback.answer("✅ Сканирование всех библиотек запущено")
        await _edit_status(callback)

    except EmbyError as e:
        logger.error("Failed to scan all libraries", error=str(e.message), exc_info=True)
        await callback.answer("Не удалось запустить сканирование", show_alert=True)

    except Exception as e:
        logger.error("Failed to scan all libraries", error=str(e), exc_info=True)
        await callback.answer("Ошибка операции", show_alert=True)


@router.callback_query(F.data == CallbackData.EMBY_SCAN_MOVIES)
async def handle_scan_movies(callback: CallbackQuery) -> None:
    """Scan movies library."""
    emby = await get_emby()
    if not emby:
        await callback.answer("Emby не настроен", show_alert=True)
        return

    try:
        libraries = await emby.get_libraries()
        movies_lib = next((lib for lib in libraries if lib.collection_type == "movies"), None)

        if movies_lib:
            await emby.refresh_library(movies_lib.id)
            await callback.answer("✅ Сканирование фильмов запущено")
        else:
            await callback.answer("Библиотека фильмов не найдена", show_alert=True)

        await _edit_status(callback)

    except EmbyError as e:
        logger.error("Failed to scan movies library", error=str(e.message), exc_info=True)
        await callback.answer("Не удалось запустить сканирование", show_alert=True)

    except Exception as e:
        logger.error("Failed to scan movies library", error=str(e), exc_info=True)
        await callback.answer("Ошибка операции", show_alert=True)


@router.callback_query(F.data == CallbackData.EMBY_SCAN_SERIES)
async def handle_scan_series(callback: CallbackQuery) -> None:
    """Scan series library."""
    emby = await get_emby()
    if not emby:
        await callback.answer("Emby не настроен", show_alert=True)
        return

    try:
        libraries = await emby.get_libraries()
        series_lib = next((lib for lib in libraries if lib.collection_type == "tvshows"), None)

        if series_lib:
            await emby.refresh_library(series_lib.id)
            await callback.answer("✅ Сканирование сериалов запущено")
        else:
            await callback.answer("Библиотека сериалов не найдена", show_alert=True)

        await _edit_status(callback)

    except EmbyError as e:
        logger.error("Failed to scan series library", error=str(e.message), exc_info=True)
        await callback.answer("Не удалось запустить сканирование", show_alert=True)

    except Exception as e:
        logger.error("Failed to scan series library", error=str(e), exc_info=True)
        await callback.answer("Ошибка операции", show_alert=True)


@router.callback_query(F.data == CallbackData.EMBY_RESTART)
async def handle_restart_prompt(callback: CallbackQuery) -> None:
    """Show restart confirmation."""
    if not callback.message:
        return

    await callback.message.edit_text(
        "⚠️ <b>Перезагрузить Emby сервер?</b>\n\n"
        "Все активные сессии будут прерваны.",
        reply_markup=Keyboards.emby_confirm_restart(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == CallbackData.EMBY_RESTART_CONFIRM)
async def handle_restart_confirm(callback: CallbackQuery, is_admin: bool = False) -> None:
    """Confirm and restart server."""
    if not is_admin:
        await callback.answer("Недостаточно прав для перезагрузки", show_alert=True)
        return

    emby = await get_emby()
    if not emby:
        await callback.answer("Emby не настроен", show_alert=True)
        return

    try:
        await emby.restart_server()

        if callback.message:
            await callback.message.edit_text(
                "🔁 <b>Сервер перезагружается...</b>\n\n"
                "Подождите 30-60 секунд, затем используйте /emby для проверки.",
                parse_mode="HTML",
            )

        await callback.answer("Перезагрузка запущена")

    except EmbyError as e:
        logger.error("Failed to restart server", error=str(e.message), exc_info=True)
        await callback.answer("Не удалось перезагрузить сервер", show_alert=True)
        await _edit_status(callback)

    except Exception as e:
        logger.error("Failed to restart server", error=str(e), exc_info=True)
        await callback.answer("Ошибка операции", show_alert=True)
        await _edit_status(callback)


@router.callback_query(F.data == CallbackData.EMBY_UPDATE)
async def handle_update_prompt(callback: CallbackQuery) -> None:
    """Show update confirmation."""
    if not callback.message:
        return

    await callback.message.edit_text(
        "⚠️ <b>Установить обновление Emby?</b>\n\n"
        "Сервер будет перезагружен после установки.",
        reply_markup=Keyboards.emby_confirm_update(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == CallbackData.EMBY_UPDATE_CONFIRM)
async def handle_update_confirm(callback: CallbackQuery, is_admin: bool = False) -> None:
    """Confirm and install update."""
    if not is_admin:
        await callback.answer("Недостаточно прав для обновления", show_alert=True)
        return

    emby = await get_emby()
    if not emby:
        await callback.answer("Emby не настроен", show_alert=True)
        return

    try:
        await emby.install_update()

        if callback.message:
            await callback.message.edit_text(
                "⬆️ <b>Обновление устанавливается...</b>\n\n"
                "Сервер перезагрузится автоматически. "
                "Подождите несколько минут, затем используйте /emby для проверки.",
                parse_mode="HTML",
            )

        await callback.answer("Обновление запущено")

    except EmbyError as e:
        logger.error("Failed to install update", error=str(e.message), exc_info=True)
        await callback.answer("Не удалось установить обновление", show_alert=True)
        await _edit_status(callback)

    except Exception as e:
        logger.error("Failed to install update", error=str(e), exc_info=True)
        await callback.answer("Ошибка операции", show_alert=True)
        await _edit_status(callback)
