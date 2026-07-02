"""Emby-domain keyboards: main control panel, restart/update confirmations."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.ui.keyboards._constants import CallbackData


class _EmbyKeyboards:
    """Emby keyboard mixin."""

    @staticmethod
    def emby_main(
        has_update: bool = False,
        can_restart: bool = True,
        can_update: bool = True,
    ) -> InlineKeyboardMarkup:
        """Create main Emby control keyboard."""
        keyboard = []

        # Library scan buttons
        keyboard.append([
            InlineKeyboardButton(text="🔄 Обновить статус", callback_data=CallbackData.EMBY_REFRESH),
        ])

        keyboard.append([
            InlineKeyboardButton(text="📚 Сканировать всё", callback_data=CallbackData.EMBY_SCAN_ALL),
        ])

        keyboard.append([
            InlineKeyboardButton(text="🎬 Фильмы", callback_data=CallbackData.EMBY_SCAN_MOVIES),
            InlineKeyboardButton(text="📺 Сериалы", callback_data=CallbackData.EMBY_SCAN_SERIES),
        ])

        # Server control buttons
        if can_restart:
            keyboard.append([
                InlineKeyboardButton(text="🔁 Перезагрузить", callback_data=CallbackData.EMBY_RESTART),
            ])

        if can_update and has_update:
            keyboard.append([
                InlineKeyboardButton(text="⬆️ Установить обновление", callback_data=CallbackData.EMBY_UPDATE),
            ])

        keyboard.append([
            InlineKeyboardButton(text="❌ Закрыть", callback_data=CallbackData.EMBY_CLOSE),
        ])

        return InlineKeyboardMarkup(inline_keyboard=keyboard)

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
                    InlineKeyboardButton(text="❌ Отмена", callback_data=CallbackData.EMBY_REFRESH),
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
                    InlineKeyboardButton(text="❌ Отмена", callback_data=CallbackData.EMBY_REFRESH),
                ],
            ]
        )
