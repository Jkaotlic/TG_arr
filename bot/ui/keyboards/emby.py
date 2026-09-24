"""Emby-domain keyboards: main control panel, restart/update confirmations."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.ui.keyboards._constants import BTN_BACK, BTN_CLOSE, CallbackData


class _EmbyKeyboards:
    """Emby keyboard mixin."""

    @staticmethod
    def emby_main(
        has_update: bool = False,
        can_restart: bool = True,
        can_update: bool = True,
        is_admin: bool = False,
    ) -> InlineKeyboardMarkup:
        """Create main Emby control keyboard.

        Audit UI-16: scans, restart and update are admin-only in the handlers,
        so only an admin sees them.
        """
        keyboard = []

        keyboard.append([
            InlineKeyboardButton(text="🔄 Обновить статус", callback_data=CallbackData.EMBY_REFRESH),
        ])

        if is_admin:
            keyboard.append([
                InlineKeyboardButton(text="📚 Сканировать всё", callback_data=CallbackData.EMBY_SCAN_ALL),
            ])
            keyboard.append([
                InlineKeyboardButton(text="🎬 Фильмы", callback_data=CallbackData.EMBY_SCAN_MOVIES),
                InlineKeyboardButton(text="📺 Сериалы", callback_data=CallbackData.EMBY_SCAN_SERIES),
            ])

        # Server control buttons
        if is_admin and can_restart:
            keyboard.append([
                InlineKeyboardButton(text="🔁 Перезагрузить", callback_data=CallbackData.EMBY_RESTART),
            ])

        if is_admin and can_update and has_update:
            keyboard.append([
                InlineKeyboardButton(text="⬆️ Установить обновление", callback_data=CallbackData.EMBY_UPDATE),
            ])

        keyboard.append([
            InlineKeyboardButton(text=BTN_CLOSE, callback_data=CallbackData.CLOSE),
        ])

        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def watch_in_emby(url: str) -> InlineKeyboardMarkup:
        """E2 (improvements round 2): «▶️ Смотреть в Emby» under a library
        notice / the living grab message — a plain URL button."""
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="▶️ Смотреть в Emby", url=url),
        ]])

    @staticmethod
    def emby_confirm_restart() -> InlineKeyboardMarkup:
        """Create confirmation keyboard for server restart."""
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⚠️ Да, перезагрузить",
                        callback_data=CallbackData.EMBY_RESTART_CONFIRM,
                    ),
                ],
                [
                    InlineKeyboardButton(text=BTN_BACK, callback_data=CallbackData.EMBY_REFRESH),
                ],
            ]
        )

    @staticmethod
    def emby_confirm_update() -> InlineKeyboardMarkup:
        """Create confirmation keyboard for server update."""
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⚠️ Да, обновить",
                        callback_data=CallbackData.EMBY_UPDATE_CONFIRM,
                    ),
                ],
                [
                    InlineKeyboardButton(text=BTN_BACK, callback_data=CallbackData.EMBY_REFRESH),
                ],
            ]
        )
