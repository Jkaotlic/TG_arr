"""Round 2 / D2: the torrent list keeps only Обновить/Фильтр/⋯ Ещё at the
bottom; the blanket actions, speed and Close live in the "⋯ Ещё" sub-panel."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.models import TorrentFilter, TorrentInfo, TorrentState


def _torrent(i: int = 0) -> TorrentInfo:
    return TorrentInfo(hash=f"{'a' * 39}{i % 10}", name=f"T{i}", size=1, progress=0.5,
                       state=TorrentState.DOWNLOADING)


def _rows(kb) -> list[list[str]]:
    return [[b.text for b in row] for row in kb.inline_keyboard]


def _buttons(kb):
    return [b for row in kb.inline_keyboard for b in row]


def test_list_bottom_rows_are_refresh_filter_more_then_pagination():
    from bot.ui.keyboards import Keyboards

    torrents = [_torrent(i) for i in range(5)]
    for is_admin in (True, False):
        kb = Keyboards.torrent_list(torrents, 1, 3, TorrentFilter.SEEDING, is_admin=is_admin)
        rows = _rows(kb)
        assert len(rows) == 5 + 2  # per-torrent rows unchanged + actions + pagination
        assert rows[5] == ["🔄 Обновить", "🔍 Фильтр", "⋯ Ещё"]
        assert rows[6] == ["◀️", "2/3", "▶️"]


def test_list_without_pagination_ends_with_the_action_row():
    from bot.ui.keyboards import Keyboards

    rows = _rows(Keyboards.torrent_list([_torrent()], 0, 1, is_admin=True))
    assert rows[-1] == ["🔄 Обновить", "🔍 Фильтр", "⋯ Ещё"]
    assert len(rows) == 2


def test_more_button_carries_filter_and_page():
    from bot.ui.callbacks import TorrentMoreCB
    from bot.ui.keyboards import Keyboards

    kb = Keyboards.torrent_list([_torrent()], 2, 4, TorrentFilter.PAUSED, is_admin=False)
    more = next(b for b in _buttons(kb) if b.text == "⋯ Ещё")
    assert TorrentMoreCB.unpack(more.callback_data) == TorrentMoreCB(page=2, flt="paused")


def test_more_panel_admin_sees_all():
    from bot.ui.keyboards import CallbackData, Keyboards
    from bot.ui.keyboards._constants import BTN_BACK, BTN_CLOSE

    kb = Keyboards.torrent_more(TorrentFilter.ALL, 0, is_admin=True)
    assert _rows(kb) == [
        ["⏸ Пауза всех", "▶️ Возобновить все"],
        ["🚀 Скорость"],
        [BTN_BACK, BTN_CLOSE],
    ]
    btns = _buttons(kb)
    assert btns[0].callback_data == CallbackData.TORRENT_PAUSE_ALL
    assert btns[1].callback_data == CallbackData.TORRENT_RESUME_ALL
    assert btns[2].callback_data == CallbackData.SPEED_MENU
    assert btns[4].callback_data == CallbackData.CLOSE


def test_more_panel_non_admin_sees_only_navigation():
    from bot.ui.keyboards import Keyboards
    from bot.ui.keyboards._constants import BTN_BACK, BTN_CLOSE

    kb = Keyboards.torrent_more(TorrentFilter.ALL, 0, is_admin=False)
    assert _rows(kb) == [[BTN_BACK, BTN_CLOSE]]


def test_more_panel_back_restores_filter_and_page():
    from bot.ui.callbacks import TorrentPageCB
    from bot.ui.keyboards import Keyboards
    from bot.ui.keyboards._constants import BTN_BACK

    kb = Keyboards.torrent_more(TorrentFilter.STALLED, 3, is_admin=True)
    back = next(b for b in _buttons(kb) if b.text == BTN_BACK)
    assert TorrentPageCB.unpack(back.callback_data) == TorrentPageCB(page=3, flt="stalled")


def test_more_callbacks_fit_64_bytes():
    from bot.ui.callbacks import TorrentMoreCB
    from bot.ui.keyboards import Keyboards

    longest_flt = max((f.value for f in TorrentFilter), key=len)
    data = TorrentMoreCB(page=9999, flt=longest_flt).pack()
    assert len(data.encode()) <= 64
    for kb in (
        Keyboards.torrent_list([_torrent()], 9999, 10000, TorrentFilter(longest_flt), is_admin=True),
        Keyboards.torrent_more(TorrentFilter(longest_flt), 9999, is_admin=True),
    ):
        for b in _buttons(kb):
            assert len((b.callback_data or "").encode()) <= 64


@pytest.mark.asyncio
async def test_more_handler_swaps_keyboard_in_place_and_remembers_view():
    from bot.handlers import downloads
    from bot.ui.callbacks import TorrentMoreCB

    cb = MagicMock()
    cb.from_user = MagicMock(id=4242)
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.message.edit_reply_markup = AsyncMock()

    await downloads.handle_torrent_more(cb, TorrentMoreCB(page=1, flt="paused"), is_admin=False)

    cb.message.edit_text.assert_not_awaited()  # same message, list text kept
    kb = cb.message.edit_reply_markup.await_args.kwargs["reply_markup"]
    assert [b.text for row in kb.inline_keyboard for b in row] == ["◀️ Назад", "✖️ Закрыть"]
    cb.answer.assert_awaited_once()
    # pause-all/speed opened from the panel return to this same view
    assert downloads._view_of(4242) == (TorrentFilter.PAUSED, 1)


@pytest.mark.asyncio
async def test_more_then_back_redraws_same_filter_and_page():
    from bot.handlers import downloads
    from bot.ui.callbacks import TorrentPageCB

    torrents = [TorrentInfo(hash=f"{i:040x}", name=f"T{i}", size=1, progress=0.5,
                            state=TorrentState.PAUSED) for i in range(12)]
    qbt = MagicMock()
    qbt.get_torrents = AsyncMock(return_value=torrents)
    qbt.get_status = AsyncMock(side_effect=RuntimeError("no summary"))

    kb = downloads.Keyboards.torrent_more(TorrentFilter.PAUSED, 2, is_admin=True)
    back = next(b for b in _buttons(kb) if b.text == "◀️ Назад")

    cb = MagicMock()
    cb.from_user = MagicMock(id=4243)
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)):
        await downloads.handle_torrent_page(cb, TorrentPageCB.unpack(back.callback_data), is_admin=True)

    qbt.get_torrents.assert_awaited_once_with(filter_type=TorrentFilter.PAUSED)
    kb = cb.message.edit_text.await_args.kwargs["reply_markup"]
    assert ["◀️", "3/3"] in _rows(kb)
