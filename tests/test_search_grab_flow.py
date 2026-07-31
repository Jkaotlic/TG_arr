"""TEST-03: user-facing grab flow in bot/handlers/search.py.

Covers the previously-untested paths flagged in 08-testing-quality.md:
- handle_force_grab: force_download=True reaches _execute_grab; "qBittorrent
  не настроен" branch.
- handle_confirm_grab: dispatch to the music flow for ArtistInfo, movie flow
  for MovieInfo (BUG-27 regression guard — the music handler must not
  swallow non-music confirmations).
- _execute_grab: movie-path (only the series path had coverage before).

Also covers BUG-07 (search.py:handle_release_selection) — callback.answer()
must fire right after session validation, before any slower work.

Migration 2026-07-28: `_execute_grab` no longer looks the title up, picks a
quality profile or resolves a root folder — the title was already resolved (and
created in Scryer) before releases could be listed at all, and Scryer owns the
profile/folder. Grabbing is redeeming a candidate token.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.models import (
    ArtistInfo,
    ContentType,
    MovieInfo,
    SearchResult,
    SearchSession,
    User,
    UserPreferences,
)


def _make_callback(data: str = "") -> MagicMock:
    callback = MagicMock()
    callback.data = data
    callback.from_user = MagicMock()
    callback.from_user.id = 42
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()
    callback.message.answer = AsyncMock()
    callback.answer = AsyncMock()
    return callback


def _make_db_user(**prefs_kwargs) -> User:
    return User(tg_id=42, preferences=UserPreferences(**prefs_kwargs))


def _make_db(session=None) -> MagicMock:
    db = MagicMock()
    db.get_session = AsyncMock(return_value=session)
    db.save_session = AsyncMock()
    db.update_session = AsyncMock(return_value=True)
    db.delete_session = AsyncMock()
    db.log_action = AsyncMock()
    return db


def _null_lock():
    """A no-op async context manager standing in for db.session_lock(user_id)."""
    import contextlib

    @contextlib.asynccontextmanager
    async def _cm():
        yield

    return _cm()


@pytest.fixture(autouse=True)
def _clear_grab_guard():
    from bot.handlers import search

    search._grab_in_progress.clear()
    yield
    search._grab_in_progress.clear()


# ---------------------------------------------------------------------------
# handle_force_grab
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_force_grab_reaches_execute_grab_with_force_download_true():
    """force_download=True must actually reach _execute_grab (the button that
    bypasses *arr approval — previously had zero test coverage)."""
    from bot.handlers import search

    result = SearchResult(guid="g1", title="Test.Release", calculated_score=50)
    session = SearchSession(
        user_id=42, query="test", content_type=ContentType.MOVIE, results=[result],
        selected_result=result,
    )
    db = _make_db(session)
    db_user = _make_db_user()
    callback = _make_callback()

    add_service = MagicMock()
    add_service.qbittorrent = MagicMock()  # configured
    services = (MagicMock(), add_service)

    with patch.object(search, "get_services", AsyncMock(return_value=services)), \
         patch.object(search, "_execute_grab", AsyncMock()) as execute_grab:
        await search.handle_force_grab(callback, db_user, db)

    execute_grab.assert_awaited_once()
    assert execute_grab.await_args.kwargs.get("force_download") is True


@pytest.mark.asyncio
async def test_force_grab_without_qbittorrent_shows_not_configured_error():
    """LOGIC path: force-grab with no qBittorrent configured must tell the
    user plainly instead of attempting a grab."""
    from bot.handlers import search

    result = SearchResult(guid="g1", title="Test.Release", calculated_score=50)
    session = SearchSession(
        user_id=42, query="test", content_type=ContentType.MOVIE, results=[result],
        selected_result=result,
    )
    db = _make_db(session)
    db_user = _make_db_user()
    callback = _make_callback()

    add_service = MagicMock()
    add_service.qbittorrent = None  # not configured
    services = (MagicMock(), add_service)

    with patch.object(search, "get_services", AsyncMock(return_value=services)), \
         patch.object(search, "_execute_grab", AsyncMock()) as execute_grab:
        await search.handle_force_grab(callback, db_user, db)

    execute_grab.assert_not_awaited()
    sent = callback.message.edit_text.await_args_list[-1].args[0]
    assert "qBittorrent" in sent
    db.delete_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_force_grab_no_session_shows_expired_message():
    from bot.handlers import search

    db = _make_db(session=None)
    db_user = _make_db_user()
    callback = _make_callback()

    with patch.object(search, "get_services", AsyncMock(return_value=(MagicMock(), MagicMock()))):
        await search.handle_force_grab(callback, db_user, db)

    sent = callback.message.edit_text.await_args_list[-1].args[0]
    assert "истекла" in sent.lower() or "повторите" in sent.lower()


@pytest.mark.asyncio
async def test_force_grab_double_tap_second_call_rejected():
    """RACE-01 guard applies to force-grab too — Confirm→Force must not
    double-execute."""
    from bot.handlers import search

    result = SearchResult(guid="g1", title="Test.Release", calculated_score=50)
    session = SearchSession(
        user_id=42, query="test", content_type=ContentType.MOVIE, results=[result],
        selected_result=result,
    )
    db = _make_db(session)
    db_user = _make_db_user()

    assert await search._claim_grab(42) is True  # simulate an in-flight grab

    callback = _make_callback()
    with patch.object(search, "get_services", AsyncMock(return_value=(MagicMock(), MagicMock()))), \
         patch.object(search, "_execute_grab", AsyncMock()) as execute_grab:
        await search.handle_force_grab(callback, db_user, db)

    execute_grab.assert_not_awaited()
    answers = [c.args[0] for c in callback.answer.await_args_list if c.args]
    assert any("обраб" in a.lower() for a in answers)


# ---------------------------------------------------------------------------
# handle_confirm_grab — dispatch by session.selected_content type (BUG-27)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_confirm_grab_dispatches_artist_to_music_flow():
    """An ArtistInfo selected_content must be routed to the music handler,
    never treated as a movie/series grab."""
    from bot.handlers import search

    artist = ArtistInfo(mb_id="abc", name="Test Artist")
    session = SearchSession(
        user_id=42, query="test artist", content_type=ContentType.MUSIC,
    )
    session.selected_content = artist
    db = _make_db(session)
    db_user = _make_db_user()
    callback = _make_callback()

    with patch("bot.handlers.music.handle_confirm_music_add", AsyncMock()) as music_confirm, \
         patch.object(search, "_execute_grab", AsyncMock()) as execute_grab:
        await search.handle_confirm_grab(callback, db_user, db)

    music_confirm.assert_awaited_once()
    execute_grab.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirm_grab_dispatches_movie_to_grab_release():
    """A MovieInfo selected_content must go through the normal grab path, not
    the music handler (BUG-27 regression: music used to swallow this event)."""
    from bot.handlers import search

    result = SearchResult(guid="g1", title="Test.Release", calculated_score=50)
    movie = MovieInfo(title="Interstellar", tmdb_id=157336, year=2014)
    session = SearchSession(
        user_id=42, query="interstellar", content_type=ContentType.MOVIE, results=[result],
        selected_result=result,
    )
    session.selected_content = movie
    db = _make_db(session)
    db_user = _make_db_user()
    callback = _make_callback()

    services = (MagicMock(), MagicMock())
    with patch.object(search, "get_services", AsyncMock(return_value=services)), \
         patch("bot.handlers.music.handle_confirm_music_add", AsyncMock()) as music_confirm, \
         patch.object(search, "grab_release", AsyncMock()) as grab_release:
        await search.handle_confirm_grab(callback, db_user, db)

    grab_release.assert_awaited_once()
    music_confirm.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirm_grab_no_session_shows_expired():
    from bot.handlers import search

    db = _make_db(session=None)
    db_user = _make_db_user()
    callback = _make_callback()

    await search.handle_confirm_grab(callback, db_user, db)

    callback.answer.assert_awaited()
    answer_args = [c.args[0] for c in callback.answer.await_args_list if c.args]
    assert any("истекла" in a.lower() for a in answer_args)


# ---------------------------------------------------------------------------
# _execute_grab — Scryer edition
# ---------------------------------------------------------------------------
def _session_with_title(**overrides) -> SearchSession:
    release = SearchResult(
        guid="g1",
        title="Test.Release.2160p",
        indexer="RuTracker",
        candidate_token="cand-1",
        scryer_title_id="t1",
        queue_scope={"title": True},
    )
    data = dict(
        user_id=42,
        query="Interstellar 2014",
        content_type=ContentType.MOVIE,
        results=[release],
        selected_result=release,
        selected_content=MovieInfo(tmdb_id=157336, title="Interstellar", year=2014, scryer_id="t1"),
    )
    data.update(overrides)
    return SearchSession(**data)


@pytest.mark.asyncio
async def test_execute_grab_uses_the_session_title_without_a_lookup():
    from bot.handlers import search

    session = _session_with_title()
    db_user = _make_db_user()
    db = _make_db(session)
    message = MagicMock()
    message.edit_text = AsyncMock()

    search_service = MagicMock()
    add_service = MagicMock()
    add_service.grab_with_fallback = AsyncMock(
        return_value=(True, MagicMock(user_id=0), "Релиз поставлен в очередь на скачивание")
    )

    await search._execute_grab(message, session, db_user, db, search_service, add_service)

    add_service.grab_with_fallback.assert_awaited_once()
    args = add_service.grab_with_fallback.await_args.args
    assert args[0].scryer_id == "t1"
    # The chosen release leads the list; the rest are fallbacks in case it
    # turns out to be unfetchable (GRAB-01).
    assert args[1][0].candidate_token == "cand-1"
    assert args[2] == ContentType.MOVIE
    db.log_action.assert_awaited_once()
    db.delete_session.assert_awaited_once_with(42)


@pytest.mark.asyncio
async def test_execute_grab_propagates_force_download():
    from bot.handlers import search

    session = _session_with_title()
    db = _make_db(session)
    message = MagicMock()
    message.edit_text = AsyncMock()

    add_service = MagicMock()
    add_service.grab_with_fallback = AsyncMock(return_value=(True, MagicMock(user_id=0), "ok"))

    await search._execute_grab(
        message, session, _make_db_user(), db, MagicMock(), add_service, force_download=True
    )

    assert add_service.grab_with_fallback.await_args.kwargs["force_download"] is True


@pytest.mark.asyncio
async def test_execute_grab_without_a_resolved_title_asks_to_search_again():
    """A session persisted before the migration has no Scryer title id."""
    from bot.handlers import search

    session = _session_with_title(selected_content=None)
    db = _make_db(session)
    message = MagicMock()
    message.edit_text = AsyncMock()

    add_service = MagicMock()
    add_service.grab_with_fallback = AsyncMock()

    await search._execute_grab(message, session, _make_db_user(), db, MagicMock(), add_service)

    add_service.grab_with_fallback.assert_not_awaited()
    text = message.edit_text.await_args.args[0]
    assert "Scryer" in text
    db.delete_session.assert_awaited_once_with(42)


@pytest.mark.asyncio
async def test_execute_grab_failure_shows_the_service_message():
    from bot.handlers import search

    session = _session_with_title()
    db = _make_db(session)
    message = MagicMock()
    message.edit_text = AsyncMock()

    add_service = MagicMock()
    add_service.grab_with_fallback = AsyncMock(
        return_value=(False, MagicMock(user_id=0), "Scryer не принял релиз (CONFLICT)")
    )

    await search._execute_grab(message, session, _make_db_user(), db, MagicMock(), add_service)

    assert "CONFLICT" in message.edit_text.await_args.args[0]


# ---------------------------------------------------------------------------
# handle_release_selection — BUG-07 (ack first) and the no-second-lookup rule
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_release_selection_acks_before_rendering():
    """BUG-07: the tap feedback must not wait on anything slow."""
    from bot.handlers import search
    from bot.ui.callbacks import ReleaseCB

    call_order: list[str] = []
    session = _session_with_title(selected_result=None)
    db = _make_db(session)
    db.save_session = AsyncMock(side_effect=lambda *a, **kw: call_order.append("save_session"))

    callback = _make_callback()
    callback.answer = AsyncMock(side_effect=lambda *a, **kw: call_order.append("answer"))
    db.session_lock = MagicMock(return_value=_null_lock())

    add_service = MagicMock()
    add_service.qbittorrent = None

    with patch.object(search, "get_services", AsyncMock(return_value=(MagicMock(), add_service))), \
         patch.object(search, "_emby_library_note", AsyncMock(return_value="")):
        await search.handle_release_selection(callback, ReleaseCB(idx=0), _make_db_user(), db)

    assert call_order[0] == "answer"


@pytest.mark.asyncio
async def test_release_selection_does_not_hit_the_network_again():
    """The title was resolved during the search — selecting a release must be
    a pure render (plus the optional Emby probe)."""
    from bot.handlers import search
    from bot.ui.callbacks import ReleaseCB

    session = _session_with_title(selected_result=None)
    db = _make_db(session)
    db.session_lock = MagicMock(return_value=_null_lock())

    search_service = MagicMock()
    search_service.search_metadata = AsyncMock()
    add_service = MagicMock()
    add_service.qbittorrent = None
    add_service.ensure_title = AsyncMock()

    callback = _make_callback()

    with patch.object(search, "get_services", AsyncMock(return_value=(search_service, add_service))), \
         patch.object(search, "_emby_library_note", AsyncMock(return_value="")):
        await search.handle_release_selection(callback, ReleaseCB(idx=0), _make_db_user(), db)

    search_service.search_metadata.assert_not_awaited()
    add_service.ensure_title.assert_not_awaited()
    assert "Interstellar" in callback.message.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_release_selection_without_title_still_offers_the_grab_buttons():
    from bot.handlers import search
    from bot.ui.callbacks import ReleaseCB

    session = _session_with_title(selected_result=None, selected_content=None)
    db = _make_db(session)
    db.session_lock = MagicMock(return_value=_null_lock())

    add_service = MagicMock()
    add_service.qbittorrent = None
    callback = _make_callback()

    with patch.object(search, "get_services", AsyncMock(return_value=(MagicMock(), add_service))):
        await search.handle_release_selection(callback, ReleaseCB(idx=0), _make_db_user(), db)

    kwargs = callback.message.edit_text.await_args.kwargs
    assert kwargs["reply_markup"] is not None
