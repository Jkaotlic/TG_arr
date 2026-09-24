"""Main reply keyboard (persistent bottom menu) and its "⋯ Ещё" panel."""

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from bot.ui.callbacks import MoreCB
from bot.ui.menu import (
    MENU_CALENDAR,
    MENU_DOWNLOADS,
    MENU_EMBY,
    MENU_HISTORY,
    MENU_MORE,
    MENU_MUSIC,
    MENU_SEARCH,
    MENU_SETTINGS,
    MENU_STATUS,
    MENU_TORRSERVER,
    MENU_TRENDING,
)

#: "⋯ Ещё" panel rows: (button text, MoreCB key). The key names the handler
#: the old reply button of the same text used (bot/handlers/start.py).
MORE_ROWS: list[list[tuple[str, str]]] = [
    [(MENU_TRENDING, "trending"), (MENU_CALENDAR, "calendar")],
    [(MENU_TORRSERVER, "watch"), (MENU_EMBY, "emby")],
    [(MENU_HISTORY, "history"), (MENU_STATUS, "status")],
    [(MENU_SETTINGS, "settings")],
]


class _MenuKeyboards:
    """Main (reply) menu keyboard mixin."""

    @staticmethod
    def main_menu() -> ReplyKeyboardMarkup:
        """Four everyday buttons; the rest is one tap away under "⋯ Ещё".

        2026-09-24, user request («куча кнопок и все в кучу»): the old
        11-button keyboard is gone. 📊 qBit's summary lives in the header of
        📥 Загрузки now (``/qstatus`` still works).
        """
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text=MENU_SEARCH), KeyboardButton(text=MENU_MUSIC)],
                [KeyboardButton(text=MENU_DOWNLOADS), KeyboardButton(text=MENU_MORE)],
            ],
            resize_keyboard=True,
            input_field_placeholder="Введите название для поиска...",
        )

    @staticmethod
    def more_menu() -> InlineKeyboardMarkup:
        """The "⋯ Ещё" inline panel."""
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=MoreCB(key=key).pack()) for text, key in row]
            for row in MORE_ROWS
        ])
