"""Feature #2: season-monitoring presets when grabbing/adding a series."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.models import ContentType, SearchResult, SearchSession


def test_decide_monitor_type_override_wins():
    """An explicit user preset must override the auto-decided monitor type."""
    from bot.handlers.search import _decide_monitor_type

    result = SearchResult(guid="g", title="t", detected_season=2, is_season_pack=False)
    # auto would be "none"; the user preset must win
    assert _decide_monitor_type(result, force_download=False, override="future") == "future"
    assert _decide_monitor_type(result, force_download=False, override=None) == "none"


def test_season_presets_keyboard_offers_all_presets():
    from bot.ui.keyboards import Keyboards

    kb = Keyboards.season_presets()
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]
    joined = " ".join(cbs)
    for preset in ("all", "future", "latestSeason", "firstSeason"):
        assert preset in joined, f"missing preset {preset}"


@pytest.mark.asyncio
async def test_handle_season_preset_stores_choice_in_session():
    from bot.handlers import search
    from bot.ui.callbacks import SeasonPresetCB

    session = SearchSession(
        user_id=1, query="q", content_type=ContentType.SERIES,
        selected_result=SearchResult(guid="g", title="t"),
    )
    db = AsyncMock()
    db.get_session = AsyncMock(return_value=session)
    db.update_session = AsyncMock(return_value=True)
    db.session_lock = MagicMock(return_value=asyncio.Lock())  # DB-02: real lock, not AsyncMock

    cb = MagicMock()
    cb.data = None
    cb.from_user = MagicMock(id=1)
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()

    with patch.object(search, "get_services", AsyncMock(return_value=(MagicMock(), MagicMock()))):
        await search.handle_season_preset(cb, SeasonPresetCB(preset="future"), db_user=MagicMock(), db=db)

    assert session.monitor_type == "future"
    db.update_session.assert_awaited()


def test_season_presets_keyboard_back_uses_season_back_not_generic_back():
    """BUG-16: the season-picker's "Назад" must NOT be the generic
    CallbackData.BACK (which clears selected_result/selected_content and
    returns to the results list) — it must be the dedicated SEASON_BACK."""
    from bot.ui.keyboards import CallbackData, Keyboards

    kb = Keyboards.season_presets()
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]
    assert CallbackData.SEASON_BACK in cbs
    assert CallbackData.BACK not in cbs


@pytest.mark.asyncio
async def test_season_back_preserves_selected_result_and_shows_release_card():
    """BUG-16 RED->GREEN: season_back must NOT clear the selection — it must
    redraw the release card (like handle_season_preset's return path), not
    fall through to handle_back's "clear selection, show results list"."""
    from bot.handlers import search

    result = SearchResult(guid="g", title="Test.Release", calculated_score=50)
    session = SearchSession(
        user_id=1, query="q", content_type=ContentType.SERIES,
        results=[result], selected_result=result, monitor_type="all",
    )
    db = AsyncMock()
    db.get_session = AsyncMock(return_value=session)
    db.save_session = AsyncMock()

    cb = MagicMock()
    cb.data = "season_back"
    cb.from_user = MagicMock(id=1)
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()

    add_service = MagicMock()
    add_service.qbittorrent = None
    with patch.object(search, "get_services", AsyncMock(return_value=(MagicMock(), add_service))):
        await search.handle_season_back(cb, db_user=MagicMock(), db=db)

    # Selection must survive — this is the whole point of the fix.
    assert session.selected_result is result
    db.save_session.assert_not_awaited()  # nothing was mutated, nothing to persist

    # Must show the release card (title present), not the bare results list.
    sent_text = cb.message.edit_text.await_args.args[0]
    assert "Test.Release" in sent_text
    cb.answer.assert_awaited()
