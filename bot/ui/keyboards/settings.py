"""Settings-domain keyboards: quality/metadata profile & root-folder pickers,
the settings menu, resolution selection, and the auto-grab toggle.
"""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.models import MetadataProfile, QualityProfile, RootFolder
from bot.ui.callbacks import SettingCB
from bot.ui.keyboards._constants import CallbackData


class _SettingsKeyboards:
    """Settings keyboard mixin."""

    @staticmethod
    def quality_profiles(profiles: list[QualityProfile], key: str) -> InlineKeyboardMarkup:
        """Create keyboard for selecting quality profile.

        ``key`` is the ``UserPreferences`` field name this picker writes
        (was a raw ``CallbackData.SET_*`` string prefix — see ``SettingCB``).
        """
        keyboard = []

        for profile in profiles:
            keyboard.append([
                InlineKeyboardButton(
                    text=profile.name,
                    callback_data=SettingCB(key=key, value=str(profile.id)).pack(),
                )
            ])

        keyboard.append([
            InlineKeyboardButton(text="◀️ Назад", callback_data=CallbackData.SETTINGS),
        ])

        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def root_folders(folders: list[RootFolder], key: str) -> InlineKeyboardMarkup:
        """Create keyboard for selecting root folder (see ``quality_profiles`` for ``key``)."""
        keyboard = []

        for folder in folders:
            label = f"{folder.path} ({folder.free_space_formatted})"
            if len(label) > 40:
                label = folder.path[:37] + "..."

            keyboard.append([
                InlineKeyboardButton(
                    text=label,
                    callback_data=SettingCB(key=key, value=str(folder.id)).pack(),
                )
            ])

        keyboard.append([
            InlineKeyboardButton(text="◀️ Назад", callback_data=CallbackData.SETTINGS),
        ])

        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def settings_menu(lidarr_enabled: bool = False) -> InlineKeyboardMarkup:
        """Create main settings menu keyboard."""
        rows = [
            [
                InlineKeyboardButton(text="🎬 Профиль Radarr", callback_data="settings:radarr_profile"),
                InlineKeyboardButton(text="📁 Папка Radarr", callback_data="settings:radarr_folder"),
            ],
            [
                InlineKeyboardButton(text="📺 Профиль Sonarr", callback_data="settings:sonarr_profile"),
                InlineKeyboardButton(text="📁 Папка Sonarr", callback_data="settings:sonarr_folder"),
            ],
        ]
        if lidarr_enabled:
            rows.append([
                InlineKeyboardButton(text="🎵 Профиль Lidarr", callback_data="settings:lidarr_profile"),
                InlineKeyboardButton(text="📁 Папка Lidarr", callback_data="settings:lidarr_folder"),
            ])
            rows.append([
                InlineKeyboardButton(text="🎧 Lidarr metadata", callback_data="settings:lidarr_meta"),
            ])
        rows.append([InlineKeyboardButton(text="🎯 Качество", callback_data="settings:resolution")])
        rows.append([InlineKeyboardButton(text="⚡ Авто-граб", callback_data="settings:auto_grab")])
        rows.append([InlineKeyboardButton(text="❌ Закрыть", callback_data=CallbackData.CANCEL)])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @staticmethod
    def metadata_profiles(profiles: list[MetadataProfile], key: str) -> InlineKeyboardMarkup:
        """Create keyboard for selecting Lidarr metadata profile (see ``quality_profiles`` for ``key``)."""
        keyboard = []
        for profile in profiles:
            keyboard.append([
                InlineKeyboardButton(
                    text=profile.name,
                    callback_data=SettingCB(key=key, value=str(profile.id)).pack(),
                )
            ])
        keyboard.append([
            InlineKeyboardButton(text="◀️ Назад", callback_data=CallbackData.SETTINGS),
        ])
        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def resolution_selection() -> InlineKeyboardMarkup:
        """Create keyboard for selecting preferred resolution."""
        resolutions = [("2160p", "2160p"), ("1080p", "1080p"), ("720p", "720p"), ("Любое", "any")]
        keyboard = []

        for i in range(0, len(resolutions), 2):
            row = []
            for label, value in resolutions[i:i + 2]:
                callback = SettingCB(key="preferred_resolution", value=value).pack()
                row.append(InlineKeyboardButton(text=label, callback_data=callback))
            keyboard.append(row)

        keyboard.append([
            InlineKeyboardButton(text="◀️ Назад", callback_data=CallbackData.SETTINGS),
        ])

        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def auto_grab_toggle(current: bool) -> InlineKeyboardMarkup:
        """Create keyboard for toggling auto-grab."""
        current_text = "ВКЛ ✓" if current else "ВЫКЛ"
        new_value = 0 if current else 1

        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"Авто-граб: {current_text}",
                        callback_data=SettingCB(key="auto_grab_enabled", value=str(new_value)).pack(),
                    ),
                ],
                [
                    InlineKeyboardButton(text="◀️ Назад", callback_data=CallbackData.SETTINGS),
                ],
            ]
        )
