"""Settings-domain keyboards: quality/metadata profile & root-folder pickers,
the settings menu, resolution selection, and the auto-grab toggle.
"""

from typing import Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.models import MetadataProfile, QualityProfile, RootFolder, UserPreferences
from bot.ui.callbacks import SettingCB
from bot.ui.keyboards._constants import BTN_CLOSE, CallbackData


def grab_best_label(enabled: bool) -> str:
    """Name + state of the "⚡ Скачать лучший" setting (audit UI-12)."""
    return f"⚡ Кнопка «Скачать лучший»: {'вкл' if enabled else 'выкл'}"


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
    def settings_menu(
        lidarr_enabled: bool = False, prefs: Optional[UserPreferences] = None
    ) -> InlineKeyboardMarkup:
        """Create main settings menu keyboard.

        Rollback 2026-08-10 (Task 13): one Radarr row and one Sonarr row
        instead of a shared pair — Radarr's and Sonarr's profile/
        folder ids are independent sequences (live measurement: both start
        at 1/2 but point at different paths), so a single shared preference
        could silently apply a movie's folder/profile choice to a series.
        """
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
        # Audit 2026-09-23 (NOTIF-06): one-tap toggles; the label shows the
        # current state, the button sets the other one.
        prefs = prefs or UserPreferences()
        # Audit UI-12: the setting only shows/hides a button — "Авто-граб"
        # promised automatic grabs the bot never makes.
        rows.append([InlineKeyboardButton(
            text=grab_best_label(prefs.auto_grab_enabled), callback_data="settings:auto_grab",
        )])
        music_on = prefs.notify_music != "off"
        rows.append([InlineKeyboardButton(
            text=f"🎵 Уведомления о музыке: {'сводка' if music_on else 'выкл'}",
            callback_data=SettingCB(key="notify_music", value="off" if music_on else "summary").pack(),
        )])
        rows.append([InlineKeyboardButton(
            text=f"🎬 «В библиотеке»: {'вкл' if prefs.notify_library else 'выкл'}",
            callback_data=SettingCB(key="notify_library", value="0" if prefs.notify_library else "1").pack(),
        )])
        rows.append([InlineKeyboardButton(
            text=f"🌅 Утренняя сводка: {'вкл' if prefs.digest_enabled else 'выкл'}",
            callback_data=SettingCB(key="digest_enabled", value="0" if prefs.digest_enabled else "1").pack(),
        )])
        rows.append([InlineKeyboardButton(text=BTN_CLOSE, callback_data=CallbackData.CLOSE)])
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
        """Toggle for the "⚡ Скачать лучший" button (audit UI-12)."""
        new_value = 0 if current else 1

        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=grab_best_label(current),
                        callback_data=SettingCB(key="auto_grab_enabled", value=str(new_value)).pack(),
                    ),
                ],
                [
                    InlineKeyboardButton(text="◀️ Назад", callback_data=CallbackData.SETTINGS),
                ],
            ]
        )
