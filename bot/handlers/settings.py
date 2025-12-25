"""Settings handlers."""

import structlog
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot.config import get_settings
from bot.db import Database
from bot.models import User, UserPreferences
from bot.services.add_service import AddService
from bot.ui.formatters import Formatters
from bot.ui.keyboards import CallbackData, Keyboards

logger = structlog.get_logger()
router = Router()


async def get_db() -> Database:
    """Get database instance."""
    settings = get_settings()
    db = Database(settings.database_path)
    await db.connect()
    return db


async def get_add_service() -> AddService:
    """Get add service instance."""
    from bot.clients import ProwlarrClient, RadarrClient, SonarrClient

    settings = get_settings()

    prowlarr = ProwlarrClient(settings.prowlarr_url, settings.prowlarr_api_key)
    radarr = RadarrClient(settings.radarr_url, settings.radarr_api_key)
    sonarr = SonarrClient(settings.sonarr_url, settings.sonarr_api_key)

    return AddService(prowlarr, radarr, sonarr)


@router.message(Command("settings"))
async def cmd_settings(message: Message, db_user: User) -> None:
    """Handle /settings command."""
    add_service = await get_add_service()

    try:
        # Get current settings data
        radarr_profiles = await add_service.get_radarr_profiles()
        radarr_folders = await add_service.get_radarr_root_folders()
        sonarr_profiles = await add_service.get_sonarr_profiles()
        sonarr_folders = await add_service.get_sonarr_root_folders()

        text = Formatters.format_user_preferences(
            db_user.preferences,
            radarr_profiles,
            radarr_folders,
            sonarr_profiles,
            sonarr_folders,
        )

        await message.answer(
            text,
            reply_markup=Keyboards.settings_menu(),
            parse_mode="Markdown",
        )

    except Exception as e:
        logger.error("Failed to load settings", error=str(e))
        await message.answer(Formatters.format_error(f"Failed to load settings: {str(e)}"))
    finally:
        await add_service.radarr.close()
        await add_service.sonarr.close()
        await add_service.prowlarr.close()


@router.callback_query(F.data == CallbackData.SETTINGS)
async def handle_settings_back(callback: CallbackQuery, db_user: User) -> None:
    """Return to main settings menu."""
    if not callback.message:
        return

    add_service = await get_add_service()

    try:
        radarr_profiles = await add_service.get_radarr_profiles()
        radarr_folders = await add_service.get_radarr_root_folders()
        sonarr_profiles = await add_service.get_sonarr_profiles()
        sonarr_folders = await add_service.get_sonarr_root_folders()

        text = Formatters.format_user_preferences(
            db_user.preferences,
            radarr_profiles,
            radarr_folders,
            sonarr_profiles,
            sonarr_folders,
        )

        await callback.message.edit_text(
            text,
            reply_markup=Keyboards.settings_menu(),
            parse_mode="Markdown",
        )
        await callback.answer()

    except Exception as e:
        logger.error("Failed to load settings", error=str(e))
        await callback.answer("Failed to load settings", show_alert=True)
    finally:
        await add_service.radarr.close()
        await add_service.sonarr.close()
        await add_service.prowlarr.close()


@router.callback_query(F.data == "settings:radarr_profile")
async def handle_radarr_profile_menu(callback: CallbackQuery) -> None:
    """Show Radarr profile selection."""
    if not callback.message:
        return

    add_service = await get_add_service()

    try:
        profiles = await add_service.get_radarr_profiles()

        if not profiles:
            await callback.answer("No quality profiles found in Radarr", show_alert=True)
            return

        await callback.message.edit_text(
            "**Select Radarr Quality Profile:**",
            reply_markup=Keyboards.quality_profiles(profiles, CallbackData.SET_RADARR_PROFILE),
            parse_mode="Markdown",
        )
        await callback.answer()

    except Exception as e:
        logger.error("Failed to load Radarr profiles", error=str(e))
        await callback.answer("Failed to load profiles", show_alert=True)
    finally:
        await add_service.radarr.close()
        await add_service.sonarr.close()
        await add_service.prowlarr.close()


