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
async def test_handle_delete_with_files_calls_answer_once(fake_torrent):
    """BUG-15: delete-with-files handler must call callback.answer exactly once."""
    from bot.handlers import downloads

    qbt = AsyncMock()
    qbt.get_torrent_by_short_hash = AsyncMock(return_value=fake_torrent)
    qbt.delete = AsyncMock()
    qbt.get_torrents = AsyncMock(return_value=[])

    cb = _make_callback(f"t_delf:{fake_torrent.hash[:8]}")

    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)):
        await downloads.handle_delete_with_files(cb, is_admin=True)

    assert cb.answer.call_count == 1
    qbt.delete.assert_awaited_once()
