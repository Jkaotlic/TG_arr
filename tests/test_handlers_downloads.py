"""Tests for bot/handlers/downloads.py.

BUG-15: handle_pause_torrent/resume/delete/delete_with_files must not call
callback.answer twice (once themselves, and again via handle_torrent_details).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.models import TorrentInfo, TorrentState


@pytest.fixture
def fake_torrent():
    """A minimal TorrentInfo stub."""
    return TorrentInfo(
        hash="abc123def456789012345678901234567890abcd",
        name="Test.Torrent.2024",
        size=1_000_000,
        progress=0.5,
        download_speed=0,
        upload_speed=0,
        eta=None,
        state=TorrentState.DOWNLOADING,
    )


def _make_callback(data: str) -> MagicMock:
    """Build a CallbackQuery-like mock with AsyncMock for answer."""
    cb = MagicMock()
    cb.data = data
    cb.answer = AsyncMock()
    message = MagicMock()
    message.edit_text = AsyncMock()
    cb.message = message
    return cb


@pytest.mark.asyncio
async def test_handle_pause_torrent_calls_answer_once(fake_torrent):
    """BUG-15: pause handler must call callback.answer exactly once."""
    from bot.handlers import downloads

    qbt = AsyncMock()
    qbt.get_torrent_by_short_hash = AsyncMock(return_value=fake_torrent)
    qbt.pause = AsyncMock()
    qbt.get_torrents = AsyncMock(return_value=[fake_torrent])

    cb = _make_callback(f"t_pause:{fake_torrent.hash[:8]}")

    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)):
        await downloads.handle_pause_torrent(cb)

    assert cb.answer.call_count == 1, (
        f"callback.answer was called {cb.answer.call_count} times "
        f"(expected 1). Recursive answer leak (BUG-15)."
    )
    qbt.pause.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_resume_torrent_calls_answer_once(fake_torrent):
    """BUG-15: resume handler must call callback.answer exactly once."""
    from bot.handlers import downloads

    qbt = AsyncMock()
    qbt.get_torrent_by_short_hash = AsyncMock(return_value=fake_torrent)
    qbt.resume = AsyncMock()
    qbt.get_torrents = AsyncMock(return_value=[fake_torrent])

    cb = _make_callback(f"t_resume:{fake_torrent.hash[:8]}")

    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)):
        await downloads.handle_resume_torrent(cb)

    assert cb.answer.call_count == 1
    qbt.resume.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_delete_torrent_calls_answer_once(fake_torrent):
    """BUG-15: delete handler must call callback.answer exactly once."""
    from bot.handlers import downloads

    qbt = AsyncMock()
    qbt.get_torrent_by_short_hash = AsyncMock(return_value=fake_torrent)
    qbt.delete = AsyncMock()
    qbt.get_torrents = AsyncMock(return_value=[])

    cb = _make_callback(f"t_delete:{fake_torrent.hash[:8]}")

    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)):
        await downloads.handle_delete_torrent(cb, is_admin=True)

    assert cb.answer.call_count == 1
    qbt.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_delete_with_files_shows_confirmation(fake_torrent):
    """BUG-14/DEAD-03: t_delf must show a confirmation, not delete immediately."""
    from bot.handlers import downloads

    qbt = AsyncMock()
    qbt.get_torrent_by_short_hash = AsyncMock(return_value=fake_torrent)
    qbt.get_torrent = AsyncMock(return_value=fake_torrent)
    qbt.delete = AsyncMock()

    cb = _make_callback(f"t_delf:{fake_torrent.hash}")

    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)):
        await downloads.handle_delete_with_files(cb, is_admin=True)

    assert cb.answer.call_count == 1
    qbt.delete.assert_not_awaited()
    cb.message.edit_text.assert_awaited_once()
    _, kwargs = cb.message.edit_text.call_args
    markup = kwargs["reply_markup"]
    confirm_cbs = [
        btn.callback_data
        for row in markup.inline_keyboard
        for btn in row
        if btn.callback_data
    ]
    assert any(c.startswith("t_delfc:") for c in confirm_cbs)


@pytest.mark.asyncio
async def test_handle_delete_with_files_confirm_calls_answer_once(fake_torrent):
    """BUG-14/DEAD-03: t_delfc actually deletes with files, exactly one answer."""
    from bot.handlers import downloads

    qbt = AsyncMock()
    qbt.get_torrent_by_short_hash = AsyncMock(return_value=fake_torrent)
    qbt.get_torrent = AsyncMock(return_value=fake_torrent)
    qbt.delete = AsyncMock()
    qbt.get_torrents = AsyncMock(return_value=[])

    cb = _make_callback(f"t_delfc:{fake_torrent.hash}")

    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)):
        await downloads.handle_delete_with_files_confirm(cb, is_admin=True)

    assert cb.answer.call_count == 1
    qbt.delete.assert_awaited_once_with([fake_torrent.hash], delete_files=True)


@pytest.mark.asyncio
async def test_handle_delete_with_files_confirm_requires_admin(fake_torrent):
    """SEC: non-admin must not be able to confirm a with-files delete."""
    from bot.handlers import downloads

    qbt = AsyncMock()
    qbt.delete = AsyncMock()

    cb = _make_callback(f"t_delfc:{fake_torrent.hash}")

    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)):
        await downloads.handle_delete_with_files_confirm(cb, is_admin=False)

    qbt.delete.assert_not_awaited()
    cb.answer.assert_awaited_once()


# ============================================================================
# LOGIC-01: filter survives pagination/refresh/back
# ============================================================================


def _make_torrent_page_callback(page: int, flt: str) -> MagicMock:
    """Build a CallbackQuery-like mock carrying a packed TorrentPageCB."""
    from bot.ui.callbacks import TorrentPageCB

    cb = MagicMock()
    cb.data = TorrentPageCB(page=page, flt=flt).pack()
    cb.answer = AsyncMock()
    message = MagicMock()
    message.edit_text = AsyncMock()
    cb.message = message
    return cb


def _torrents(n: int, state=TorrentState.DOWNLOADING) -> list[TorrentInfo]:
    return [
        TorrentInfo(hash=f"{'a' * 39}{i}", name=f"Torrent {i}", progress=0.1, state=state)
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_torrent_page_callback_preserves_filter():
    """LOGIC-01: paging a filtered list keeps the filter (not silently ALL)."""
    from bot.handlers import downloads
    from bot.models import TorrentFilter
    from bot.ui.callbacks import TorrentPageCB

    qbt = AsyncMock()
    qbt.get_torrents = AsyncMock(return_value=_torrents(10))

    cb = _make_torrent_page_callback(page=1, flt=TorrentFilter.DOWNLOADING.value)

    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)):
        await downloads.handle_torrent_page(cb, TorrentPageCB.unpack(cb.data))

    # get_torrents must have been called with the DOWNLOADING filter, not ALL
    qbt.get_torrents.assert_awaited_once_with(filter_type=TorrentFilter.DOWNLOADING)
    cb.answer.assert_awaited_once()
    cb.message.edit_text.assert_awaited_once()
    args, _ = cb.message.edit_text.call_args
    assert "Загружаются" in args[0]


@pytest.mark.asyncio
async def test_torrent_page_callback_back_button_keeps_filter():
    """LOGIC-01: torrent_details' back button carries the filter into TorrentPageCB."""
    from bot.ui.callbacks import TorrentPageCB
    from bot.ui.keyboards import Keyboards
    from bot.models import TorrentFilter

    torrent = _torrents(1)[0]
    kb = Keyboards.torrent_details(torrent, current_filter=TorrentFilter.SEEDING)
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]
    back_cbs = [c for c in cbs if c.startswith("tpg:")]
    assert back_cbs, f"no TorrentPageCB back button in {cbs}"
    unpacked = TorrentPageCB.unpack(back_cbs[0])
    assert unpacked.flt == TorrentFilter.SEEDING.value


