"""Round 2 / D1: the speed screen is a "turtle" (qBit alternative speed
limits) toggle; the exact preset grid moved behind "⋯ Точные лимиты"."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.clients.qbittorrent import QBittorrentClient
from bot.models import TorrentFilter

ADMIN_ONLY = "Только для администратора"


def _cb(data: str) -> MagicMock:
    cb = MagicMock()
    cb.data = data
    cb.answer = AsyncMock()
    cb.from_user = MagicMock(id=7)
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    return cb


def _texts(kb) -> list[str]:
    return [b.text for row in kb.inline_keyboard for b in row]


def _buttons(kb):
    return [b for row in kb.inline_keyboard for b in row]


# ------------------------------------------------------------------ client
@pytest.fixture
def client():
    return QBittorrentClient("http://198.51.100.10:8080", "admin", "pw")


@pytest.mark.asyncio
@pytest.mark.parametrize("raw,expected", [(1, True), ("1", True), (0, False), ("0", False), (None, False)])
async def test_get_speed_limits_mode_parses_flag(client, raw, expected):
    with patch.object(client, "_request", AsyncMock(return_value=raw)) as req:
        assert await client.get_speed_limits_mode() is expected
    req.assert_awaited_once_with("GET", "/api/v2/transfer/speedLimitsMode")


@pytest.mark.asyncio
async def test_toggle_speed_limits_mode_posts(client):
    # qBit 5 answers 204 with no body → _request returns None; must not fail.
    with patch.object(client, "_request", AsyncMock(return_value=None)) as req:
        await client.toggle_speed_limits_mode()
    req.assert_awaited_once_with("POST", "/api/v2/transfer/toggleSpeedLimitsMode")


@pytest.mark.asyncio
async def test_get_alt_speed_limits_reads_preferences(client):
    prefs = {"alt_dl_limit": 1024 * 1024, "alt_up_limit": 0, "dl_limit": 0}
    with patch.object(client, "_request", AsyncMock(return_value=prefs)) as req:
        assert await client.get_alt_speed_limits() == (1024 * 1024, 0)
    req.assert_awaited_once_with("GET", "/api/v2/app/preferences")


@pytest.mark.asyncio
async def test_get_alt_speed_limits_tolerates_garbage(client):
    with patch.object(client, "_request", AsyncMock(return_value="Forbidden")):
        assert await client.get_alt_speed_limits() == (0, 0)


# ---------------------------------------------------------------- keyboard
def test_speed_screen_rows_and_back_keeps_view():
    from bot.ui.callbacks import TorrentPageCB
    from bot.ui.keyboards import CallbackData, Keyboards

    kb = Keyboards.speed_screen(turtle_on=True, current_filter=TorrentFilter.PAUSED, page=1)
    rows = [[b.text for b in row] for row in kb.inline_keyboard]
    assert rows == [["🐢 Черепаха: вкл"], ["⋯ Точные лимиты"], ["◀️ Назад"]]
    btns = _buttons(kb)
    assert btns[0].callback_data == CallbackData.SPEED_TURTLE
    assert btns[1].callback_data == CallbackData.SPEED_EXACT
    assert TorrentPageCB.unpack(btns[2].callback_data) == TorrentPageCB(page=1, flt="paused")

    off = Keyboards.speed_screen(turtle_on=False)
    assert _texts(off)[0] == "🐢 Черепаха: выкл"


def test_exact_limits_back_returns_to_speed_screen():
    from bot.ui.keyboards import CallbackData, Keyboards

    kb = Keyboards.speed_limits_menu(0, 0)
    back = _buttons(kb)[-1]
    assert back.text == "◀️ Назад"
    assert back.callback_data == CallbackData.SPEED_MENU


def test_new_speed_callbacks_fit_and_do_not_collide():
    from bot.ui.keyboards import CallbackData

    for data in (CallbackData.SPEED_TURTLE, CallbackData.SPEED_EXACT):
        assert len(data.encode()) <= 64
        # The preset handler matches startswith("speed:") — must not swallow these.
        assert not data.startswith(CallbackData.SPEED_LIMIT)
        assert data != CallbackData.SPEED_MENU


def test_speed_screen_text_shows_units():
    from bot.ui.formatters import Formatters

    text = Formatters.format_speed_screen(
        dl_speed=2 * 1024 * 1024, ul_speed=512 * 1024,
        alt_dl=1024 * 1024, alt_ul=0,
    )
    lines = text.splitlines()
    assert "🚀 Скорость" in lines[0]
    # fix round 1: Russian units, like the preset grid
    assert lines[1] == "Сейчас: ⬇️ 2 МБ/с · ⬆️ 512 КБ/с"
    assert lines[2] == "🐢 Черепаха: ⬇️ 1 МБ/с · ⬆️ ∞ без лимита"


# ---------------------------------------------------------------- handlers
def _qbt(mode_on: bool) -> AsyncMock:
    qbt = AsyncMock()
    qbt.get_transfer_info = AsyncMock(return_value={"dl_info_speed": 2048, "up_info_speed": 0})
    qbt.get_alt_speed_limits = AsyncMock(return_value=(512 * 1024, 256 * 1024))
    state = {"on": mode_on}

    async def _mode():
        return state["on"]

    async def _toggle():
        state["on"] = not state["on"]

    qbt.get_speed_limits_mode = AsyncMock(side_effect=_mode)
    qbt.toggle_speed_limits_mode = AsyncMock(side_effect=_toggle)
    return qbt


@pytest.mark.asyncio
async def test_speed_menu_renders_turtle_screen():
    from bot.handlers import downloads

    qbt = _qbt(mode_on=False)
    cb = _cb("speed_menu")
    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)):
        await downloads.handle_speed_menu(cb, is_admin=True)

    text = cb.message.edit_text.await_args.args[0]
    assert "Сейчас: ⬇️ 2 КБ/с · ⬆️ 0 КБ/с" in text
    assert "🐢 Черепаха: ⬇️ 512 КБ/с · ⬆️ 256 КБ/с" in text
    kb = cb.message.edit_text.await_args.kwargs["reply_markup"]
    assert _texts(kb)[0] == "🐢 Черепаха: выкл"
    cb.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_turtle_toggle_calls_api_and_rerenders_new_state():
    from bot.handlers import downloads

    qbt = _qbt(mode_on=False)
    cb = _cb("speed_turtle")
    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)):
        await downloads.handle_speed_turtle(cb, is_admin=True)

    qbt.toggle_speed_limits_mode.assert_awaited_once()
    kb = cb.message.edit_text.await_args.kwargs["reply_markup"]
    assert _texts(kb)[0] == "🐢 Черепаха: вкл"
    assert cb.answer.await_count == 1
    assert "включена" in cb.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_turtle_toggle_refused_for_non_admin():
    from bot.handlers import downloads

    qbt = _qbt(mode_on=False)
    cb = _cb("speed_turtle")
    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)):
        await downloads.handle_speed_turtle(cb, is_admin=False)

    qbt.toggle_speed_limits_mode.assert_not_awaited()
    cb.message.edit_text.assert_not_awaited()
    assert ADMIN_ONLY in cb.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_exact_limits_opens_preset_grid_admin_only():
    from bot.handlers import downloads
    from bot.models import QBittorrentStatus

    qbt = AsyncMock()
    qbt.get_status = AsyncMock(return_value=QBittorrentStatus(download_limit=1024 * 1024, upload_limit=0))

    denied = _cb("speed_exact")
    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)):
        await downloads.handle_speed_exact(denied, is_admin=False)
    denied.message.edit_text.assert_not_awaited()
    assert ADMIN_ONLY in denied.answer.await_args.args[0]

    cb = _cb("speed_exact")
    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)):
        await downloads.handle_speed_exact(cb, is_admin=True)
    kb = cb.message.edit_text.await_args.kwargs["reply_markup"]
    assert any(t.startswith("✓") and "1 МБ/с" in t for t in _texts(kb))
    cb.answer.assert_awaited_once()


# ============================================================ fix round 1
@pytest.mark.asyncio
async def test_set_alt_speed_limits_posts_json_preferences(client):
    import json

    with patch.object(client, "_request", AsyncMock(return_value=None)) as req:
        await client.set_alt_speed_limits(2097152, 524288)
    method, endpoint = req.await_args.args
    assert (method, endpoint) == ("POST", "/api/v2/app/setPreferences")
    assert json.loads(req.await_args.kwargs["data"]["json"]) == {
        "alt_dl_limit": 2097152, "alt_up_limit": 524288,
    }


def _qbt_alt(mode_on: bool, alt: tuple[int, int]) -> AsyncMock:
    qbt = _qbt(mode_on)
    state = {"alt": alt}

    async def _get_alt():
        return state["alt"]

    async def _set_alt(dl, up):
        state["alt"] = (dl, up)

    qbt.get_alt_speed_limits = AsyncMock(side_effect=_get_alt)
    qbt.set_alt_speed_limits = AsyncMock(side_effect=_set_alt)
    return qbt


@pytest.mark.asyncio
async def test_turning_on_replaces_factory_alt_limits():
    from bot.handlers import downloads

    qbt = _qbt_alt(mode_on=False, alt=(10240, 10240))
    cb = _cb("speed_turtle")
    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)):
        await downloads.handle_speed_turtle(cb, is_admin=True)

    qbt.set_alt_speed_limits.assert_awaited_once_with(2097152, 524288)
    qbt.toggle_speed_limits_mode.assert_awaited_once()
    assert cb.answer.await_count == 1
    assert cb.answer.await_args.args[0] == "🐢 Черепаха включена: ⬇️ 2 МБ/с · ⬆️ 512 КБ/с"
    text = cb.message.edit_text.await_args.args[0]
    assert "🐢 Черепаха: ⬇️ 2 МБ/с · ⬆️ 512 КБ/с" in text


@pytest.mark.asyncio
async def test_turning_on_keeps_custom_alt_limits():
    from bot.handlers import downloads

    qbt = _qbt_alt(mode_on=False, alt=(1048576, 0))
    cb = _cb("speed_turtle")
    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)):
        await downloads.handle_speed_turtle(cb, is_admin=True)

    qbt.set_alt_speed_limits.assert_not_awaited()
    assert cb.answer.await_args.args[0] == "🐢 Черепаха включена: ⬇️ 1 МБ/с · ⬆️ ∞ без лимита"


@pytest.mark.asyncio
async def test_turning_off_does_not_touch_alt_limits():
    from bot.handlers import downloads

    qbt = _qbt_alt(mode_on=True, alt=(10240, 10240))
    cb = _cb("speed_turtle")
    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)):
        await downloads.handle_speed_turtle(cb, is_admin=True)

    qbt.set_alt_speed_limits.assert_not_awaited()
    assert cb.answer.await_args.args[0] == "🐢 Черепаха выключена"


@pytest.mark.asyncio
async def test_turtle_answers_once_when_the_redraw_fails():
    from bot.handlers import downloads

    qbt = _qbt_alt(mode_on=False, alt=(1048576, 1048576))
    qbt.get_transfer_info = AsyncMock(side_effect=RuntimeError("redraw boom"))
    cb = _cb("speed_turtle")
    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)):
        await downloads.handle_speed_turtle(cb, is_admin=True)
    assert cb.answer.await_count == 1


@pytest.mark.parametrize("bps,expected", [
    (0, "0 КБ/с"),
    (512, "512 Б/с"),
    (512 * 1024, "512 КБ/с"),
    (1536, "1.5 КБ/с"),
    (2 * 1024 * 1024, "2 МБ/с"),
    (int(2.5 * 1024 * 1024), "2.5 МБ/с"),
])
def test_russian_speed_units(bps, expected):
    from bot.ui.formatters.torrent import format_speed_ru

    assert format_speed_ru(bps) == expected


def test_speed_screen_uses_russian_units_and_infinity():
    from bot.ui.formatters import Formatters

    text = Formatters.format_speed_screen(dl_speed=0, ul_speed=512 * 1024, alt_dl=0, alt_ul=1048576)
    assert "Сейчас: ⬇️ 0 КБ/с · ⬆️ 512 КБ/с" in text
    assert "🐢 Черепаха: ⬇️ ∞ без лимита · ⬆️ 1 МБ/с" in text
    assert "B/s" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("turtle_on", [True, False])
async def test_exact_grid_warns_when_turtle_is_on(turtle_on):
    from bot.handlers import downloads
    from bot.models import QBittorrentStatus

    qbt = AsyncMock()
    qbt.get_status = AsyncMock(return_value=QBittorrentStatus(download_limit=0, upload_limit=0))
    qbt.get_speed_limits_mode = AsyncMock(return_value=turtle_on)
    cb = _cb("speed_exact")
    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)):
        await downloads.handle_speed_exact(cb, is_admin=True)
    text = cb.message.edit_text.await_args.args[0]
    assert ("🐢 включена — эти лимиты действуют после её выключения" in text) is turtle_on