@router.callback_query(F.data.startswith(CallbackData.SET_RADARR_PROFILE))
async def handle_set_radarr_profile(callback: CallbackQuery, db_user: User) -> None:
    """Set Radarr quality profile."""
    if not callback.data or not callback.message:
        return

    db = await get_db()

    try:
        profile_id = int(callback.data.replace(CallbackData.SET_RADARR_PROFILE, ""))

        db_user.preferences.radarr_quality_profile_id = profile_id
        await db.update_user_preferences(db_user.tg_id, db_user.preferences)

        await callback.answer("Radarr profile updated!")

        # Return to settings
        await handle_settings_back(callback, db_user)

    except ValueError:
        await callback.answer("Invalid profile", show_alert=True)
    except Exception as e:
        logger.error("Failed to update profile", error=str(e))
        await callback.answer("Failed to update", show_alert=True)
    finally:
        await db.close()


@router.callback_query(F.data == "settings:radarr_folder")
async def handle_radarr_folder_menu(callback: CallbackQuery) -> None:
    """Show Radarr root folder selection."""
    if not callback.message:
        return

    add_service = await get_add_service()

    try:
        folders = await add_service.get_radarr_root_folders()

        if not folders:
            await callback.answer("No root folders found in Radarr", show_alert=True)
            return

        await callback.message.edit_text(
            "**Select Radarr Root Folder:**",
            reply_markup=Keyboards.root_folders(folders, CallbackData.SET_RADARR_FOLDER),
            parse_mode="Markdown",
        )
        await callback.answer()

    except Exception as e:
        logger.error("Failed to load Radarr folders", error=str(e))
        await callback.answer("Failed to load folders", show_alert=True)
    finally:
        await add_service.radarr.close()
        await add_service.sonarr.close()
        await add_service.prowlarr.close()


@router.callback_query(F.data.startswith(CallbackData.SET_RADARR_FOLDER))
async def handle_set_radarr_folder(callback: CallbackQuery, db_user: User) -> None:
    """Set Radarr root folder."""
    if not callback.data or not callback.message:
        return

    db = await get_db()

    try:
        folder_id = int(callback.data.replace(CallbackData.SET_RADARR_FOLDER, ""))

        db_user.preferences.radarr_root_folder_id = folder_id
        await db.update_user_preferences(db_user.tg_id, db_user.preferences)

        await callback.answer("Radarr folder updated!")

        await handle_settings_back(callback, db_user)

    except ValueError:
        await callback.answer("Invalid folder", show_alert=True)
    except Exception as e:
        logger.error("Failed to update folder", error=str(e))
        await callback.answer("Failed to update", show_alert=True)
    finally:
        await db.close()


@router.callback_query(F.data == "settings:sonarr_profile")
async def handle_sonarr_profile_menu(callback: CallbackQuery) -> None:
    """Show Sonarr profile selection."""
    if not callback.message:
        return

    add_service = await get_add_service()

    try:
        profiles = await add_service.get_sonarr_profiles()

        if not profiles:
            await callback.answer("No quality profiles found in Sonarr", show_alert=True)
            return

        await callback.message.edit_text(
            "**Select Sonarr Quality Profile:**",
            reply_markup=Keyboards.quality_profiles(profiles, CallbackData.SET_SONARR_PROFILE),
            parse_mode="Markdown",
        )
        await callback.answer()

    except Exception as e:
        logger.error("Failed to load Sonarr profiles", error=str(e))
        await callback.answer("Failed to load profiles", show_alert=True)
    finally:
        await add_service.radarr.close()
        await add_service.sonarr.close()
        await add_service.prowlarr.close()


@router.callback_query(F.data.startswith(CallbackData.SET_SONARR_PROFILE))
async def handle_set_sonarr_profile(callback: CallbackQuery, db_user: User) -> None:
    """Set Sonarr quality profile."""
    if not callback.data or not callback.message:
        return

    db = await get_db()

    try:
        profile_id = int(callback.data.replace(CallbackData.SET_SONARR_PROFILE, ""))

        db_user.preferences.sonarr_quality_profile_id = profile_id
        await db.update_user_preferences(db_user.tg_id, db_user.preferences)

        await callback.answer("Sonarr profile updated!")

        await handle_settings_back(callback, db_user)

    except ValueError:
        await callback.answer("Invalid profile", show_alert=True)
    except Exception as e:
        logger.error("Failed to update profile", error=str(e))
        await callback.answer("Failed to update", show_alert=True)
    finally:
        await db.close()


