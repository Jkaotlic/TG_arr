"""Last-resort callback handler (audit 2026-09-23 UI-19).

A button whose callback_data no handler knows — typically from a message sent
by an older bot version — used to spin until Telegram's timeout. This router
is included LAST, so it only sees what every other router declined.
"""

import structlog
from aiogram import Router
from aiogram.types import CallbackQuery

from bot.ui.keyboards._constants import MSG_OLD_BUTTON

logger = structlog.get_logger()
router = Router()


@router.callback_query()
async def handle_unknown_callback(callback: CallbackQuery) -> None:
    """Answer an unknown/legacy button so its spinner stops."""
    logger.info("unknown_callback", data=(callback.data or "")[:64])
    await callback.answer(MSG_OLD_BUTTON, show_alert=True)
