"""Music (Lidarr/Deezer artist lookup) keyboards."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.models import ArtistInfo
from bot.ui.callbacks import ArtistCB
from bot.ui.keyboards._constants import CallbackData


class _MusicKeyboards:
    """Artist lookup / details keyboard mixin."""

    @staticmethod
    def artist_list(
        artists: list[ArtistInfo],
        current_page: int = 0,
        per_page: int = 5,
    ) -> InlineKeyboardMarkup:
        """Create keyboard for artist selection from lookup results."""
        total_pages = max(1, (len(artists) + per_page - 1) // per_page)
        start_idx = current_page * per_page
        page_artists = artists[start_idx:start_idx + per_page]

        keyboard = []
        for i, a in enumerate(page_artists):
            idx = start_idx + i
            disamb = f" [{a.disambiguation}]" if a.disambiguation else ""
            label = f"{a.name}{disamb}"
            if len(label) > 40:
                label = label[:37] + "..."
            keyboard.append([
                InlineKeyboardButton(
                    text=label,
                    callback_data=ArtistCB(idx=idx).pack(),
                )
            ])

        if total_pages > 1:
            nav_buttons = []
            if current_page > 0:
                nav_buttons.append(
                    InlineKeyboardButton(text="◀️", callback_data=f"{CallbackData.ARTIST_PAGE}{current_page - 1}")
                )
            nav_buttons.append(
                InlineKeyboardButton(text=f"{current_page + 1}/{total_pages}", callback_data="noop")
            )
            if current_page < total_pages - 1:
                nav_buttons.append(
                    InlineKeyboardButton(text="▶️", callback_data=f"{CallbackData.ARTIST_PAGE}{current_page + 1}")
                )
            keyboard.append(nav_buttons)

        keyboard.append([
            InlineKeyboardButton(text="❌ Отмена", callback_data=CallbackData.CANCEL),
        ])
        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def artist_details(artist: ArtistInfo, already_in_library: bool = False) -> InlineKeyboardMarkup:
        """Create keyboard for artist details (add/search)."""
        keyboard = []
        if already_in_library:
            keyboard.append([
                InlineKeyboardButton(text="🔍 Запустить поиск", callback_data=CallbackData.CONFIRM_GRAB),
            ])
        else:
            keyboard.append([
                InlineKeyboardButton(text="➕ Добавить и искать", callback_data=CallbackData.CONFIRM_GRAB),
            ])
        keyboard.append([
            # LOGIC-24: dedicated music-back so search.handle_back doesn't reply
            # "сессия истекла" on a music session (which has no .results).
            InlineKeyboardButton(text="◀️ Назад", callback_data=CallbackData.MUSIC_BACK),
            InlineKeyboardButton(text="❌ Отмена", callback_data=CallbackData.CANCEL),
        ])
        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def slskd_results(results: list, per_page: int = 5) -> InlineKeyboardMarkup:
        """Keyboard for picking one Soulseek candidate (see `SlskdCB`)."""
        from bot.ui.callbacks import SlskdCB

        keyboard = []
        for idx, result in enumerate(results[:per_page]):
            fmt = result.dominant_format.upper() or "?"
            label = f"{idx + 1}. {fmt} · {result.track_count} трек. · {result.size_formatted}"
            if len(label) > 60:
                label = label[:57] + "..."
            keyboard.append([
                InlineKeyboardButton(text=label, callback_data=SlskdCB(idx=idx).pack())
            ])

        keyboard.append([
            InlineKeyboardButton(text="❌ Отмена", callback_data=CallbackData.CANCEL),
        ])
        return InlineKeyboardMarkup(inline_keyboard=keyboard)
