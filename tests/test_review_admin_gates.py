"""Final branch review (2026-09-24): catalogue delete and qBit speed limits
are admin-only — in the handler (a stale/forged button must not work) and in
the keyboard (a non-admin must not be shown the button at all)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.models import MovieInfo, QBittorrentStatus, TorrentInfo, TorrentState
from bot.ui.callbacks import TitleActionCB

ADMIN_ONLY = "⛔ Только для администратора"


def _movie() -> MovieInfo:
    return MovieInfo(tmdb_id=1, title="Some Movie", year=2020, radarr_id=15, monitored=True, has_file=False)


def _cb(data: str = "") -> MagicMock:
    cb = MagicMock()
    cb.data = data
    cb.from_user = MagicMock(id=42)
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    return cb


def _texts(kb) -> list[str]:
    return [b.text for row in kb.inline_keyboard for b in row]


# ------------------------------------------------------------------ titles
@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["delete", "delconf"])
async def test_non_admin_cannot_delete_a_title(action):
    from bot.handlers import titles as titles_handler

    radarr = AsyncMock()
    db = AsyncMock()
    cb = _cb()
    titles_handler._title_candidates[42] = {"movie:15": _movie()}
    try:
        with patch.object(titles_handler, "get_radarr", AsyncMock(return_value=radarr)), \
             patch.object(titles_handler, "get_sonarr", AsyncMock(return_value=AsyncMock())):
            await titles_handler.handle_title_action(
                cb, TitleActionCB(action=action, kind="movie", title_id="15"),
                MagicMock(tg_id=42), db, is_admin=False,
            )
    finally:
        titles_handler._title_candidates.pop(42, None)

    radarr.delete_movie.assert_not_awaited()
    cb.message.edit_text.assert_not_awaited()
    cb.answer.assert_awaited_once()
    assert ADMIN_ONLY in cb.answer.await_args.args[0]
    assert cb.answer.await_args.kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_non_admin_can_still_toggle_monitoring():
    from bot.handlers import titles as titles_handler

    radarr = AsyncMock()
    radarr.get_movie = AsyncMock(return_value={"id": 15, "monitored": True})
    radarr.update_movie = AsyncMock(return_value=True)
    cb = _cb()
    with patch.object(titles_handler, "get_radarr", AsyncMock(return_value=radarr)), \
         patch.object(titles_handler, "_apply_monitor_toggle", AsyncMock(return_value=True)) as toggle:
        await titles_handler.handle_title_action(
            cb, TitleActionCB(action="unmon", kind="movie", title_id="15"),
            MagicMock(tg_id=42), AsyncMock(), is_admin=False,
        )
    toggle.assert_awaited_once()


def test_title_actions_hides_delete_for_non_admin():
    from bot.ui.keyboards import Keyboards

    assert not any("🗑" in t for t in _texts(Keyboards.title_actions(_movie())))
    assert not any("🗑" in t for t in _texts(Keyboards.title_actions(_movie(), is_admin=False)))
    assert any("🗑" in t for t in _texts(Keyboards.title_actions(_movie(), is_admin=True)))


@pytest.mark.asyncio
async def test_pick_renders_card_with_delete_only_for_admin():
    from bot.handlers import titles as titles_handler

    for is_admin, expected in ((True, True), (False, False)):
        cb = _cb()
        titles_handler._title_candidates[42] = {"movie:15": _movie()}
        try:
            with patch.object(titles_handler, "get_radarr", AsyncMock(return_value=AsyncMock())):
                await titles_handler.handle_title_action(
                    cb, TitleActionCB(action="pick", kind="movie", title_id="15"),
                    MagicMock(tg_id=42), AsyncMock(), is_admin=is_admin,
                )
        finally:
            titles_handler._title_candidates.pop(42, None)
        kb = cb.message.edit_text.await_args.kwargs["reply_markup"]
        assert any("🗑" in t for t in _texts(kb)) is expected


# --------------------------------------------------------------- downloads
def _status() -> QBittorrentStatus:
    return QBittorrentStatus(version="4.6.0", connection_status="connected", download_limit=0, upload_limit=0)


@pytest.mark.asyncio
async def test_non_admin_cannot_open_speed_menu():
    from bot.handlers import downloads

    qbt = AsyncMock()
    qbt.get_status = AsyncMock(return_value=_status())
    cb = _cb("speed_menu")
    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)):
        await downloads.handle_speed_menu(cb, is_admin=False)
    cb.message.edit_text.assert_not_awaited()
    assert ADMIN_ONLY in cb.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_non_admin_cannot_set_speed_limit():
    from bot.handlers import downloads

    qbt = AsyncMock()
    cb = _cb("speed:dl:1024")
    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)):
        await downloads.handle_speed_set(cb, is_admin=False)
    qbt.set_download_limit.assert_not_awaited()
    qbt.set_upload_limit.assert_not_awaited()
    assert ADMIN_ONLY in cb.answer.await_args.args[0]


def test_torrent_list_hides_speed_button_for_non_admin():
    from bot.models import TorrentFilter
    from bot.ui.keyboards import Keyboards

    t = TorrentInfo(hash="a" * 40, name="Test.Torrent", size=1, progress=0.5,
                    download_speed=0, upload_speed=0, eta=None, state=TorrentState.DOWNLOADING)
    user = _texts(Keyboards.torrent_list([t], 0, 1, TorrentFilter.ALL, is_admin=False))
    admin = _texts(Keyboards.torrent_list([t], 0, 1, TorrentFilter.ALL, is_admin=True))
    assert not any("🚀" in x for x in user)
    assert any("🚀" in x for x in admin)
