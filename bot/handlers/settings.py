"""Settings handlers.

LOGIC-05: the original module had 7 near-identical menu/set handler pairs
(Radarr/Sonarr profile+folder, Lidarr profile+meta+folder) — ~300 lines of
copy-pasted template differing only in the CallbackData prefix, the
AddService getter, the keyboard builder, and which UserPreferences field to
write. They are now driven by the ``_SETTINGS_MAP`` table plus two generic
handlers (``handle_settings_menu`` / ``handle_settings_set``). Resolution and
auto-grab keep dedicated handlers (different shape: no *arr API call), but
now go through the same render helper and point-update as everything else.

r5: the "set:*" value picks (``set:rp:``/``rf:``/``sp:``/``sf:``/``lp:``/
``lm:``/``lf:``/``res:``/``ag:``) are now the typed ``SettingCB`` — ``key``
replaces the old per-setting string prefix (it *is* the UserPreferences
field name), so the dispatch table's ``pref_key`` doubles as the callback
key with no separate prefix to keep in sync.
"""

from dataclasses import dataclass
from typing import Awaitable, Callable

import structlog
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot.clients.registry import (
    get_lidarr,
    get_prowlarr,
    get_qbittorrent,
    get_radarr,
    get_sonarr,
)
from bot.config import get_settings
from bot.db import Database
from bot.models import User
from bot.services.add_service import AddService
from bot.ui.callbacks import SettingCB
from bot.ui.formatters import Formatters
from bot.ui.keyboards import CallbackData, Keyboards
from bot.ui.menu import MENU_SETTINGS

logger = structlog.get_logger()
router = Router()


async def _get_add_service() -> AddService:
    """Get add service instance using singleton clients from registry."""
    return AddService(
        await get_prowlarr(),
        await get_radarr(),
        await get_sonarr(),
        qbittorrent=await get_qbittorrent(),
        lidarr=await get_lidarr(),
    )


async def _render_settings_menu(db_user: User) -> tuple[str, "Keyboards"]:
    """Build the settings-menu text + keyboard.

    PERF-07c: the 4 profile/folder lookups are independent HTTP calls to two
    different *arr instances — fire them concurrently instead of sequentially
    (cuts the round-trip from ~4x latency to ~1x).
    """
    import asyncio

    add_service = await _get_add_service()

    radarr_profiles, radarr_folders, sonarr_profiles, sonarr_folders = await asyncio.gather(
        add_service.get_radarr_profiles(),
        add_service.get_radarr_root_folders(),
        add_service.get_sonarr_profiles(),
        add_service.get_sonarr_root_folders(),
    )

    text = Formatters.format_user_preferences(
        db_user.preferences,
        radarr_profiles,
        radarr_folders,
        sonarr_profiles,
        sonarr_folders,
    )
    keyboard = Keyboards.settings_menu(lidarr_enabled=get_settings().lidarr_enabled)
    return text, keyboard


@router.message(F.text == MENU_SETTINGS)
@router.message(Command("settings"))
async def cmd_settings(message: Message, db_user: User) -> None:
    """Handle /settings command."""
    try:
        text, keyboard = await _render_settings_menu(db_user)
        await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
    except Exception as e:
        logger.error("Failed to load settings", error=str(e), exc_info=True)
        await message.answer(Formatters.format_error("Ошибка загрузки настроек"))


@router.callback_query(F.data == CallbackData.SETTINGS)
async def handle_settings_back(callback: CallbackQuery, db_user: User) -> None:
    """Return to main settings menu."""
    if not callback.message:
        return

    try:
        text, keyboard = await _render_settings_menu(db_user)
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
        await callback.answer()
    except Exception as e:
        logger.error("Failed to load settings", error=str(e), exc_info=True)
        await callback.answer("Ошибка загрузки настроек", show_alert=True)


# ---------------------------------------------------------------------------
# LOGIC-05: table-driven profile/folder pickers
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _SettingsEntry:
    """One row of the settings dispatch table.

    menu_callback: exact callback_data that opens the picker (e.g. "settings:radarr_profile").
    getter: AddService coroutine method returning the list of choices.
    keyboard_builder: Keyboards static method building the picker keyboard.
    pref_key: UserPreferences field name to point-update (DB-05, json_set).
        r5: also doubles as the typed ``SettingCB.key`` — the picker keyboard
        and ``handle_settings_set`` both key off this one string, so there is
        no separate "set:*" prefix to keep in sync with it.
    not_found_msg: alert shown when the getter returns an empty list.
    picker_title: HTML header shown above the picker keyboard.
    success_msg: toast shown after a successful set.
    """

    menu_callback: str
    getter: Callable[[AddService], Awaitable[list]]
    keyboard_builder: Callable[[list, str], object]
    pref_key: str
    not_found_msg: str
    picker_title: str
    success_msg: str


