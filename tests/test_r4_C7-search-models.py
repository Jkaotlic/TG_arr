"""Round-4 cluster C7 — search/models dead-code removal + pagination write reduction.

Covers:
- DEAD-01: bot/constants.py is deleted (no importable module).
- DEAD-10: UserPreferences.language removed.
- DEAD-11: SearchSession.monitor_type removed.
- PERF-06: handle_pagination/handle_back avoid redundant save_session writes.
"""

import importlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.models import ContentType, SearchResult, SearchSession, UserPreferences, User


# ---------------------------------------------------------------------------
# DEAD-01: bot/constants.py must be gone (orphaned module, zero importers)
# ---------------------------------------------------------------------------
def test_bot_constants_module_removed():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("bot.constants")


# ---------------------------------------------------------------------------
# DEAD-10: UserPreferences.language removed
# ---------------------------------------------------------------------------
def test_user_preferences_has_no_language_field():
    assert "language" not in UserPreferences.model_fields
    prefs = UserPreferences()
    assert not hasattr(prefs, "language")


def test_user_preferences_ignores_legacy_language_in_stored_json():
    # pydantic ignores unknown keys in stored JSON → old rows still load.
    prefs = UserPreferences.model_validate({"language": "ru", "auto_grab_enabled": True})
    assert prefs.auto_grab_enabled is True
    assert not hasattr(prefs, "language")


# ---------------------------------------------------------------------------
# Feature #2 (supersedes DEAD-11): SearchSession.monitor_type re-added and is now
# actually READ by search._decide_monitor_type as the user-chosen season preset.
# ---------------------------------------------------------------------------
def test_search_session_monitor_type_defaults_none():
    assert "monitor_type" in SearchSession.model_fields
    session = SearchSession(user_id=1, query="x", content_type=ContentType.MOVIE)
    assert session.monitor_type is None


def test_search_session_monitor_type_roundtrips_from_stored_json():
    session = SearchSession.model_validate(
        {
            "user_id": 1,
            "query": "x",
            "content_type": "movie",
            "monitor_type": "future",
        }
    )
    assert session.monitor_type == "future"


# ---------------------------------------------------------------------------
# PERF-06: pagination/back handlers avoid the redundant SQLite write
# ---------------------------------------------------------------------------
def _make_session(page: int = 0, with_selection: bool = False) -> SearchSession:
    results = [
        SearchResult(guid=f"g{i}", title=f"Title {i}", calculated_score=10)
        for i in range(12)
    ]
    session = SearchSession(
        user_id=42,
        query="dune",
        content_type=ContentType.MOVIE,
        results=results,
        current_page=page,
    )
    if with_selection:
        session.selected_result = results[0]
    return session


def _make_callback(data: str) -> MagicMock:
    callback = MagicMock()
    callback.data = data
    callback.from_user = MagicMock()
    callback.from_user.id = 42
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()
    callback.answer = AsyncMock()
    return callback


def _make_db_user() -> User:
    return User(tg_id=42, preferences=UserPreferences(auto_grab_enabled=False))


@pytest.mark.asyncio
async def test_pagination_skips_save_when_page_unchanged():
    """Tapping the page the user is already on must NOT re-serialize the session."""
    from bot.handlers import search

    session = _make_session(page=1)
    db = MagicMock()
    db.get_session = AsyncMock(return_value=session)
    db.save_session = AsyncMock()

    from bot.ui.callbacks import PageCB

    callback = _make_callback("")

    await search.handle_pagination(callback, PageCB(scope="search", page=1), _make_db_user(), db)  # same page

    db.save_session.assert_not_called()
    callback.answer.assert_awaited()


@pytest.mark.asyncio
async def test_pagination_saves_when_page_changes():
    """Navigating to a different page still persists current_page."""
    from bot.handlers import search

    session = _make_session(page=0)
    db = MagicMock()
    db.get_session = AsyncMock(return_value=session)
    db.save_session = AsyncMock()

    from bot.ui.callbacks import PageCB

    callback = _make_callback("")

    await search.handle_pagination(callback, PageCB(scope="search", page=1), _make_db_user(), db)  # different page

    db.save_session.assert_awaited_once()
    assert session.current_page == 1


@pytest.mark.asyncio
async def test_back_skips_save_when_no_selection_to_clear():
    """Back with nothing selected must not write the session again."""
    from bot.handlers import search

    session = _make_session(page=0, with_selection=False)
    db = MagicMock()
    db.get_session = AsyncMock(return_value=session)
    db.save_session = AsyncMock()

    callback = _make_callback(search.CallbackData.BACK)

    await search.handle_back(callback, _make_db_user(), db)

    db.save_session.assert_not_called()
    callback.answer.assert_awaited()


@pytest.mark.asyncio
async def test_back_saves_when_clearing_selection():
    """Back after selecting a release clears + persists the cleared selection."""
    from bot.handlers import search

    session = _make_session(page=0, with_selection=True)
    db = MagicMock()
    db.get_session = AsyncMock(return_value=session)
    db.save_session = AsyncMock()

    callback = _make_callback(search.CallbackData.BACK)

    await search.handle_back(callback, _make_db_user(), db)

    db.save_session.assert_awaited_once()
    assert session.selected_result is None
    assert session.selected_content is None