@pytest.mark.asyncio
async def test_legacy_t_page_tells_user_to_refresh():
    """TEST-08a/LOGIC-01: old plain t_page:N buttons can't carry a filter —
    must not silently render an unfiltered list; must prompt a refresh."""
    from bot.handlers import downloads

    cb = _make_callback("t_page:1")

    await downloads.handle_legacy_page(cb)

    cb.answer.assert_awaited_once()
    args, kwargs = cb.answer.call_args
    assert "устарел" in (args[0] if args else kwargs.get("text", "")).lower()
    cb.message.edit_text.assert_not_called()


@pytest.mark.asyncio
async def test_torrent_page_callback_garbage_page_shows_alert():
    """TEST-08a: an out-of-range page number must not render, must alert."""
    from bot.handlers import downloads
    from bot.models import TorrentFilter
    from bot.ui.callbacks import TorrentPageCB

    qbt = AsyncMock()
    qbt.get_torrents = AsyncMock(return_value=_torrents(3))  # 1 page only

    cb = _make_torrent_page_callback(page=99, flt=TorrentFilter.ALL.value)

    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)):
        await downloads.handle_torrent_page(cb, TorrentPageCB.unpack(cb.data))

    cb.answer.assert_awaited_once()
    args, kwargs = cb.answer.call_args
    assert kwargs.get("show_alert") is True
    cb.message.edit_text.assert_not_called()


