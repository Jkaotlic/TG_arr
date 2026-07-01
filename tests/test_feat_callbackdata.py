"""Feature #1: typed CallbackData factory for pagination (LOGIC-14 collision class)."""

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
