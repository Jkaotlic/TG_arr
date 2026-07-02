"""Tests for bot/handlers/settings.py.

LOGIC-05: table-driven menu/set dispatch (_SETTINGS_MAP / _SETTINGS_SET_MAP)
must behave 1:1 with the old copy-pasted handlers — every menu item opens the
right picker and every set-callback writes the right preference key.

BUG-04b: set-handlers must call callback.answer() exactly once (previously
they called handle_settings_back, which issued a second answer()).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.handlers import settings
from bot.models import MetadataProfile, QualityProfile, RootFolder, User, UserPreferences


def _make_callback(data: str) -> MagicMock:
    """Build a CallbackQuery-like mock with AsyncMock for answer/edit_text."""
    cb = MagicMock()
    cb.data = data
    cb.answer = AsyncMock()
    message = MagicMock()
    message.edit_text = AsyncMock()
    cb.message = message
    return cb


def _make_user(**prefs_kwargs) -> User:
    return User(tg_id=123456789, preferences=UserPreferences(**prefs_kwargs))


def _fake_add_service(**overrides) -> AsyncMock:
    """AsyncMock AddService with all getter methods stubbed to empty/defaults."""
    svc = AsyncMock()
    svc.get_radarr_profiles = AsyncMock(return_value=overrides.get("radarr_profiles", []))
    svc.get_radarr_root_folders = AsyncMock(return_value=overrides.get("radarr_folders", []))
    svc.get_sonarr_profiles = AsyncMock(return_value=overrides.get("sonarr_profiles", []))
    svc.get_sonarr_root_folders = AsyncMock(return_value=overrides.get("sonarr_folders", []))
    svc.get_lidarr_profiles = AsyncMock(return_value=overrides.get("lidarr_profiles", []))
    svc.get_lidarr_metadata_profiles = AsyncMock(
        return_value=overrides.get("lidarr_meta_profiles", [])
    )
    svc.get_lidarr_root_folders = AsyncMock(return_value=overrides.get("lidarr_folders", []))
    return svc


# ---------------------------------------------------------------------------
# LOGIC-05: every mapped menu item opens its picker with the right prefix.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "menu_callback,getter_attr,choices,expected_prefix",
    [
        (
            "settings:radarr_profile",
            "radarr_profiles",
            [QualityProfile(id=1, name="HD-1080p")],
            "set:rp:",
        ),
        (
            "settings:radarr_folder",
            "radarr_folders",
            [RootFolder(id=1, path="/movies")],
            "set:rf:",
        ),
        (
            "settings:sonarr_profile",
            "sonarr_profiles",
            [QualityProfile(id=2, name="HD-720p")],
            "set:sp:",
        ),
        (
            "settings:sonarr_folder",
            "sonarr_folders",
            [RootFolder(id=2, path="/tv")],
            "set:sf:",
        ),
        (
            "settings:lidarr_profile",
            "lidarr_profiles",
            [QualityProfile(id=3, name="Lossless")],
            "set:lp:",
        ),
        (
            "settings:lidarr_meta",
            "lidarr_meta_profiles",
            [MetadataProfile(id=4, name="Standard")],
            "set:lm:",
        ),
        (
            "settings:lidarr_folder",
            "lidarr_folders",
            [RootFolder(id=5, path="/music")],
            "set:lf:",
        ),
    ],
)
async def test_settings_menu_opens_correct_picker(
    menu_callback, getter_attr, choices, expected_prefix
):
    svc = _fake_add_service(**{getter_attr: choices})
    cb = _make_callback(menu_callback)

    with patch.object(settings, "_get_add_service", AsyncMock(return_value=svc)):
        await settings.handle_settings_menu(cb)

    cb.message.edit_text.assert_awaited_once()
    _, kwargs = cb.message.edit_text.call_args
    keyboard = kwargs["reply_markup"]
    button = keyboard.inline_keyboard[0][0]
    assert button.callback_data.startswith(expected_prefix)
    cb.answer.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_settings_menu_alerts_when_empty():
    svc = _fake_add_service(radarr_profiles=[])
    cb = _make_callback("settings:radarr_profile")

    with patch.object(settings, "_get_add_service", AsyncMock(return_value=svc)):
        await settings.handle_settings_menu(cb)

    cb.answer.assert_awaited_once()
    args, kwargs = cb.answer.call_args
    assert "не найдены" in args[0]
    assert kwargs.get("show_alert") is True
    cb.message.edit_text.assert_not_called()


# ---------------------------------------------------------------------------
# LOGIC-05 / DB-05: every mapped set-callback writes the right preference key.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "set_callback,pref_key",
    [
        ("set:rp:7", "radarr_quality_profile_id"),
        ("set:rf:8", "radarr_root_folder_id"),
        ("set:sp:9", "sonarr_quality_profile_id"),
        ("set:sf:10", "sonarr_root_folder_id"),
        ("set:lp:11", "lidarr_quality_profile_id"),
        ("set:lm:12", "lidarr_metadata_profile_id"),
        ("set:lf:13", "lidarr_root_folder_id"),
    ],
)
async def test_settings_set_writes_correct_preference_key(set_callback, pref_key):
    db_user = _make_user()
    db = AsyncMock()
    db.update_user_preference = AsyncMock(return_value=True)
    svc = _fake_add_service()
    cb = _make_callback(set_callback)

    with patch.object(settings, "_get_add_service", AsyncMock(return_value=svc)):
        await settings.handle_settings_set(cb, db_user, db)

    expected_value = int(set_callback.rsplit(":", 1)[1])
    db.update_user_preference.assert_awaited_once_with(
        db_user.tg_id, pref_key, expected_value
    )
    assert getattr(db_user.preferences, pref_key) == expected_value


# ---------------------------------------------------------------------------
# BUG-04b: exactly one callback.answer() per callback (no double-ack).
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_settings_set_calls_answer_exactly_once():
    db_user = _make_user()
    db = AsyncMock()
    db.update_user_preference = AsyncMock(return_value=True)
    svc = _fake_add_service()
    cb = _make_callback("set:rp:7")

    with patch.object(settings, "_get_add_service", AsyncMock(return_value=svc)):
        await settings.handle_settings_set(cb, db_user, db)

    assert cb.answer.call_count == 1
    # And the settings menu was re-rendered (edit_text called) without a
    # second answer() from a nested handle_settings_back call.
    cb.message.edit_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_settings_set_render_failure_still_answers_exactly_once():
    """If the point-update succeeds but re-rendering the menu fails (e.g. an
    *arr HTTP error), the handler must still call answer() exactly once (the
    error alert) — not once for a premature success toast and again in the
    except block.
    """
    db_user = _make_user()
    db = AsyncMock()
    db.update_user_preference = AsyncMock(return_value=True)
    cb = _make_callback("set:rp:7")

    broken_add_service = AsyncMock()
    broken_add_service.get_radarr_profiles = AsyncMock(side_effect=RuntimeError("arr down"))

    with patch.object(settings, "_get_add_service", AsyncMock(return_value=broken_add_service)):
        await settings.handle_settings_set(cb, db_user, db)

    assert cb.answer.call_count == 1
    args, kwargs = cb.answer.call_args
    assert kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_settings_set_invalid_value_answers_once_no_render():
    db_user = _make_user()
    db = AsyncMock()
    cb = _make_callback("set:rp:not-an-int")

    await settings.handle_settings_set(cb, db_user, db)

    assert cb.answer.call_count == 1
    db.update_user_preference.assert_not_called()
    cb.message.edit_text.assert_not_called()


@pytest.mark.asyncio
async def test_resolution_set_calls_answer_exactly_once():
    db_user = _make_user()
    db = AsyncMock()
    db.update_user_preference = AsyncMock(return_value=True)
    svc = _fake_add_service()
    cb = _make_callback("set:res:1080p")

    with patch.object(settings, "_get_add_service", AsyncMock(return_value=svc)):
        await settings.handle_set_resolution(cb, db_user, db)

    assert cb.answer.call_count == 1
    db.update_user_preference.assert_awaited_once_with(
        db_user.tg_id, "preferred_resolution", "1080p"
    )
    assert db_user.preferences.preferred_resolution == "1080p"


@pytest.mark.asyncio
async def test_resolution_set_any_maps_to_none():
    db_user = _make_user()
    db = AsyncMock()
    db.update_user_preference = AsyncMock(return_value=True)
    svc = _fake_add_service()
    cb = _make_callback("set:res:any")

    with patch.object(settings, "_get_add_service", AsyncMock(return_value=svc)):
        await settings.handle_set_resolution(cb, db_user, db)

    db.update_user_preference.assert_awaited_once_with(
        db_user.tg_id, "preferred_resolution", None
    )
    assert db_user.preferences.preferred_resolution is None


@pytest.mark.asyncio
async def test_auto_grab_set_calls_answer_exactly_once():
    db_user = _make_user()
    db = AsyncMock()
    db.update_user_preference = AsyncMock(return_value=True)
    svc = _fake_add_service()
    cb = _make_callback("set:ag:1")

    with patch.object(settings, "_get_add_service", AsyncMock(return_value=svc)):
        await settings.handle_set_auto_grab(cb, db_user, db)

    assert cb.answer.call_count == 1
    db.update_user_preference.assert_awaited_once_with(
        db_user.tg_id, "auto_grab_enabled", True
    )
    assert db_user.preferences.auto_grab_enabled is True


@pytest.mark.asyncio
async def test_settings_back_calls_answer_exactly_once():
    db_user = _make_user()
    svc = _fake_add_service()
    cb = _make_callback("settings")

    with patch.object(settings, "_get_add_service", AsyncMock(return_value=svc)):
        await settings.handle_settings_back(cb, db_user)

    assert cb.answer.call_count == 1
    cb.message.edit_text.assert_awaited_once()


# ---------------------------------------------------------------------------
# PERF-07c: the 4 profile/folder calls for the main settings render run
# concurrently, not sequentially.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_render_settings_menu_gathers_calls_concurrently():
    import asyncio

    call_order: list[str] = []

    async def slow(name, delay):
        call_order.append(f"{name}_start")
        await asyncio.sleep(delay)
        call_order.append(f"{name}_end")
        return []

    svc = AsyncMock()
    # Plain lambdas returning the coroutine directly — NOT AsyncMock(side_effect=...),
    # which does not await a coroutine returned by a synchronous side_effect.
    svc.get_radarr_profiles = lambda: slow("radarr_profiles", 0.05)
    svc.get_radarr_root_folders = lambda: slow("radarr_folders", 0.05)
    svc.get_sonarr_profiles = lambda: slow("sonarr_profiles", 0.05)
    svc.get_sonarr_root_folders = lambda: slow("sonarr_folders", 0.05)

    db_user = _make_user()

    with patch.object(settings, "_get_add_service", AsyncMock(return_value=svc)):
        await settings._render_settings_menu(db_user)

    # If calls were sequential, every *_end would appear before the next
    # *_start. With gather, all 4 *_start entries precede all 4 *_end entries.
    starts_before_first_end = call_order[:4]
    assert all(entry.endswith("_start") for entry in starts_before_first_end)
