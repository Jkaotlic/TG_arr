"""Batch B1, review fix round 1.

1. "◀️ Назад" on a release card cleared `selected_content`, so every grab
   button on the freshly drawn list answered "Список устарел".
2. Grab handlers read the session before `_claim_grab`; they must re-check it
   after the claim (a concurrent grab may have deleted/replaced it).
6. A 404 on the retry grab after refetch means the release is gone.
"""

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.clients.base import APIError
from bot.models import ContentType, MovieInfo, SearchResult, SearchSession, User, UserPreferences
from bot.ui.list_token import STALE_LIST_TEXT

TOK = "tok123"


def _rel(guid: str) -> SearchResult:
    return SearchResult(guid=guid, title=f"R.{guid}", origin="arr", indexer_id=1, size=1)


def _session() -> SearchSession:
    return SearchSession(
        user_id=42, query="q", content_type=ContentType.MOVIE,
        results=[_rel("r0"), _rel("r1")], selected_result=_rel("r0"),
        selected_content=MovieInfo(tmdb_id=1, title="Movie", year=2024, radarr_id=15),
        list_token=TOK,
    )


class _StatefulDb:
    """Keeps the saved session, like the real DB does."""

    def __init__(self, session):
        self.session = session
        self.save_session = AsyncMock(side_effect=self._save)
        self.get_session = AsyncMock(side_effect=self._get)
        self.update_session = AsyncMock(return_value=True)
        self.delete_session = AsyncMock()
        self.log_action = AsyncMock()

    async def _save(self, _uid, s):
        self.session = s

    async def _get(self, _uid):
        return self.session

    def session_lock(self, _uid):
        @contextlib.asynccontextmanager
        async def _cm():
            yield

        return _cm()


def _cb() -> MagicMock:
    cb = MagicMock()
    cb.from_user = MagicMock(id=42)
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    return cb


def _user() -> User:
    return User(tg_id=42, preferences=UserPreferences(auto_grab_enabled=True))


def _stale(cb) -> bool:
    return any(c.args and c.args[0] == STALE_LIST_TEXT for c in cb.answer.await_args_list)


@pytest.fixture(autouse=True)
def _clear_guard():
    from bot.handlers import search

    search._grab_in_progress.clear()
    yield
    search._grab_in_progress.clear()


@pytest.mark.asyncio
async def test_back_then_another_release_still_grabs():
    from bot.handlers import search
    from bot.ui.callbacks import GrabCB

    db = _StatefulDb(_session())
    with patch.object(search, "_render_results_page", AsyncMock()):
        await search.handle_back(_cb(), _user(), db)

    assert db.session.selected_content is not None
    assert db.session.selected_result is None

    cb = _cb()
    with patch.object(search, "get_services", AsyncMock(return_value=(MagicMock(), MagicMock()))), \
         patch.object(search, "grab_release", AsyncMock()) as gr:
        await search.handle_confirm_grab(cb, GrabCB(tok=TOK, idx=1), _user(), db)

    assert not _stale(cb)
    gr.assert_awaited_once()
    assert gr.await_args.args[1].selected_result.guid == "r1"


@pytest.mark.asyncio
async def test_back_then_grab_best_still_works():
    from bot.handlers import search
    from bot.ui.callbacks import GrabBestCB

    db = _StatefulDb(_session())
    with patch.object(search, "_render_results_page", AsyncMock()):
        await search.handle_back(_cb(), _user(), db)

    cb = _cb()
    with patch.object(search, "get_services", AsyncMock(return_value=(MagicMock(), MagicMock()))), \
         patch.object(search, "grab_release", AsyncMock()) as gr:
        await search.handle_grab_best(cb, GrabBestCB(tok=TOK), _user(), db)

    assert not _stale(cb)
    gr.assert_awaited_once()


def _claim_then_swap(search, db, new_session):
    """A `_claim_grab` that lets a concurrent action replace the session
    between the handler's first read and its claim."""
    real = search._claim_grab

    async def _claim(uid):
        db.session = new_session
        return await real(uid)

    return patch.object(search, "_claim_grab", _claim)


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", ["confirm", "force", "auto"])
async def test_grab_handlers_recheck_the_session_after_the_claim(handler):
    from bot.handlers import search
    from bot.ui.callbacks import AutoSearchCB, ForceGrabCB, GrabCB

    db = _StatefulDb(_session())
    newer = _session()
    newer.list_token = "newtok"  # a new search landed meanwhile
    cb = _cb()
    add_service = MagicMock()
    add_service.qbittorrent = MagicMock()

    with patch.object(search, "get_services", AsyncMock(return_value=(MagicMock(), add_service))), \
         patch.object(search, "grab_release", AsyncMock()) as gr, \
         patch.object(search, "_execute_grab", AsyncMock()) as ex, \
         _claim_then_swap(search, db, newer):
        if handler == "confirm":
            await search.handle_confirm_grab(cb, GrabCB(tok=TOK, idx=0), _user(), db)
        elif handler == "force":
            await search.handle_force_grab(cb, ForceGrabCB(tok=TOK, idx=0), _user(), db)
        else:
            await search.handle_auto_search(cb, AutoSearchCB(tok=TOK, idx=0), _user(), db)

    gr.assert_not_awaited()
    ex.assert_not_awaited()
    assert _stale(cb)
    assert search._grab_in_progress == set()  # the claim was released


@pytest.mark.asyncio
async def test_404_on_the_retry_grab_is_gone_not_unreachable():
    from bot.services.add_service import GRAB_OUTCOME_GONE, RELEASE_GONE_TEXT, AddService, grab_outcome

    radarr = AsyncMock()
    radarr.grab_release.side_effect = [APIError("404", status_code=404), APIError("404", status_code=404)]
    radarr.get_releases.return_value = [_rel("g1")]
    service = AddService(radarr, AsyncMock())

    ok, action = await service.grab_release(_rel("g1"), ContentType.MOVIE, arr_id=15)

    assert ok is False
    assert radarr.grab_release.await_count == 2
    assert grab_outcome(action) == GRAB_OUTCOME_GONE
    assert action.error_message == RELEASE_GONE_TEXT


def test_free_search_targets_are_a_set():
    from bot.handlers.search import free

    assert free._ARR_TARGETS == {ContentType.MOVIE, ContentType.SERIES, ContentType.ANIME}