_SETTINGS_MAP: dict[str, _SettingsEntry] = {
    entry.menu_callback: entry
    for entry in (
        _SettingsEntry(
            menu_callback="settings:radarr_profile",
            getter=lambda svc: svc.get_radarr_profiles(),
            keyboard_builder=Keyboards.quality_profiles,
            pref_key="radarr_quality_profile_id",
            not_found_msg="Профили качества в Radarr не найдены",
            picker_title="<b>Выберите профиль качества Radarr:</b>",
            success_msg="Профиль Radarr обновлён!",
        ),
        _SettingsEntry(
            menu_callback="settings:radarr_folder",
            getter=lambda svc: svc.get_radarr_root_folders(),
            keyboard_builder=Keyboards.root_folders,
            pref_key="radarr_root_folder_id",
            not_found_msg="Корневые папки в Radarr не найдены",
            picker_title="<b>Выберите корневую папку Radarr:</b>",
            success_msg="Папка Radarr обновлена!",
        ),
        _SettingsEntry(
            menu_callback="settings:sonarr_profile",
            getter=lambda svc: svc.get_sonarr_profiles(),
            keyboard_builder=Keyboards.quality_profiles,
            pref_key="sonarr_quality_profile_id",
            not_found_msg="Профили качества в Sonarr не найдены",
            picker_title="<b>Выберите профиль качества Sonarr:</b>",
            success_msg="Профиль Sonarr обновлён!",
        ),
        _SettingsEntry(
            menu_callback="settings:sonarr_folder",
            getter=lambda svc: svc.get_sonarr_root_folders(),
            keyboard_builder=Keyboards.root_folders,
            pref_key="sonarr_root_folder_id",
            not_found_msg="Корневые папки в Sonarr не найдены",
            picker_title="<b>Выберите корневую папку Sonarr:</b>",
            success_msg="Папка Sonarr обновлена!",
        ),
        _SettingsEntry(
            menu_callback="settings:lidarr_profile",
            getter=lambda svc: svc.get_lidarr_profiles(),
            keyboard_builder=Keyboards.quality_profiles,
            pref_key="lidarr_quality_profile_id",
            not_found_msg="Профили качества в Lidarr не найдены",
            picker_title="<b>Выберите профиль качества Lidarr:</b>",
            success_msg="Профиль Lidarr обновлён!",
        ),
        _SettingsEntry(
            menu_callback="settings:lidarr_meta",
            getter=lambda svc: svc.get_lidarr_metadata_profiles(),
            keyboard_builder=Keyboards.metadata_profiles,
            pref_key="lidarr_metadata_profile_id",
            not_found_msg="Metadata-профили в Lidarr не найдены",
            picker_title="<b>Выберите metadata-профиль Lidarr:</b>",
            success_msg="Metadata-профиль Lidarr обновлён!",
        ),
        _SettingsEntry(
            menu_callback="settings:lidarr_folder",
            getter=lambda svc: svc.get_lidarr_root_folders(),
            keyboard_builder=Keyboards.root_folders,
            pref_key="lidarr_root_folder_id",
            not_found_msg="Корневые папки в Lidarr не найдены",
            picker_title="<b>Выберите корневую папку Lidarr:</b>",
            success_msg="Папка Lidarr обновлена!",
        ),
    )
}

# Reverse lookup: SettingCB.key -> table entry, for handle_settings_set.
_SETTINGS_SET_MAP: dict[str, _SettingsEntry] = {
    entry.pref_key: entry for entry in _SETTINGS_MAP.values()
}


@router.callback_query(F.data.in_(_SETTINGS_MAP.keys()))
async def handle_settings_menu(callback: CallbackQuery) -> None:
    """Generic menu handler: show the picker keyboard for any mapped setting."""
    if not callback.message or not callback.data:
        return

    entry = _SETTINGS_MAP[callback.data]
    add_service = await _get_add_service()

    try:
        choices = await entry.getter(add_service)

        if not choices:
            await callback.answer(entry.not_found_msg, show_alert=True)
            return

        await callback.message.edit_text(
            entry.picker_title,
            reply_markup=entry.keyboard_builder(choices, entry.pref_key),
            parse_mode="HTML",
        )
        await callback.answer()

    except Exception as e:
        logger.error(
            "Failed to load settings picker",
            pref_key=entry.pref_key,
            error=str(e),
            exc_info=True,
        )
        await callback.answer("Ошибка загрузки", show_alert=True)


def _matches_settings_set_key(callback_data: SettingCB) -> bool:
    return callback_data.key in _SETTINGS_SET_MAP


