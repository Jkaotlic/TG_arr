"""Feature #1: typed CallbackData factory for pagination (LOGIC-14 collision class)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.models import ContentType, SearchResult, SearchSession


def test_page_cb_roundtrip():
    from bot.ui.callbacks import PageCB

    packed = PageCB(scope="search", page=3).pack()
    got = PageCB.unpack(packed)
    assert got.scope == "search"
    assert got.page == 3


def test_search_results_nav_uses_typed_pagecb():
    from bot.ui.callbacks import PageCB
    from bot.ui.keyboards import Keyboards

    results = [SearchResult(guid=str(i), title=f"t{i}") for i in range(5)]
    kb = Keyboards.search_results(results, 0, 2, 5, False, 0)
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]
    page_cbs = [c for c in cbs if c.startswith("pg:")]
    assert page_cbs, f"no typed page callback in {cbs}"
    got = PageCB.unpack(page_cbs[0])
    assert got.scope == "search" and got.page == 1  # next page from page 0


@pytest.mark.asyncio
async def test_handle_pagination_reads_callback_data():
    from bot.handlers import search
    from bot.ui.callbacks import PageCB

    session = SearchSession(
        user_id=1, query="q", content_type=ContentType.MOVIE,
        results=[SearchResult(guid=str(i), title=f"t{i}") for i in range(10)],
    )
    db = AsyncMock()
    db.get_session = AsyncMock(return_value=session)
    db.save_session = AsyncMock()
    db.session_lock = MagicMock(return_value=asyncio.Lock())  # DB-02: real lock, not AsyncMock

    db_user = MagicMock()
    db_user.preferences = MagicMock(auto_grab_enabled=False)

    cb = MagicMock()
    cb.from_user = MagicMock(id=1)
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()

    await search.handle_pagination(cb, PageCB(scope="search", page=1), db_user, db)

    assert session.current_page == 1
    cb.message.edit_text.assert_awaited()


def test_torrent_page_cb_roundtrip():
    """LOGIC-01: TorrentPageCB field is named `flt` (not `filter`, which
    shadows aiogram's CallbackData.filter() classmethod and triggers a
    pydantic UserWarning)."""
    from bot.ui.callbacks import TorrentPageCB

    packed = TorrentPageCB(page=2, flt="downloading").pack()
    got = TorrentPageCB.unpack(packed)
    assert got.page == 2
    assert got.flt == "downloading"


def test_torrent_page_cb_does_not_shadow_parent_filter():
    """Regression guard: constructing TorrentPageCB must not warn."""
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        from bot.ui.callbacks import TorrentPageCB

        # Re-affirm the class is usable and the classmethod is intact.
        assert callable(TorrentPageCB.filter)
        TorrentPageCB(page=0, flt="all")


def test_torrent_list_pagination_buttons_use_typed_cb():
    """LOGIC-01/TEST-08a: pagination/refresh buttons carry the active filter
    via the typed TorrentPageCB, not the old plain t_page:N string."""
    from bot.models import TorrentFilter, TorrentInfo
    from bot.ui.callbacks import TorrentPageCB
    from bot.ui.keyboards import Keyboards

    torrents = [TorrentInfo(hash=f"{'a' * 39}{i}", name=f"T{i}", progress=0.1) for i in range(5)]
    kb = Keyboards.torrent_list(torrents, current_page=1, total_pages=3, current_filter=TorrentFilter.SEEDING)

    cbs = [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]
    typed_cbs = [c for c in cbs if c.startswith("tpg:")]
    assert typed_cbs, f"no typed TorrentPageCB button in {cbs}"
    for c in typed_cbs:
        unpacked = TorrentPageCB.unpack(c)
        assert unpacked.flt == TorrentFilter.SEEDING.value

    # Old plain string prefix must no longer be produced.
    assert not any(c.startswith("t_page:") for c in cbs)
