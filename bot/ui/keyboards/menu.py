"""Main reply-keyboard (persistent bottom menu) builder."""

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

from bot.ui.menu import (
    MENU_CALENDAR,
    MENU_DOWNLOADS,
    MENU_EMBY,
    MENU_HISTORY,
    MENU_MUSIC,
    MENU_QSTATUS,
    MENU_SEARCH,
    MENU_SETTINGS,
    MENU_STATUS,
    MENU_TRENDING,
)


class _MenuKeyboards:
    """Main (reply) menu keyboard mixin."""

    @staticmethod
    def main_menu() -> ReplyKeyboardMarkup:
        """Create main (reply) menu keyboard with the most used commands."""
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text=MENU_SEARCH), KeyboardButton(text=MENU_MUSIC), KeyboardButton(text=MENU_TRENDING)],
                [KeyboardButton(text=MENU_CALENDAR), KeyboardButton(text=MENU_DOWNLOADS), KeyboardButton(text=MENU_QSTATUS)],
                [KeyboardButton(text=MENU_EMBY), KeyboardButton(text=MENU_STATUS), KeyboardButton(text=MENU_SETTINGS)],
                [KeyboardButton(text=MENU_HISTORY)],
            ],
            resize_keyboard=True,
            input_field_placeholder="Введите название для поиска...",
        )