@router.callback_query(F.data == "settings:sonarr_folder")
async def handle_sonarr_folder_menu(callback: CallbackQuery) -> None:
    """Show Sonarr root folder selection."""
    if not callback.message:
        return

    add_service = await get_add_service()

    try:
        folders = await add_service.get_sonarr_root_folders()

        if not folders:
            await callback.answer("No root folders found in Sonarr", show_alert=True)
            return

        await callback.message.edit_text(
            "**Select Sonarr Root Folder:**",
            reply_markup=Keyboards.root_folders(folders, CallbackData.SET_SONARR_FOLDER),
            parse_mode="Markdown",
        )
        await callback.answer()

    except Exception as e:
        logger.error("Failed to load Sonarr folders", error=str(e))
        await callback.answer("Failed to load folders", show_alert=True)
    finally:
        await add_service.radarr.close()
        await add_service.sonarr.close()
        await add_service.prowlarr.close()


@router.callback_query(F.data.startswith(CallbackData.SET_SONARR_FOLDER))
async def handle_set_sonarr_folder(callback: CallbackQuery, db_user: User) -> None:
    """Set Sonarr root folder."""
    if not callback.data or not callback.message:
        return

    db = await get_db()

    try:
        folder_id = int(callback.data.replace(CallbackData.SET_SONARR_FOLDER, ""))

        db_user.preferences.sonarr_root_folder_id = folder_id
        await db.update_user_preferences(db_user.tg_id, db_user.preferences)

        await callback.answer("Sonarr folder updated!")

        await handle_settings_back(callback, db_user)

    except ValueError:
        await callback.answer("Invalid folder", show_alert=True)
    except Exception as e:
        logger.error("Failed to update folder", error=str(e))
        await callback.answer("Failed to update", show_alert=True)
    finally:
        await db.close()


@router.callback_query(F.data == "settings:resolution")
async def handle_resolution_menu(callback: CallbackQuery) -> None:
    """Show resolution selection."""
    if not callback.message:
        return

    await callback.message.edit_text(
        "**Select Preferred Resolution:**",
        reply_markup=Keyboards.resolution_selection(),
        parse_mode="Markdown",
    )
    await callback.answer()


@router.callback_query(F.data.startswith(CallbackData.SET_RESOLUTION))
async def handle_set_resolution(callback: CallbackQuery, db_user: User) -> None:
    """Set preferred resolution."""
    if not callback.data or not callback.message:
        return

    db = await get_db()

    try:
        resolution = callback.data.replace(CallbackData.SET_RESOLUTION, "")
        if resolution == "any":
            resolution = None

        db_user.preferences.preferred_resolution = resolution
        await db.update_user_preferences(db_user.tg_id, db_user.preferences)

        await callback.answer("Resolution preference updated!")

        await handle_settings_back(callback, db_user)

    except Exception as e:
        logger.error("Failed to update resolution", error=str(e))
        await callback.answer("Failed to update", show_alert=True)
    finally:
        await db.close()


@router.callback_query(F.data == "settings:auto_grab")
async def handle_auto_grab_menu(callback: CallbackQuery, db_user: User) -> None:
    """Show auto-grab toggle."""
    if not callback.message:
        return

    settings = get_settings()

    await callback.message.edit_text(
        f"**Auto-Grab Setting**\n\n"
        f"When enabled, high-scored releases (score ≥ {settings.auto_grab_score_threshold}) "
        f"will show a 'Grab Best' button for quick downloads.",
        reply_markup=Keyboards.auto_grab_toggle(db_user.preferences.auto_grab_enabled),
        parse_mode="Markdown",
    )
    await callback.answer()


@router.callback_query(F.data.startswith(CallbackData.SET_AUTO_GRAB))
async def handle_set_auto_grab(callback: CallbackQuery, db_user: User) -> None:
    """Toggle auto-grab setting."""
    if not callback.data or not callback.message:
        return

    db = await get_db()

    try:
        value = callback.data.replace(CallbackData.SET_AUTO_GRAB, "")
        enabled = value == "1"

        db_user.preferences.auto_grab_enabled = enabled
        await db.update_user_preferences(db_user.tg_id, db_user.preferences)

        await callback.answer(f"Auto-grab {'enabled' if enabled else 'disabled'}!")

        await handle_settings_back(callback, db_user)

    except Exception as e:
        logger.error("Failed to update auto-grab", error=str(e))
        await callback.answer("Failed to update", show_alert=True)
    finally:
        await db.close()
