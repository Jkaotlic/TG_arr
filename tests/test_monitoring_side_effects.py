"""Audit 2026-09-23 B4: grabbing/adding must not rewrite monitoring the user
already set (GRAB-05, GRAB-07, UI-05, UI-06, GRAB-19).

- The season preset is applied only to a title this very search added to
  Sonarr (``SearchSession.title_created``). A title that was already in the
  library keeps its monitoring.
- A season pack for season N monitors season N only — never "all".
- A force (qBit) grab never touches *arr monitoring.
- Trending "➕" is honest about titles already in the library.
- /title "Включить мониторинг" for a series also monitors future seasons.
- ``monitored`` is parsed for movies/series and the card says which it is.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.models import (
    ActionLog,
    ActionType,
    ContentType,
    MovieInfo,
    SearchResult,
    SearchSession,
    SeriesInfo,
    User,
)


def _release(*, season=None, pack=False) -> SearchResult:
    return SearchResult(
        guid="guid-1", origin="arr", indexer="idx", indexer_id=1,
        title="Show S03E05 1080p", size=1,
        detected_season=season, is_season_pack=pack,
    )


def _session(*, created: bool, release=None, monitor_type=None) -> SearchSession:
    return SearchSession(
        user_id=5, query="show", content_type=ContentType.SERIES,
        selected_result=release or _release(season=3),
        selected_content=SeriesInfo(tvdb_id=1, title="Show", sonarr_id=77),
        monitor_type=monitor_type,
        title_created=created,
    )


def _add_service(content_type=ContentType.SERIES) -> MagicMock:
    add_service = MagicMock()
    add_service.grab_release = AsyncMock(return_value=(
        True, ActionLog(user_id=0, action_type=ActionType.GRAB, content_type=content_type),
    ))
    add_service.sonarr = MagicMock()
    add_service.sonarr.set_series_monitored = AsyncMock(return_value=True)
    add_service.sonarr.set_season_monitoring = AsyncMock(return_value=True)
    add_service.sonarr.monitor_season = AsyncMock(return_value=True)
    add_service.radarr = MagicMock()
    add_service.radarr.set_movie_monitored = AsyncMock(return_value=True)
    return add_service


def _msg() -> MagicMock:
    m = MagicMock()
    m.edit_text = AsyncMock()
    return m


def _db() -> MagicMock:
    db = MagicMock()
    db.log_action = AsyncMock()
    db.delete_session = AsyncMock()
    return db


async def _grab(session, add_service, **kw):
    from bot.handlers.search import grab as grab_mod

    await grab_mod._execute_grab(
        _msg(), session, User(tg_id=5), _db(), MagicMock(), add_service, **kw,
    )


# ------------------------------------------------------------------ GRAB-05
@pytest.mark.asyncio
async def test_episode_grab_on_an_existing_series_leaves_monitoring_alone():
    add_service = _add_service()
    await _grab(_session(created=False), add_service)

    add_service.sonarr.set_series_monitored.assert_not_awaited()
    add_service.sonarr.set_season_monitoring.assert_not_awaited()
    add_service.sonarr.monitor_season.assert_not_awaited()


@pytest.mark.asyncio
async def test_preset_is_not_applied_to_a_series_that_was_already_in_the_library():
    add_service = _add_service()
    await _grab(_session(created=False, monitor_type="all"), add_service)

    add_service.sonarr.set_season_monitoring.assert_not_awaited()
    add_service.sonarr.set_series_monitored.assert_not_awaited()


@pytest.mark.asyncio
async def test_season_pack_on_an_existing_series_monitors_that_season_only():
    add_service = _add_service()
    await _grab(_session(created=False, release=_release(season=3, pack=True)), add_service)

    add_service.sonarr.monitor_season.assert_awaited_once_with(77, 3, monitor_series=False)
    add_service.sonarr.set_season_monitoring.assert_not_awaited()
    add_service.sonarr.set_series_monitored.assert_not_awaited()


@pytest.mark.asyncio
async def test_season_pack_on_a_new_series_monitors_that_season_not_all():
    add_service = _add_service()
    await _grab(_session(created=True, release=_release(season=3, pack=True)), add_service)

    add_service.sonarr.monitor_season.assert_awaited_once_with(77, 3, monitor_series=True)
    add_service.sonarr.set_season_monitoring.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_series_episode_grab_still_gets_the_decided_preset():
    add_service = _add_service()
    await _grab(_session(created=True), add_service)

    add_service.sonarr.set_series_monitored.assert_awaited_once_with(77, True)
    add_service.sonarr.set_season_monitoring.assert_awaited_once_with(77, "future")


@pytest.mark.asyncio
async def test_new_series_user_preset_wins():
    add_service = _add_service()
    await _grab(_session(created=True, monitor_type="firstSeason"), add_service)

    add_service.sonarr.set_season_monitoring.assert_awaited_once_with(77, "firstSeason")


# ------------------------------------------------------------------ GRAB-07
@pytest.mark.asyncio
async def test_force_grab_never_changes_series_monitoring():
    add_service = _add_service()
    await _grab(_session(created=True), add_service, force_download=True)

    add_service.sonarr.set_series_monitored.assert_not_awaited()
    add_service.sonarr.set_season_monitoring.assert_not_awaited()
    add_service.sonarr.monitor_season.assert_not_awaited()


@pytest.mark.asyncio
async def test_force_grab_never_changes_movie_monitoring():
    add_service = _add_service(ContentType.MOVIE)
    session = SearchSession(
        user_id=5, query="film", content_type=ContentType.MOVIE,
        selected_result=_release(),
        selected_content=MovieInfo(tmdb_id=1, title="Film", year=2021, radarr_id=99),
        title_created=True,
    )
    await _grab(session, add_service, force_download=True)

    add_service.radarr.set_movie_monitored.assert_not_awaited()


# ------------------------------------------- title_created survives the flow
def _search_mocks(results):
    search_service = MagicMock()
    search_service.parse_query = MagicMock(return_value={
        "title": "Show", "year": None, "season": None, "episode": None, "quality": None,
    })
    search_service.search_releases_for_title = AsyncMock(return_value=results)
    status_msg = MagicMock()
    status_msg.edit_text = AsyncMock()
    message = MagicMock()
    message.answer = AsyncMock(return_value=status_msg)
    db = MagicMock()
    db.save_session = AsyncMock()
    db.log_action = AsyncMock()
    return search_service, message, db


@pytest.mark.asyncio
async def test_process_search_remembers_that_it_created_the_title():
    from bot.handlers import search
    from bot.handlers.search import commands

    new = SeriesInfo(tvdb_id=1, title="Show", sonarr_id=77)
    search_service, message, db = _search_mocks([_release(season=1)])
    with patch.object(search, "get_services", AsyncMock(return_value=(search_service, MagicMock()))), \
            patch.object(commands, "_resolve_arr_entry", AsyncMock(return_value=(new, 77, True))), \
            patch.object(search, "_render_results_page", AsyncMock()):
        await search.process_search(
            message, "Show", ContentType.SERIES, User(tg_id=5), db, chosen_title=new,
        )

    saved = db.save_session.await_args.args[1]
    assert saved.title_created is True


@pytest.mark.asyncio
async def test_created_flag_survives_the_season_picker():
    """The picker re-enters process_search with the title (now carrying a
    sonarr_id, so _resolve_arr_entry says created=False) — the flag must
    come from the session the picker saved."""
    from bot.handlers import search
    from bot.handlers.search import commands

    show = SeriesInfo(
        tvdb_id=1, title="Show", sonarr_id=77,
        seasons=[{"seasonNumber": n} for n in range(0, 4)],
    )
    search_service, message, db = _search_mocks([])
    with patch.object(search, "get_services", AsyncMock(return_value=(search_service, MagicMock()))), \
            patch.object(commands, "_resolve_arr_entry", AsyncMock(return_value=(show, 77, True))):
        await search.process_search(
            message, "Show", ContentType.SERIES, User(tg_id=5), db, chosen_title=show,
        )
    picker_session = db.save_session.await_args.args[1]
    assert picker_session.title_created is True

    # Second leg: the season-scope handler passes the flag back in.
    search_service, message, db = _search_mocks([_release(season=2)])
    with patch.object(search, "get_services", AsyncMock(return_value=(search_service, MagicMock()))), \
            patch.object(commands, "_resolve_arr_entry", AsyncMock(return_value=(show, 77, False))), \
            patch.object(search, "_render_results_page", AsyncMock()):
        await search.process_search(
            message, "Show", ContentType.SERIES, User(tg_id=5), db,
            chosen_title=show, season_override=2, title_created=True,
        )
    assert db.save_session.await_args.args[1].title_created is True


@pytest.mark.asyncio
async def test_season_scope_handler_passes_the_created_flag():
    from bot.handlers import search as search_mod
    from bot.handlers.search import results as results_mod
    from bot.ui.callbacks import SeasonScopeCB

    cb = MagicMock()
    cb.from_user = MagicMock(id=5)
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.edit_reply_markup = AsyncMock()
    session = SearchSession(
        user_id=5, query="Show", content_type=ContentType.SERIES,
        selected_content=SeriesInfo(tvdb_id=1, title="Show", sonarr_id=77),
        title_created=True,
    )
    db = MagicMock()
    db.get_session = AsyncMock(return_value=session)
    with patch.object(results_mod, "accessible_message", return_value=cb.message), \
            patch.object(search_mod, "process_search", AsyncMock()) as spied:
        await results_mod.handle_season_scope(
            cb, SeasonScopeCB(season=2, title_id="77"), User(tg_id=5), db,
        )
    assert spied.await_args.kwargs["title_created"] is True


def test_season_menu_is_hidden_for_a_series_already_in_the_library():
    from bot.ui.keyboards import Keyboards

    def texts(kb):
        return [b.text for row in kb.inline_keyboard for b in row]

    shown = Keyboards.release_details(_release(), ContentType.SERIES, season_menu=True)
    hidden = Keyboards.release_details(_release(), ContentType.SERIES, season_menu=False)
    assert "📺 Мониторинг сезонов" in texts(shown)
    assert "📺 Мониторинг сезонов" not in texts(hidden)


# ------------------------------------------------ SonarrClient.monitor_season
@pytest.mark.asyncio
async def test_monitor_season_puts_the_series_with_one_season_flipped():
    from bot.clients.sonarr import SonarrClient

    client = SonarrClient("http://sonarr.example.com", "key")
    current = {
        "id": 77, "monitored": False, "qualityProfileId": 7,
        "seasons": [
            {"seasonNumber": 1, "monitored": False},
            {"seasonNumber": 2, "monitored": True},
            {"seasonNumber": 3, "monitored": False},
        ],
    }
    client.get = AsyncMock(return_value=current)
    client._safe_request = AsyncMock(return_value={})

    assert await client.monitor_season(77, 3, monitor_series=False) is True

    method, path = client._safe_request.await_args.args[:2]
    body = client._safe_request.await_args.kwargs["json_data"]
    assert (method, path) == ("PUT", "/api/v3/series/77")
    assert body["monitored"] is False
    assert body["qualityProfileId"] == 7
    assert [s["monitored"] for s in body["seasons"]] == [False, True, True]


@pytest.mark.asyncio
async def test_monitor_season_can_lift_the_series_flag_and_rejects_unknown_season():
    from bot.clients.sonarr import SonarrClient

    client = SonarrClient("http://sonarr.example.com", "key")
    client.get = AsyncMock(return_value={
        "id": 77, "monitored": False, "seasons": [{"seasonNumber": 1, "monitored": False}],
    })
    client._safe_request = AsyncMock(return_value={})

    assert await client.monitor_season(77, 1, monitor_series=True) is True
    assert client._safe_request.await_args.kwargs["json_data"]["monitored"] is True

    client._safe_request.reset_mock()
    assert await client.monitor_season(77, 9, monitor_series=True) is False
    client._safe_request.assert_not_awaited()


# ------------------------------------------------------------ UI-05 trending
def _trending_cb():
    cb = MagicMock()
    cb.answer = AsyncMock()
    cb.from_user = MagicMock(id=1)
    cb.message = MagicMock()
    cb.message.edit_reply_markup = AsyncMock()
    status = MagicMock()
    status.edit_text = AsyncMock()
    cb.message.answer = AsyncMock(return_value=status)
    return cb, status


def _trending_user():
    db_user = MagicMock()
    db_user.tg_id = 1
    db_user.preferences = MagicMock(
        radarr_quality_profile_id=None, radarr_root_folder_id=None,
        sonarr_quality_profile_id=None, sonarr_root_folder_id=None,
    )
    return db_user


def _movie_add_service(existing):
    from bot.models import QualityProfile, RootFolder

    add_service = MagicMock()
    add_service.radarr = MagicMock()
    add_service.radarr.get_movie_by_tmdb = AsyncMock(return_value=existing)
    add_service.radarr.set_movie_monitored = AsyncMock(return_value=True)
    add_service.radarr.search_movie = AsyncMock(return_value={})
    add_service.get_radarr_profiles = AsyncMock(return_value=[QualityProfile(id=1, name="Any")])
    add_service.get_radarr_root_folders = AsyncMock(return_value=[RootFolder(id=1, path="/movies")])
    add_service.add_movie = AsyncMock(return_value=(
        MovieInfo(tmdb_id=603, title="The Matrix", year=1999, radarr_id=42),
        ActionLog(user_id=0, action_type=ActionType.ADD, content_type=ContentType.MOVIE),
    ))
    return add_service


async def _press_movie_add(add_service):
    from bot.handlers import trending
    from bot.ui.callbacks import AddContentCB

    trending._cache_put(trending._trending_movies_cache, 603, MovieInfo(tmdb_id=603, title="The Matrix", year=1999))
    cb, status = _trending_cb()
    db = MagicMock()
    db.log_action = AsyncMock()
    with patch.object(trending, "get_radarr", AsyncMock()), \
            patch.object(trending, "get_sonarr", AsyncMock()), \
            patch.object(trending, "get_qbittorrent", AsyncMock()), \
            patch.object(trending, "AddService", return_value=add_service):
        await trending.handle_add_movie_from_trending(
            cb, AddContentCB(kind="movie", tmdb_id=603), _trending_user(), db,
        )
    trending._trending_movies_cache.pop(603, None)
    trending._trending_movies_inserted_at.pop(603, None)
    return cb, status


@pytest.mark.asyncio
async def test_trending_movie_already_monitored_does_nothing():
    add_service = _movie_add_service(
        MovieInfo(tmdb_id=603, title="The Matrix", year=1999, radarr_id=42, monitored=True),
    )
    cb, status = await _press_movie_add(add_service)

    add_service.add_movie.assert_not_awaited()
    add_service.radarr.set_movie_monitored.assert_not_awaited()
    add_service.radarr.search_movie.assert_not_awaited()
    assert "Уже в библиотеке и отслеживается" in status.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_trending_movie_in_catalogue_unmonitored_gets_monitored_and_searched():
    add_service = _movie_add_service(
        MovieInfo(tmdb_id=603, title="The Matrix", year=1999, radarr_id=42, monitored=False),
    )
    cb, status = await _press_movie_add(add_service)

    add_service.add_movie.assert_not_awaited()
    add_service.radarr.set_movie_monitored.assert_awaited_once_with(42, True)
    add_service.radarr.search_movie.assert_awaited_once_with(42)
    assert "Включил отслеживание и поиск" in status.edit_text.await_args.args[0]
    cb.message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)


@pytest.mark.asyncio
async def test_trending_new_movie_is_added_and_the_keyboard_removed():
    add_service = _movie_add_service(None)
    cb, status = await _press_movie_add(add_service)

    add_service.add_movie.assert_awaited_once()
    assert "добавлен в Radarr" in status.edit_text.await_args.args[0]
    cb.message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)


@pytest.mark.asyncio
async def test_trending_series_already_in_library_is_left_alone():
    from bot.handlers import trending
    from bot.models import QualityProfile, RootFolder
    from bot.ui.callbacks import AddContentCB

    trending._cache_put(trending._trending_series_cache, 55, SeriesInfo(tmdb_id=55, title="Show", year=2024))
    resolved = SeriesInfo(tmdb_id=55, tvdb_id=123, title="Show", year=2024, sonarr_id=7)
    add_service = MagicMock()
    add_service.radarr = MagicMock()
    add_service.sonarr = MagicMock()
    add_service.sonarr.get_series_by_tvdb = AsyncMock(return_value=resolved)
    add_service.sonarr.set_series_monitored = AsyncMock()
    add_service.sonarr.set_season_monitoring = AsyncMock()
    add_service.get_sonarr_profiles = AsyncMock(return_value=[QualityProfile(id=1, name="Any")])
    add_service.get_sonarr_root_folders = AsyncMock(return_value=[RootFolder(id=1, path="/tv")])
    add_service.add_series = AsyncMock()
    search_service = MagicMock()
    search_service.lookup_series = AsyncMock(return_value=[resolved])
    cb, status = _trending_cb()
    db = MagicMock()
    db.log_action = AsyncMock()
    with patch.object(trending, "get_radarr", AsyncMock()), \
            patch.object(trending, "get_sonarr", AsyncMock()), \
            patch.object(trending, "get_qbittorrent", AsyncMock()), \
            patch.object(trending, "AddService", return_value=add_service), \
            patch.object(trending, "SearchService", return_value=search_service):
        await trending.handle_add_series_from_trending(
            cb, AddContentCB(kind="series", tmdb_id=55), _trending_user(), db,
        )
    trending._trending_series_cache.pop(55, None)
    trending._trending_series_inserted_at.pop(55, None)

    add_service.add_series.assert_not_awaited()
    add_service.sonarr.set_series_monitored.assert_not_awaited()
    add_service.sonarr.set_season_monitoring.assert_not_awaited()
    assert "Уже в библиотеке — управлять через /title" in status.edit_text.await_args.args[0]


def test_trending_series_button_says_all_seasons():
    from bot.ui.keyboards import Keyboards

    kb = Keyboards.series_details(SeriesInfo(tmdb_id=55, title="Show"))
    texts = [b.text for row in kb.inline_keyboard for b in row]
    assert "➕ В библиотеку (все сезоны)" in texts


# ------------------------------------------------------------ UI-06 /title
@pytest.mark.asyncio
async def test_title_monitor_on_for_a_series_also_monitors_future_seasons():
    from bot.handlers import titles
    from bot.ui.callbacks import TitleActionCB

    sonarr = AsyncMock()
    sonarr.set_series_monitored = AsyncMock(return_value=True)
    sonarr.set_season_monitoring = AsyncMock(return_value=True)
    cb = MagicMock()
    cb.from_user = MagicMock(id=42)
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    titles._title_candidates[42] = {"series:3": SeriesInfo(title="Paw", sonarr_id=3, monitored=False)}
    with patch.object(titles, "get_radarr", AsyncMock()), \
            patch.object(titles, "get_sonarr", AsyncMock(return_value=sonarr)):
        await titles.handle_title_action(
            cb, TitleActionCB(action="mon", kind="series", title_id="3"), MagicMock(tg_id=42), AsyncMock(),
        )
    titles._title_candidates.pop(42, None)

    sonarr.set_series_monitored.assert_awaited_once_with(3, True)
    sonarr.set_season_monitoring.assert_awaited_once_with(3, "future")


@pytest.mark.asyncio
async def test_title_unmonitor_does_not_touch_seasons():
    from bot.handlers.titles import _apply_monitor_toggle

    sonarr = AsyncMock()
    sonarr.set_series_monitored = AsyncMock(return_value=True)
    await _apply_monitor_toggle(sonarr, 3, False, is_movie=False)
    sonarr.set_season_monitoring.assert_not_awaited()


# ------------------------------------------------------------ GRAB-19 parsing
def test_radarr_parses_monitored():
    from bot.clients.radarr import RadarrClient

    client = RadarrClient("http://radarr.example.com", "key")
    movie = client._parse_movie({"tmdbId": 1, "title": "Film", "id": 3, "monitored": True})
    assert movie is not None and movie.monitored is True
    movie = client._parse_movie({"tmdbId": 1, "title": "Film", "id": 3})
    assert movie is not None and movie.monitored is False


def test_sonarr_parses_monitored():
    from bot.clients.sonarr import SonarrClient

    client = SonarrClient("http://sonarr.example.com", "key")
    series = client._parse_series({"tvdbId": 1, "title": "Show", "id": 3, "monitored": True})
    assert series is not None and series.monitored is True


def test_movie_card_distinguishes_catalogue_from_monitored():
    from bot.ui.formatters import Formatters

    idle = Formatters.format_movie_info(MovieInfo(tmdb_id=1, title="F", year=2020, radarr_id=3, monitored=False))
    watched = Formatters.format_movie_info(MovieInfo(tmdb_id=1, title="F", year=2020, radarr_id=3, monitored=True))
    assert "📇 В каталоге, не отслеживается" in idle
    assert "👁 Отслеживается" in watched
