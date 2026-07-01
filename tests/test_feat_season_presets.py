"""Feature #2: season-monitoring presets when grabbing/adding a series."""

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

    session = SearchSession(
        user_id=1, query="q", content_type=ContentType.SERIES,
        selected_result=SearchResult(guid="g", title="t"),
    )
    db = AsyncMock()
    db.get_session = AsyncMock(return_value=session)
    db.update_session = AsyncMock(return_value=True)

    cb = MagicMock()
    cb.data = "season_set:future"
    cb.from_user = MagicMock(id=1)
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()

    with patch.object(search, "get_services", AsyncMock(return_value=(MagicMock(), MagicMock(), None))):
        await search.handle_season_preset(cb, db_user=MagicMock(), db=db)

    assert session.monitor_type == "future"
    db.update_session.assert_awaited()
