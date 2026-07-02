"""TEST-03: user-facing grab flow in bot/handlers/search.py.

Covers the previously-untested paths flagged in 08-testing-quality.md:
- handle_force_grab: force_download=True reaches _execute_grab; "qBittorrent
  не настроен" branch.
- handle_confirm_grab: dispatch to the music flow for ArtistInfo, movie flow
  for MovieInfo (BUG-27 regression guard — the music handler must not
  swallow non-music confirmations).
- _execute_grab: movie-path (only the series path had coverage before).

Also covers BUG-07 (search.py:handle_release_selection) — callback.answer()
must fire right after session validation, before the (potentially slow)
Radarr/Sonarr lookup, not after.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.models import (
    ArtistInfo,
    ContentType,
    MovieInfo,
    QualityProfile,
    RootFolder,
    SearchResult,
    SearchSession,
    SeriesInfo,
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
# _execute_grab — movie path (only the series path had coverage before)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_execute_grab_movie_path_success():
    from bot.handlers import search

    result = SearchResult(guid="g1", title="Test.Movie.Release", calculated_score=80)
    movie = MovieInfo(title="Interstellar", tmdb_id=157336, year=2014)
    session = SearchSession(
        user_id=42, query="interstellar", content_type=ContentType.MOVIE, results=[result],
        selected_result=result,
    )
    session.selected_content = movie
    db = _make_db(session)
    db_user = _make_db_user()
    message = MagicMock()
    message.edit_text = AsyncMock()

    search_service = MagicMock()
    search_service.parse_query = MagicMock(return_value={"title": "interstellar", "year": None})

    add_service = MagicMock()
    add_service.get_radarr_profiles = AsyncMock(return_value=[QualityProfile(id=1, name="HD")])
    add_service.get_radarr_root_folders = AsyncMock(return_value=[RootFolder(id=1, path="/movies")])
    action = MagicMock(success=True, error_message=None)
    add_service.grab_movie_release = AsyncMock(return_value=(True, action, "Отправлено в Radarr"))

    await search._execute_grab(message, session, db_user, db, search_service, add_service)

    add_service.grab_movie_release.assert_awaited_once()
    call_kwargs = add_service.grab_movie_release.await_args.kwargs
    assert call_kwargs["movie"] is movie
    assert call_kwargs["force_download"] is False
    sent = message.edit_text.await_args_list[-1].args[0]
    assert "Interstellar" in sent
    db.delete_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_grab_movie_path_looks_up_when_no_selected_content():
    """When selected_content isn't a MovieInfo yet (grab_best path), it must
    be looked up via search_service.lookup_movie before grabbing."""
    from bot.handlers import search

    result = SearchResult(guid="g1", title="Test.Movie.Release", calculated_score=80, detected_year=2014)
    session = SearchSession(
        user_id=42, query="interstellar", content_type=ContentType.MOVIE, results=[result],
        selected_result=result,
    )
    # selected_content left unset (None) — forces the lookup branch.
    db = _make_db(session)
    db_user = _make_db_user()
    message = MagicMock()
    message.edit_text = AsyncMock()

    movie = MovieInfo(title="Interstellar", tmdb_id=157336, year=2014)
    search_service = MagicMock()
    search_service.parse_query = MagicMock(return_value={"title": "interstellar", "year": None})
    search_service.lookup_movie = AsyncMock(return_value=[movie])

    add_service = MagicMock()
    add_service.get_radarr_profiles = AsyncMock(return_value=[QualityProfile(id=1, name="HD")])
    add_service.get_radarr_root_folders = AsyncMock(return_value=[RootFolder(id=1, path="/movies")])
    action = MagicMock(success=True, error_message=None)
    add_service.grab_movie_release = AsyncMock(return_value=(True, action, "OK"))

    await search._execute_grab(message, session, db_user, db, search_service, add_service)

    search_service.lookup_movie.assert_awaited_once()
    add_service.grab_movie_release.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_grab_movie_path_force_download_propagates():
    from bot.handlers import search

    result = SearchResult(guid="g1", title="Test.Movie.Release", calculated_score=80)
    movie = MovieInfo(title="Interstellar", tmdb_id=157336, year=2014)
    session = SearchSession(
        user_id=42, query="interstellar", content_type=ContentType.MOVIE, results=[result],
        selected_result=result,
    )
    session.selected_content = movie
    db = _make_db(session)
    db_user = _make_db_user()
    message = MagicMock()
    message.edit_text = AsyncMock()

    search_service = MagicMock()
    search_service.parse_query = MagicMock(return_value={"title": "interstellar", "year": None})

    add_service = MagicMock()
    add_service.get_radarr_profiles = AsyncMock(return_value=[QualityProfile(id=1, name="HD")])
    add_service.get_radarr_root_folders = AsyncMock(return_value=[RootFolder(id=1, path="/movies")])
    action = MagicMock(success=True, error_message=None)
    add_service.grab_movie_release = AsyncMock(return_value=(True, action, "OK"))

    await search._execute_grab(
        message, session, db_user, db, search_service, add_service, force_download=True
    )

    call_kwargs = add_service.grab_movie_release.await_args.kwargs
    assert call_kwargs["force_download"] is True


@pytest.mark.asyncio
async def test_execute_grab_movie_path_no_profiles_shows_error():
    from bot.handlers import search

    result = SearchResult(guid="g1", title="Test.Movie.Release", calculated_score=80)
    movie = MovieInfo(title="Interstellar", tmdb_id=157336, year=2014)
    session = SearchSession(
        user_id=42, query="interstellar", content_type=ContentType.MOVIE, results=[result],
        selected_result=result,
    )
    session.selected_content = movie
    db = _make_db(session)
    db_user = _make_db_user()
    message = MagicMock()
    message.edit_text = AsyncMock()

    search_service = MagicMock()
    search_service.parse_query = MagicMock(return_value={"title": "interstellar", "year": None})

    add_service = MagicMock()
    add_service.get_radarr_profiles = AsyncMock(return_value=[])
    add_service.get_radarr_root_folders = AsyncMock(return_value=[])

    await search._execute_grab(message, session, db_user, db, search_service, add_service)

    sent = message.edit_text.await_args_list[-1].args[0]
    assert "профилей" in sent.lower() or "папок" in sent.lower()


# ---------------------------------------------------------------------------
# BUG-07: handle_release_selection must ack right after validation, before
# the (potentially slow) Radarr/Sonarr lookup.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_release_selection_acks_before_lookup():
    """callback.answer() must be called before search_service.lookup_movie —
    not after. We assert ordering via a shared call-order list."""
    from bot.handlers import search

    result = SearchResult(guid="g1", title="Test.Release", calculated_score=50)
    session = SearchSession(
        user_id=42, query="test movie", content_type=ContentType.MOVIE, results=[result],
    )
    db = _make_db(session)
    db_user = _make_db_user()
    callback = _make_callback(data=search.CallbackData.RELEASE + "0")

    call_order = []

    async def _answer_tracker(*_a, **_k):
        call_order.append("answer")

    async def _lookup_tracker(*_a, **_k):
        call_order.append("lookup_movie")
        return [MovieInfo(title="Test", tmdb_id=1, year=2024)]

    callback.answer = AsyncMock(side_effect=_answer_tracker)

    search_service = MagicMock()
    search_service.parse_query = MagicMock(return_value={"title": "test movie", "year": None})
    search_service.lookup_movie = AsyncMock(side_effect=_lookup_tracker)

    add_service = MagicMock()
    add_service.qbittorrent = None
    services = (search_service, add_service)

    with patch.object(search, "get_services", AsyncMock(return_value=services)), \
         patch.object(search, "get_emby", AsyncMock(return_value=None)):
        await search.handle_release_selection(callback, db_user, db)

    assert call_order == ["answer", "lookup_movie"], call_order


# ---------------------------------------------------------------------------
# LOGIC-06: detection's lookup_results are cached on the session so a grab
# doesn't repeat the same Radarr/Sonarr lookup 2-3 times.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_release_selection_reuses_cached_lookup_candidates_no_network_call():
    """When session.lookup_candidates already has the matching MovieInfo
    (from detect_with_confidence), handle_release_selection must NOT call
    search_service.lookup_movie again."""
    from bot.handlers import search

    result = SearchResult(guid="g1", title="Test.Release", calculated_score=50, detected_year=2014)
    cached_movie = MovieInfo(title="Interstellar", tmdb_id=157336, year=2014)
    session = SearchSession(
        user_id=42, query="interstellar", content_type=ContentType.MOVIE, results=[result],
        lookup_candidates=[cached_movie],
    )
    db = _make_db(session)
    db_user = _make_db_user()
    callback = _make_callback(data=search.CallbackData.RELEASE + "0")

    search_service = MagicMock()
    search_service.parse_query = MagicMock(return_value={"title": "interstellar", "year": None})
    search_service.lookup_movie = AsyncMock(side_effect=AssertionError("must not be called"))

    add_service = MagicMock()
    add_service.qbittorrent = None
    services = (search_service, add_service)

    with patch.object(search, "get_services", AsyncMock(return_value=services)), \
         patch.object(search, "get_emby", AsyncMock(return_value=None)):
        await search.handle_release_selection(callback, db_user, db)

    search_service.lookup_movie.assert_not_awaited()
    sent = callback.message.edit_text.await_args_list[-1].args[0]
    assert "Interstellar" in sent


@pytest.mark.asyncio
async def test_release_selection_falls_back_to_lookup_when_no_cached_candidates():
    """No lookup_candidates on the session (e.g. detection never ran, or
    returned no movie matches) — handle_release_selection must fall back to
    the network lookup exactly as before."""
    from bot.handlers import search

    result = SearchResult(guid="g1", title="Test.Release", calculated_score=50, detected_year=2014)
    session = SearchSession(
        user_id=42, query="interstellar", content_type=ContentType.MOVIE, results=[result],
        lookup_candidates=None,
    )
    db = _make_db(session)
    db_user = _make_db_user()
    callback = _make_callback(data=search.CallbackData.RELEASE + "0")

    fetched_movie = MovieInfo(title="Interstellar", tmdb_id=157336, year=2014)
    search_service = MagicMock()
    search_service.parse_query = MagicMock(return_value={"title": "interstellar", "year": None})
    search_service.lookup_movie = AsyncMock(return_value=[fetched_movie])

    add_service = MagicMock()
    add_service.qbittorrent = None
    services = (search_service, add_service)

    with patch.object(search, "get_services", AsyncMock(return_value=services)), \
         patch.object(search, "get_emby", AsyncMock(return_value=None)):
        await search.handle_release_selection(callback, db_user, db)

    search_service.lookup_movie.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_grab_best_path_reuses_cached_lookup_candidates_no_network_call():
    """grab_best skips handle_release_selection entirely (selected_content is
    never set) — _execute_grab must still reuse session.lookup_candidates
    instead of calling search_service.lookup_movie."""
    from bot.handlers import search

    result = SearchResult(guid="g1", title="Test.Movie.Release", calculated_score=80, detected_year=2014)
    cached_movie = MovieInfo(title="Interstellar", tmdb_id=157336, year=2014)
    session = SearchSession(
        user_id=42, query="interstellar", content_type=ContentType.MOVIE, results=[result],
        selected_result=result,
        lookup_candidates=[cached_movie],
    )
    db = _make_db(session)
    db_user = _make_db_user()
    message = MagicMock()
    message.edit_text = AsyncMock()

    search_service = MagicMock()
    search_service.parse_query = MagicMock(return_value={"title": "interstellar", "year": None})
    search_service.lookup_movie = AsyncMock(side_effect=AssertionError("must not be called"))

    add_service = MagicMock()
    add_service.get_radarr_profiles = AsyncMock(return_value=[QualityProfile(id=1, name="HD")])
    add_service.get_radarr_root_folders = AsyncMock(return_value=[RootFolder(id=1, path="/movies")])
    action = MagicMock(success=True, error_message=None)
    add_service.grab_movie_release = AsyncMock(return_value=(True, action, "OK"))

    await search._execute_grab(message, session, db_user, db, search_service, add_service)

    search_service.lookup_movie.assert_not_awaited()
    add_service.grab_movie_release.assert_awaited_once()
    call_kwargs = add_service.grab_movie_release.await_args.kwargs
    assert call_kwargs["movie"] is cached_movie


@pytest.mark.asyncio
async def test_execute_grab_best_path_series_reuses_cached_lookup_candidates():
    """Series counterpart: _execute_grab's series branch reuses
    session.lookup_candidates instead of calling search_service.lookup_series."""
    from bot.handlers import search

    result = SearchResult(
        guid="g1", title="Test.Series.Release", calculated_score=80,
        detected_year=2016, is_season_pack=True,
    )
    cached_series = SeriesInfo(title="Stranger Things", tvdb_id=305288, year=2016)
    session = SearchSession(
        user_id=42, query="stranger things", content_type=ContentType.SERIES, results=[result],
        selected_result=result,
        lookup_candidates=[cached_series],
    )
    db = _make_db(session)
    db_user = _make_db_user()
    message = MagicMock()
    message.edit_text = AsyncMock()

    search_service = MagicMock()
    search_service.parse_query = MagicMock(return_value={"title": "stranger things", "year": None})
    search_service.lookup_series = AsyncMock(side_effect=AssertionError("must not be called"))

    add_service = MagicMock()
    add_service.get_sonarr_profiles = AsyncMock(return_value=[QualityProfile(id=1, name="HD")])
    add_service.get_sonarr_root_folders = AsyncMock(return_value=[RootFolder(id=1, path="/tv")])
    action = MagicMock(success=True, error_message=None)
    add_service.grab_series_release = AsyncMock(return_value=(True, action, "OK"))

    await search._execute_grab(message, session, db_user, db, search_service, add_service)

    search_service.lookup_series.assert_not_awaited()
    add_service.grab_series_release.assert_awaited_once()
    call_kwargs = add_service.grab_series_release.await_args.kwargs
    assert call_kwargs["series"] is cached_series