@pytest.mark.asyncio
async def test_torrent_page_callback_valid_page_renders():
    """TEST-08a: a valid page number renders and acks exactly once."""
    from bot.handlers import downloads
    from bot.models import TorrentFilter
    from bot.ui.callbacks import TorrentPageCB

    qbt = AsyncMock()
    qbt.get_torrents = AsyncMock(return_value=_torrents(10))

    cb = _make_torrent_page_callback(page=0, flt=TorrentFilter.ALL.value)

    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)):
        await downloads.handle_torrent_page(cb, TorrentPageCB.unpack(cb.data))

    assert cb.answer.call_count == 1
    cb.message.edit_text.assert_awaited_once()


# ============================================================================
# LOGIC-02/BUG-04a: pause_all/resume_all/speed_set — exactly one answer,
# render helpers instead of re-invoking another callback handler.
# ============================================================================


@pytest.mark.asyncio
async def test_handle_pause_all_calls_answer_once_and_redraws():
    from bot.handlers import downloads

    qbt = AsyncMock()
    qbt.pause = AsyncMock()
    qbt.get_torrents = AsyncMock(return_value=[])

    cb = _make_callback("t_pause_all")

    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)):
        await downloads.handle_pause_all(cb, is_admin=True)

    assert cb.answer.call_count == 1
    qbt.pause.assert_awaited_once_with("all")
    cb.message.edit_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_pause_all_requires_admin():
    """SEC-06: pausing every torrent is a blanket action - admin only."""
    from bot.handlers import downloads

    qbt = AsyncMock()
    qbt.pause = AsyncMock()

    cb = _make_callback("t_pause_all")

    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)):
        await downloads.handle_pause_all(cb, is_admin=False)

    qbt.pause.assert_not_awaited()
    cb.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_resume_all_calls_answer_once_and_redraws():
    from bot.handlers import downloads

    qbt = AsyncMock()
    qbt.resume = AsyncMock()
    qbt.get_torrents = AsyncMock(return_value=[])

    cb = _make_callback("t_resume_all")

    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)):
        await downloads.handle_resume_all(cb, is_admin=True)

    assert cb.answer.call_count == 1
    qbt.resume.assert_awaited_once_with("all")
    cb.message.edit_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_resume_all_requires_admin():
    """SEC-06: resuming every torrent is a blanket action - admin only."""
    from bot.handlers import downloads

    qbt = AsyncMock()
    qbt.resume = AsyncMock()

    cb = _make_callback("t_resume_all")

    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)):
        await downloads.handle_resume_all(cb, is_admin=False)

    qbt.resume.assert_not_awaited()
    cb.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_speed_set_calls_answer_once_and_redraws():
    """LOGIC-02/BUG-04a: speed_set must not double-answer via handle_speed_menu."""
    from bot.handlers import downloads
    from bot.models import QBittorrentStatus

    qbt = AsyncMock()
    qbt.set_download_limit = AsyncMock()
    qbt.get_status = AsyncMock(
        return_value=QBittorrentStatus(
            version="4.6.0",
            connection_status="connected",
            download_limit=1024 * 1024,
            upload_limit=0,
        )
    )

    cb = _make_callback("speed:dl:1024")

    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)):
        await downloads.handle_speed_set(cb)

    assert cb.answer.call_count == 1
    qbt.set_download_limit.assert_awaited_once_with(1024 * 1024)
    cb.message.edit_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_speed_menu_passes_current_limits_to_keyboard():
    """LOGIC-03: the ✓ marker must reflect the real qBit-reported limits."""
    from bot.handlers import downloads
    from bot.models import QBittorrentStatus

    qbt = AsyncMock()
    qbt.get_status = AsyncMock(
        return_value=QBittorrentStatus(
            version="4.6.0",
            connection_status="connected",
            download_limit=1024 * 1024,  # 1 MB/s
            upload_limit=0,
        )
    )

    cb = _make_callback("speed_menu")

    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)):
        await downloads.handle_speed_menu(cb)

    cb.message.edit_text.assert_awaited_once()
    _, kwargs = cb.message.edit_text.call_args
    markup = kwargs["reply_markup"]
    # download row is presets[:3] + presets[3:] -> rows 1-2 after the "⬇️" label row
    dl_rows = markup.inline_keyboard[1:3]
    dl_marked = [btn.text for row in dl_rows for btn in row if btn.text.startswith("✓")]
    # The 1 MB/s download preset should carry the checkmark, not "Без лимита".
    assert any("1 МБ/с" in t for t in dl_marked)
    assert not any("Без лимита" in t for t in dl_marked)
    assert cb.answer.call_count == 1


