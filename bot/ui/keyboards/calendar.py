"""Calendar-domain keyboard: period selector (7/14/30 days) + refresh."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.ui.callbacks import CalCB
from bot.ui.keyboards._constants import CallbackData


class _CalendarKeyboards:
    """Calendar keyboard mixin."""

    @staticmethod
    def calendar_controls(current_days: int = 7) -> InlineKeyboardMarkup:
        """Create keyboard for calendar period selection."""
        periods = [
            ("7 дней", 7),
            ("14 дней", 14),
            ("30 дней", 30),
        ]
        buttons = []
        for label, days in periods:
            text = f"• {label} •" if days == current_days else label
            buttons.append(InlineKeyboardButton(text=text, callback_data=CalCB(days=days).pack()))

        return InlineKeyboardMarkup(
            inline_keyboard=[
                buttons,
                [InlineKeyboardButton(text="🔄 Обновить", callback_data=CallbackData.CALENDAR_REFRESH)],
            ]
        )
