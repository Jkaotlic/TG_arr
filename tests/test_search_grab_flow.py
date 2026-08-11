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

Rollback 2026-08-10: `_execute_grab` no longer looks the title up, picks a
quality profile or resolves a root folder — the title (and its Radarr/Sonarr
id) was already resolved before releases could be listed at all; see
bot/handlers/search/commands.py's module docstring for how a query becomes a
library entry. Grabbing is `AddService.grab_release` for the one release the
user selected.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.models import (
    ActionLog,
    ActionType,
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
# _execute_grab — rollback 2026-08-10: AddService.grab_release(release,
# content_type, arr_id=...) replaces Scryer's grab_with_fallback(title,
# candidates, content_type) — one release, not a multi-candidate fallback
# list, and (bool, ActionLog) instead of (bool, ActionLog, msg).
# ---------------------------------------------------------------------------
def _session_with_title(**overrides) -> SearchSession:
    release = SearchResult(
        guid="g1",
        title="Test.Release.2160p",
        indexer="RuTracker",
        origin="arr",
        indexer_id=7,
    )
    data = dict(
        user_id=42,
        query="Interstellar 2014",
        content_type=ContentType.MOVIE,
        results=[release],
        selected_result=release,
        selected_content=MovieInfo(tmdb_id=157336, title="Interstellar", year=2014, radarr_id=99),
    )
    data.update(overrides)
    return SearchSession(**data)


def _grab_action(**overrides) -> ActionLog:
    fields = dict(
        user_id=0, action_type=ActionType.GRAB, content_type=ContentType.MOVIE, success=True,
    )
    fields.update(overrides)
    return ActionLog(**fields)


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
    add_service.grab_release = AsyncMock(return_value=(True, _grab_action()))

    await search._execute_grab(message, session, db_user, db, search_service, add_service)

    add_service.grab_release.assert_awaited_once()
    args = add_service.grab_release.await_args.args
    kwargs = add_service.grab_release.await_args.kwargs
    assert args[0].guid == "g1"  # the release the user selected, not re-looked-up
    assert args[1] == ContentType.MOVIE
    assert kwargs["arr_id"] == 99  # the radarr_id already on the session's title
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
    add_service.grab_release = AsyncMock(return_value=(True, _grab_action()))

    await search._execute_grab(
        message, session, _make_db_user(), db, MagicMock(), add_service, force_download=True
    )

    assert add_service.grab_release.await_args.kwargs["force_download"] is True


@pytest.mark.asyncio
async def test_execute_grab_without_a_resolved_title_asks_to_search_again():
    """A session with no resolved arr_id (predates this rollback, or the
    title vanished from the library between search and grab)."""
    from bot.handlers import search

    session = _session_with_title(selected_content=None)
    db = _make_db(session)
    message = MagicMock()
    message.edit_text = AsyncMock()

    add_service = MagicMock()
    add_service.grab_release = AsyncMock()

    await search._execute_grab(message, session, _make_db_user(), db, MagicMock(), add_service)

    add_service.grab_release.assert_not_awaited()
    text = message.edit_text.await_args.args[0]
    assert "Radarr" in text or "Sonarr" in text
    assert "Scryer" not in text
    db.delete_session.assert_awaited_once_with(42)


@pytest.mark.asyncio
async def test_execute_grab_failure_shows_the_service_message():
    from bot.handlers import search

    session = _session_with_title()
    db = _make_db(session)
    message = MagicMock()
    message.edit_text = AsyncMock()

    add_service = MagicMock()
    add_service.grab_release = AsyncMock(
        return_value=(False, _grab_action(success=False, error_message="*arr не принял релиз (CONFLICT)"))
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
    search_service.radarr.lookup_movie = AsyncMock()
    search_service.sonarr.lookup_series = AsyncMock()
    add_service = MagicMock()
    add_service.qbittorrent = None
    add_service.add_movie = AsyncMock()
    add_service.add_series = AsyncMock()

    callback = _make_callback()

    with patch.object(search, "get_services", AsyncMock(return_value=(search_service, add_service))), \
         patch.object(search, "_emby_library_note", AsyncMock(return_value="")):
        await search.handle_release_selection(callback, ReleaseCB(idx=0), _make_db_user(), db)

    search_service.radarr.lookup_movie.assert_not_awaited()
    search_service.sonarr.lookup_series.assert_not_awaited()
    add_service.add_movie.assert_not_awaited()
    add_service.add_series.assert_not_awaited()
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


# ---------------------------------------------------------------------------
# Task 9: SearchService.search_releases_for_title — *arr's verdict wins
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_releases_are_ordered_by_the_arr_verdict_not_local_heuristics():
    """*arr's score is authoritative; rejected releases sink to the bottom."""
    from bot.models import ContentType, QualityInfo, SearchResult
    from bot.services.search_service import SearchService

    rejected_but_big = SearchResult(
        title="Дюна 2160p DUB", download_url="u1", indexer="i", guid="g1",
        size=60_000_000_000, seeders=900, leechers=0, quality=QualityInfo(resolution="2160p"),
        custom_format_score=-1000, rejected=True,
        rejections=["English is wanted, but found Russian"],
    )
    accepted = SearchResult(
        title="Dune 2160p BluRay", download_url="u2", indexer="i", guid="g2",
        size=50_000_000_000, seeders=10, leechers=0, quality=QualityInfo(resolution="2160p"),
        custom_format_score=500, rejected=False,
    )

    radarr = AsyncMock()
    radarr.get_releases.return_value = [rejected_but_big, accepted]
    service = SearchService(radarr, AsyncMock())

    results = await service.search_releases_for_title(
        ContentType.MOVIE, arr_id=15,
    )

    # Seeders and size favour the rejected one; the verdict must win anyway.
    assert [r.guid for r in results] == ["g2", "g1"]


# ---------------------------------------------------------------------------
# describe_search_failure — rollback 2026-08-10 rename (was
# describe_scryer_failure), *arr-appropriate wording.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_search_command_reports_an_indexer_failure_clearly():
    """"Поиск временно недоступен" hides the only actionable fact."""
    from bot.clients.base import ServiceConnectionError
    from bot.services.search_service import describe_search_failure

    message = describe_search_failure(ServiceConnectionError("Таймаут Prowlarr"))
    assert "индексер" in message.lower()
    assert "scryer" not in message.lower()


# ---------------------------------------------------------------------------
# process_search auto-detection — regression guard (found during self-review,
# not covered by any prior test): commands.py called the nonexistent
# `search_service.detect_with_confidence`, a Scryer-era name the rewritten
# *arr SearchService never carried (only `detect_content_type` — see
# tests/test_detect_content_type.py, which never calls it anything else).
# `handle_text_search` routes every plain-text message through
# `process_search(..., ContentType.UNKNOWN, ...)`, so this was an
# AttributeError on the bot's single most common entry point.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_process_search_auto_detection_uses_detect_content_type():
    from bot.handlers import search
    from bot.services.search_service import DetectionResult

    movie = MovieInfo(tmdb_id=1, title="Dune", year=2021, radarr_id=42)
    detection = DetectionResult(
        content_type=ContentType.MOVIE,
        confidence=0.95,
        reason="movie_match",
        candidates={},
        lookup_results=[movie],
    )

    search_service = MagicMock()
    search_service.parse_query = MagicMock(return_value={
        "title": "Dune", "year": None, "season": None, "episode": None, "quality": None,
    })
    search_service.detect_content_type = AsyncMock(return_value=detection)
    search_service.search_releases_for_title = AsyncMock(return_value=[])
    add_service = MagicMock()

    status_msg = MagicMock()
    status_msg.edit_text = AsyncMock()
    status_msg.delete = AsyncMock()
    message = MagicMock()
    message.answer = AsyncMock(return_value=status_msg)

    db = _make_db(session=None)
    db_user = _make_db_user()

    with patch.object(search, "get_services", AsyncMock(return_value=(search_service, add_service))):
        await search.process_search(message, "Dune", ContentType.UNKNOWN, db_user, db)

    search_service.detect_content_type.assert_awaited_once_with("Dune")
    # The resolved movie already carries a radarr_id — no add call, straight
    # to listing releases for it.
    search_service.search_releases_for_title.assert_awaited_once_with(
        ContentType.MOVIE, 42, season=None,
    )