@router.callback_query(SettingCB.filter(F.func(_matches_settings_set_key)))
async def handle_settings_set(
    callback: CallbackQuery, callback_data: SettingCB, db_user: User, db: Database
) -> None:
    """Generic set handler: point-update the matching preference key.

    BUG-04b: exactly one ``callback.answer()`` per callback — the toast is
    shown via the *first* answer (with alert text on error), and the settings
    menu is re-rendered without a second ``answer()`` call (previously this
    called ``handle_settings_back``, which issued its own ``answer()`` too).
    DB-05: writes go through ``Database.update_user_preference`` (point
    ``json_set`` UPDATE) instead of a read-modify-write of the whole
    ``preferences`` blob, so two concurrent settings changes on different
    keys can't clobber each other.
    """
    if not callback.message:
        return

    entry = _SETTINGS_SET_MAP[callback_data.key]

    try:
        value = int(callback_data.value)
    except ValueError:
        await callback.answer("Неверное значение", show_alert=True)
        return

    try:
        await db.update_user_preference(db_user.tg_id, entry.pref_key, value)
        setattr(db_user.preferences, entry.pref_key, value)

        text, keyboard = await _render_settings_menu(db_user)
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

    except Exception as e:
        logger.error(
            "Failed to update setting",
            pref_key=entry.pref_key,
            error=str(e),
            exc_info=True,
        )
        await callback.answer("Ошибка обновления", show_alert=True)
        return

    await callback.answer(entry.success_msg)


# ---------------------------------------------------------------------------
# Resolution & auto-grab: different shape (no *arr API call), kept dedicated.
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "settings:resolution")
async def handle_resolution_menu(callback: CallbackQuery) -> None:
    """Show resolution selection."""
    if not callback.message:
        return

    await callback.message.edit_text(
        "<b>Выберите предпочитаемое разрешение:</b>",
        reply_markup=Keyboards.resolution_selection(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(SettingCB.filter(F.key == "preferred_resolution"))
async def handle_set_resolution(
    callback: CallbackQuery, callback_data: SettingCB, db_user: User, db: Database
) -> None:
    """Set preferred resolution."""
    if not callback.message:
        return

    try:
        resolution = callback_data.value
        if resolution == "any":
            resolution = None

        await db.update_user_preference(db_user.tg_id, "preferred_resolution", resolution)
        db_user.preferences.preferred_resolution = resolution

        text, keyboard = await _render_settings_menu(db_user)
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

    except Exception as e:
        logger.error("Failed to update resolution", error=str(e), exc_info=True)
        await callback.answer("Ошибка обновления", show_alert=True)
        return

    await callback.answer("Разрешение обновлено!")


@router.callback_query(F.data == "settings:auto_grab")
async def handle_auto_grab_menu(callback: CallbackQuery, db_user: User) -> None:
    """Show auto-grab toggle."""
    if not callback.message:
        return

    settings = get_settings()

    await callback.message.edit_text(
        f"<b>Авто-загрузка</b>\n\n"
        f"При включении релизы с высоким рейтингом (≥ {settings.auto_grab_score_threshold}) "
        f"покажут кнопку «Скачать лучшее» для быстрой загрузки.",
        reply_markup=Keyboards.auto_grab_toggle(db_user.preferences.auto_grab_enabled),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(SettingCB.filter(F.key == "auto_grab_enabled"))
async def handle_set_auto_grab(
    callback: CallbackQuery, callback_data: SettingCB, db_user: User, db: Database
) -> None:
    """Toggle auto-grab setting."""
    if not callback.message:
        return

    try:
        enabled = callback_data.value == "1"

        await db.update_user_preference(db_user.tg_id, "auto_grab_enabled", enabled)
        db_user.preferences.auto_grab_enabled = enabled

        text, keyboard = await _render_settings_menu(db_user)
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

    except Exception as e:
        logger.error("Failed to update auto-grab", error=str(e), exc_info=True)
        await callback.answer("Ошибка обновления", show_alert=True)
        return

    await callback.answer(f"Авто-загрузка {'включена' if enabled else 'выключена'}!")


@router.callback_query(F.data.startswith("set:"))
async def handle_legacy_setting_set(callback: CallbackQuery) -> None:
    """r5: legacy ``set:rp:``/``rf:``/``sp:``/``sf:``/``lp:``/``lm:``/``lf:``/
    ``res:``/``ag:`` string buttons from messages sent before the SettingCB
    migration — surface an explicit alert instead of falling through
    unhandled. Registered last so it only catches callbacks the typed
    ``SettingCB.filter()`` handlers above didn't already claim (aiogram's
    typed CallbackData still serializes with the same "set:" text prefix,
    but structural unpacking there always wins first).
    """
    await callback.answer("Кнопка устарела — откройте настройки заново", show_alert=True)
