"""Emby Media Server handler."""

from typing import Optional

import structlog
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot.clients.emby import EmbyClient, EmbyError, EmbyLibrary
from bot.config import get_settings
from bot.ui.formatters import Formatters
from bot.ui.keyboards import CallbackData, Keyboards

logger = structlog.get_logger()
router = Router()

# Russian menu button text
MENU_EMBY = "📺 Emby"


def get_emby_client() -> Optional[EmbyClient]:
    """Get Emby client if configured."""
    settings = get_settings()
    if not settings.emby_enabled:
        return None
    return EmbyClient(settings.emby_url, settings.emby_api_key)


async def show_emby_status(message_or_callback, edit: bool = False) -> None:
    """Show Emby server status."""
    emby = get_emby_client()
    if not emby:
        text = "❌ Emby не настроен. Добавьте EMBY\\_URL и EMBY\\_API\\_KEY в конфигурацию."
        if edit and hasattr(message_or_callback, "message"):
            await message_or_callback.message.edit_text(text, parse_mode="Markdown")
        else:
            await message_or_callback.answer(text, parse_mode="Markdown")
        return

    try:
        info = await emby.get_server_info()
        libraries = await emby.get_libraries()
        sessions = await emby.get_sessions()

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

        if edit and hasattr(message_or_callback, "message"):
            await message_or_callback.message.edit_text(
                text,
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
            await message_or_callback.answer()
        else:
            await message_or_callback.answer(
                text,
                reply_markup=keyboard,
                parse_mode="Markdown",
            )

    except EmbyError as e:
        error_text = f"❌ Ошибка Emby: {e.message}"
        if edit and hasattr(message_or_callback, "message"):
            await message_or_callback.message.edit_text(error_text)
        else:
            await message_or_callback.answer(error_text)

    except Exception as e:
        logger.error("Failed to get Emby status", error=str(e))
        error_text = f"❌ Ошибка: {str(e)}"
        if edit and hasattr(message_or_callback, "message"):
            await message_or_callback.message.edit_text(error_text)
        else:
            await message_or_callback.answer(error_text)

    finally:
        if emby:
            await emby.close()


@router.message(F.text == MENU_EMBY)
@router.message(Command("emby"))
async def cmd_emby(message: Message) -> None:
    """Handle /emby command."""
    await show_emby_status(message)


@router.callback_query(F.data == CallbackData.EMBY_REFRESH)
async def handle_refresh(callback: CallbackQuery) -> None:
    """Refresh Emby status."""
    await show_emby_status(callback, edit=True)


@router.callback_query(F.data == CallbackData.EMBY_CLOSE)
async def handle_close(callback: CallbackQuery) -> None:
    """Close Emby message."""
    if callback.message:
        await callback.message.delete()
    await callback.answer()


@router.callback_query(F.data == CallbackData.EMBY_SCAN_ALL)
async def handle_scan_all(callback: CallbackQuery) -> None:
    """Scan all libraries."""
    emby = get_emby_client()
    if not emby:
        await callback.answer("Emby не настроен", show_alert=True)
        return

    try:
        await emby.scan_library()
        await callback.answer("✅ Сканирование всех библиотек запущено")
        await show_emby_status(callback, edit=True)

    except EmbyError as e:
        await callback.answer(f"Ошибка: {e.message}", show_alert=True)

    except Exception as e:
        logger.error("Failed to scan all libraries", error=str(e))
        await callback.answer(f"Ошибка: {str(e)[:50]}", show_alert=True)

    finally:
        if emby:
            await emby.close()


@router.callback_query(F.data == CallbackData.EMBY_SCAN_MOVIES)
async def handle_scan_movies(callback: CallbackQuery) -> None:
    """Scan movies library."""
    emby = get_emby_client()
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

        await show_emby_status(callback, edit=True)

    except EmbyError as e:
        await callback.answer(f"Ошибка: {e.message}", show_alert=True)

    except Exception as e:
        logger.error("Failed to scan movies library", error=str(e))
        await callback.answer(f"Ошибка: {str(e)[:50]}", show_alert=True)

    finally:
        if emby:
            await emby.close()


@router.callback_query(F.data == CallbackData.EMBY_SCAN_SERIES)
async def handle_scan_series(callback: CallbackQuery) -> None:
    """Scan series library."""
    emby = get_emby_client()
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

        await show_emby_status(callback, edit=True)

    except EmbyError as e:
        await callback.answer(f"Ошибка: {e.message}", show_alert=True)

    except Exception as e:
        logger.error("Failed to scan series library", error=str(e))
        await callback.answer(f"Ошибка: {str(e)[:50]}", show_alert=True)

    finally:
        if emby:
            await emby.close()


@router.callback_query(F.data == CallbackData.EMBY_RESTART)
async def handle_restart_prompt(callback: CallbackQuery) -> None:
    """Show restart confirmation."""
    if not callback.message:
        return

    await callback.message.edit_text(
        "⚠️ **Перезагрузить Emby сервер?**\n\n"
        "Все активные сессии будут прерваны.",
        reply_markup=Keyboards.emby_confirm_restart(),
        parse_mode="Markdown",
    )
    await callback.answer()


@router.callback_query(F.data == CallbackData.EMBY_RESTART_CONFIRM)
async def handle_restart_confirm(callback: CallbackQuery) -> None:
    """Confirm and restart server."""
    emby = get_emby_client()
    if not emby:
        await callback.answer("Emby не настроен", show_alert=True)
        return

    try:
        await emby.restart_server()

        if callback.message:
            await callback.message.edit_text(
                "🔁 **Сервер перезагружается...**\n\n"
                "Подождите 30-60 секунд, затем используйте /emby для проверки.",
                parse_mode="Markdown",
            )

        await callback.answer("Перезагрузка запущена")

    except EmbyError as e:
        await callback.answer(f"Ошибка: {e.message}", show_alert=True)
        await show_emby_status(callback, edit=True)

    except Exception as e:
        logger.error("Failed to restart server", error=str(e))
        await callback.answer(f"Ошибка: {str(e)[:50]}", show_alert=True)
        await show_emby_status(callback, edit=True)

    finally:
        if emby:
            await emby.close()


@router.callback_query(F.data == CallbackData.EMBY_UPDATE)
async def handle_update_prompt(callback: CallbackQuery) -> None:
    """Show update confirmation."""
    if not callback.message:
        return

    await callback.message.edit_text(
        "⚠️ **Установить обновление Emby?**\n\n"
        "Сервер будет перезагружен после установки.",
        reply_markup=Keyboards.emby_confirm_update(),
        parse_mode="Markdown",
    )
    await callback.answer()


@router.callback_query(F.data == CallbackData.EMBY_UPDATE_CONFIRM)
async def handle_update_confirm(callback: CallbackQuery) -> None:
    """Confirm and install update."""
    emby = get_emby_client()
    if not emby:
        await callback.answer("Emby не настроен", show_alert=True)
        return

    try:
        await emby.install_update()

        if callback.message:
            await callback.message.edit_text(
                "⬆️ **Обновление устанавливается...**\n\n"
                "Сервер перезагрузится автоматически. "
                "Подождите несколько минут, затем используйте /emby для проверки.",
                parse_mode="Markdown",
            )

        await callback.answer("Обновление запущено")

    except EmbyError as e:
        await callback.answer(f"Ошибка: {e.message}", show_alert=True)
        await show_emby_status(callback, edit=True)

    except Exception as e:
        logger.error("Failed to install update", error=str(e))
        await callback.answer(f"Ошибка: {str(e)[:50]}", show_alert=True)
        await show_emby_status(callback, edit=True)

    finally:
        if emby:
            await emby.close()