# ============================================================================
# BUG-12a: html.escape user-controlled args in "not found" messages
# ============================================================================


@pytest.mark.asyncio
async def test_cmd_pause_escapes_unfound_args():
    """BUG-12a: HTML-special characters in /pause args must not break parse_mode=HTML."""
    from bot.handlers import downloads

    qbt = AsyncMock()
    qbt.get_torrent_by_short_hash = AsyncMock(return_value=None)

    message = MagicMock()
    message.text = "/pause <script>"
    message.answer = AsyncMock()

    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)):
        await downloads.cmd_pause(message, db_user=MagicMock(), is_admin=True)

    message.answer.assert_awaited_once()
    (sent_text,), _ = message.answer.call_args
    assert "<script>" not in sent_text
    assert "&lt;script&gt;" in sent_text


@pytest.mark.asyncio
async def test_cmd_resume_escapes_unfound_args():
    """BUG-12a: same escaping for /resume."""
    from bot.handlers import downloads

    qbt = AsyncMock()
    qbt.get_torrent_by_short_hash = AsyncMock(return_value=None)

    message = MagicMock()
    message.text = "/resume <b>x</b>"
    message.answer = AsyncMock()

    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)):
        await downloads.cmd_resume(message, db_user=MagicMock(), is_admin=True)

    message.answer.assert_awaited_once()
    (sent_text,), _ = message.answer.call_args
    assert "<b>x</b>" not in sent_text
    assert "&lt;b&gt;x&lt;/b&gt;" in sent_text


# ============================================================================
# SEC-06: /pause all and /resume all require admin; targeted pause/resume
# remain open to all allowed users.
# ============================================================================


@pytest.mark.asyncio
async def test_cmd_pause_all_requires_admin():
    from bot.handlers import downloads

    qbt = AsyncMock()
    qbt.pause = AsyncMock()

    message = MagicMock()
    message.text = "/pause all"
    message.answer = AsyncMock()

    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)):
        await downloads.cmd_pause(message, db_user=MagicMock(), is_admin=False)

    qbt.pause.assert_not_awaited()
    message.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_cmd_pause_all_allowed_for_admin():
    from bot.handlers import downloads

    qbt = AsyncMock()
    qbt.pause = AsyncMock()

    message = MagicMock()
    message.text = "/pause all"
    message.answer = AsyncMock()

    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)):
        await downloads.cmd_pause(message, db_user=MagicMock(), is_admin=True)

    qbt.pause.assert_awaited_once_with("all")


@pytest.mark.asyncio
async def test_cmd_resume_all_requires_admin():
    from bot.handlers import downloads

    qbt = AsyncMock()
    qbt.resume = AsyncMock()

    message = MagicMock()
    message.text = "/resume all"
    message.answer = AsyncMock()

    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)):
        await downloads.cmd_resume(message, db_user=MagicMock(), is_admin=False)

    qbt.resume.assert_not_awaited()
    message.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_cmd_pause_single_torrent_open_to_non_admin():
    """SEC-06: targeted pause of one named torrent stays open to all allowed users."""
    from bot.handlers import downloads

    fake = TorrentInfo(hash="a" * 40, name="X", progress=0.1, state=TorrentState.DOWNLOADING)
    qbt = AsyncMock()
    qbt.get_torrent_by_short_hash = AsyncMock(return_value=fake)
    qbt.pause = AsyncMock()

    message = MagicMock()
    message.text = "/pause aaaaaaaa"
    message.answer = AsyncMock()

    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)):
        await downloads.cmd_pause(message, db_user=MagicMock(), is_admin=False)

    qbt.pause.assert_awaited_once_with([fake.hash])


# ============================================================================
# LOGIC-13: /pause@botname / /resume@botname must not leak "@botname" into args
# ============================================================================


@pytest.mark.asyncio
async def test_cmd_pause_strips_botname_suffix():
    """RED test (LOGIC-13): a naive text.replace("/pause", "") left "@botname all"
    as the args, so /pause@botname all tried to resolve "@botname all" as a
    torrent hash instead of running the admin "pause all" branch.
    """
    from bot.handlers import downloads

    qbt = AsyncMock()
    qbt.pause = AsyncMock()
    qbt.get_torrent_by_short_hash = AsyncMock(side_effect=AssertionError("should not look up a torrent"))

    message = MagicMock()
    message.text = "/pause@botname all"
    message.answer = AsyncMock()

    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)):
        await downloads.cmd_pause(message, db_user=MagicMock(), is_admin=True)

    qbt.pause.assert_awaited_once_with("all")


@pytest.mark.asyncio
async def test_cmd_resume_strips_botname_suffix():
    from bot.handlers import downloads

    qbt = AsyncMock()
    qbt.resume = AsyncMock()
    qbt.get_torrent_by_short_hash = AsyncMock(side_effect=AssertionError("should not look up a torrent"))

    message = MagicMock()
    message.text = "/resume@botname all"
    message.answer = AsyncMock()

    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)):
        await downloads.cmd_resume(message, db_user=MagicMock(), is_admin=True)

    qbt.resume.assert_awaited_once_with("all")


# ============================================================================
# PERF-05: full-hash callback_data resolves via get_torrent (no full-list scan)
# ============================================================================


@pytest.mark.asyncio
async def test_handle_torrent_details_full_hash_uses_targeted_lookup():
    """PERF-05: a 40-hex callback hash should resolve via get_torrent, not a
    full-list scan through get_torrent_by_short_hash."""
    from bot.handlers import downloads

    full_hash = "b" * 40
    fake = TorrentInfo(hash=full_hash, name="Y", progress=0.2, state=TorrentState.DOWNLOADING)
    qbt = AsyncMock()
    qbt.get_torrent = AsyncMock(return_value=fake)
    qbt.get_torrent_by_short_hash = AsyncMock(side_effect=AssertionError("should not scan full list"))

    cb = _make_callback(f"t:{full_hash}")

    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)):
        await downloads.handle_torrent_details(cb)

    qbt.get_torrent.assert_awaited_once_with(full_hash)
    qbt.get_torrent_by_short_hash.assert_not_called()


@pytest.mark.asyncio
async def test_handle_torrent_details_short_hash_falls_back_to_scan():
    """PERF-05: legacy 16-char callback hash still resolves via the fallback scan."""
    from bot.handlers import downloads

    short_hash = "c" * 16
    fake = TorrentInfo(hash="c" * 40, name="Z", progress=0.3, state=TorrentState.DOWNLOADING)
    qbt = AsyncMock()
    qbt.get_torrent_by_short_hash = AsyncMock(return_value=fake)

    cb = _make_callback(f"t:{short_hash}")

    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)):
        await downloads.handle_torrent_details(cb)

    qbt.get_torrent_by_short_hash.assert_awaited_once_with(short_hash)
