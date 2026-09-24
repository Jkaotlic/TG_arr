"""Audit 2026-09-23 B5: series search correctness (GRAB-04, GRAB-13) and the
default quality profile (GRAB-06).

- A single-season series is searched with seasonNumber=1: Sonarr's
  ``GET /release?seriesId`` without a season is an RSS-style query, not a
  search of the show.
- "📺 Весь сериал" lists nothing; it sends Sonarr's SeriesSearch command.
- "S01E05" in the query narrows the list to that episode plus season packs,
  which are flagged "пак сезона".
- Profile fallback: user pref → RADARR/SONARR_DEFAULT_PROFILE_ID → a profile
  named 4K/2160 → profiles[0], with a warning in /settings for the last.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.models import (
    ContentType,
    QualityProfile,
    RootFolder,
    SearchResult,
    SeriesInfo,
    User,
    UserPreferences,
)


def _rel(guid, *, season=1, episode=None, pack=False) -> SearchResult:
    return SearchResult(
        guid=guid, origin="arr", title=f"Show {guid}", size=1,
        detected_season=season, detected_episode=episode, is_season_pack=pack,
    )


def _mocks(parsed=None, results=None):
    search_service = MagicMock()
    search_service.parse_query = MagicMock(return_value=parsed or {
        "title": "Show", "year": None, "season": None, "episode": None, "quality": None,
    })
    search_service.search_releases_for_title = AsyncMock(return_value=results or [])
    add_service = MagicMock()
    add_service.sonarr = MagicMock()
    add_service.sonarr.search_series = AsyncMock(return_value={"id": 1})
    add_service.sonarr.set_series_monitored = AsyncMock(return_value=True)
    add_service.sonarr.set_season_monitoring = AsyncMock(return_value=True)
    status_msg = MagicMock()
    status_msg.edit_text = AsyncMock()
    message = MagicMock()
    message.answer = AsyncMock(return_value=status_msg)
    db = MagicMock()
    db.save_session = AsyncMock()
    db.delete_session = AsyncMock()
    db.log_action = AsyncMock()
    return search_service, add_service, message, status_msg, db


def _show(n_seasons: int) -> SeriesInfo:
    return SeriesInfo(
        tvdb_id=1, title="Show", sonarr_id=77,
        seasons=[{"seasonNumber": n} for n in range(0, n_seasons + 1)],
    )


def _texts(status_msg) -> str:
    return " ".join(str(c.args) for c in status_msg.edit_text.await_args_list)


# ------------------------------------------------------------------ GRAB-04
@pytest.mark.asyncio
async def test_single_season_series_is_searched_with_season_1():
    from bot.handlers import search

    search_service, add_service, message, _status, db = _mocks()
    with patch.object(search, "get_services", AsyncMock(return_value=(search_service, add_service))):
        await search.process_search(
            message, "Show", ContentType.SERIES, User(tg_id=5), db, chosen_title=_show(1),
        )

    search_service.search_releases_for_title.assert_awaited_once_with(
        ContentType.SERIES, 77, season=1, preferred_resolution=None,
    )


@pytest.mark.asyncio
async def test_whole_series_sends_series_search_and_lists_nothing():
    from bot.handlers import search

    search_service, add_service, message, status_msg, db = _mocks()
    with patch.object(search, "get_services", AsyncMock(return_value=(search_service, add_service))):
        await search.process_search(
            message, "Show", ContentType.SERIES, User(tg_id=5), db,
            chosen_title=_show(4), season_override=0, title_created=True,
        )

    search_service.search_releases_for_title.assert_not_awaited()
    add_service.sonarr.search_series.assert_awaited_once_with(77)
    assert "Sonarr сам ищет все сезоны — релизы выберет профиль" in _texts(status_msg)
    # A series this search added is all-off; SeriesSearch skips unmonitored
    # seasons, so "весь сериал" monitors every season first.
    add_service.sonarr.set_series_monitored.assert_awaited_once_with(77, True)
    add_service.sonarr.set_season_monitoring.assert_awaited_once_with(77, "all")


@pytest.mark.asyncio
async def test_whole_series_on_an_existing_series_keeps_its_monitoring():
    from bot.handlers import search

    search_service, add_service, message, status_msg, db = _mocks()
    with patch.object(search, "get_services", AsyncMock(return_value=(search_service, add_service))):
        await search.process_search(
            message, "Show", ContentType.SERIES, User(tg_id=5), db,
            chosen_title=_show(4), season_override=0, title_created=False,
        )

    search_service.search_releases_for_title.assert_not_awaited()
    add_service.sonarr.search_series.assert_awaited_once_with(77)
    add_service.sonarr.set_series_monitored.assert_not_awaited()
    add_service.sonarr.set_season_monitoring.assert_not_awaited()
    assert "Sonarr сам ищет" in _texts(status_msg)


# ------------------------------------------------------------------ GRAB-13
@pytest.mark.asyncio
async def test_episode_in_query_narrows_to_that_episode_and_season_packs():
    from bot.handlers import search

    results = [
        _rel("e5", episode=5), _rel("e6", episode=6), _rel("pack", pack=True), _rel("e5b", episode=5),
    ]
    search_service, add_service, message, _status, db = _mocks(
        parsed={"title": "Show", "year": None, "season": 1, "episode": 5, "quality": None},
        results=results,
    )
    with patch.object(search, "get_services", AsyncMock(return_value=(search_service, add_service))), \
            patch.object(search, "_render_results_page", AsyncMock()):
        await search.process_search(
            message, "Show S01E05", ContentType.SERIES, User(tg_id=5), db, chosen_title=_show(3),
        )

    saved = db.save_session.await_args.args[1]
    assert [r.guid for r in saved.results] == ["e5", "pack", "e5b"]


def test_season_pack_is_flagged_in_the_list():
    from bot.ui.formatters import Formatters

    text = Formatters.format_search_result(_rel("pack", season=2, pack=True), 1)
    assert "пак сезона 2" in text
    assert "пак сезона" not in Formatters.format_search_result(_rel("e", episode=3), 1)


# ------------------------------------------------------------------ GRAB-06
_ANY = QualityProfile(id=1, name="Any")
_HD = QualityProfile(id=4, name="HD-1080p")
_UHD = QualityProfile(id=7, name="4k/1080p")


def test_profile_fallback_chain():
    from bot.services.add_service import AddService

    profiles = [_ANY, _HD, _UHD]
    pick = AddService.resolve_profile_with_source
    assert pick(profiles, 4, default_id=7) == (_HD, "user")
    assert pick(profiles, None, default_id=4) == (_HD, "config")
    assert pick(profiles, 99, default_id=98) == (_UHD, "name")
    assert pick(profiles, None, default_id=None) == (_UHD, "name")
    assert pick([_ANY, _HD], None, default_id=None) == (_ANY, "first")
    assert pick([_ANY, QualityProfile(id=9, name="Ultra-HD 2160p")], None)[1] == "name"


def test_default_profile_ids_come_from_the_environment(monkeypatch):
    from bot.config import get_settings

    monkeypatch.setenv("RADARR_DEFAULT_PROFILE_ID", "7")
    monkeypatch.setenv("SONARR_DEFAULT_PROFILE_ID", "8")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.radarr_default_profile_id == 7
    assert settings.sonarr_default_profile_id == 8

    monkeypatch.delenv("RADARR_DEFAULT_PROFILE_ID")
    monkeypatch.delenv("SONARR_DEFAULT_PROFILE_ID")
    get_settings.cache_clear()
    assert get_settings().radarr_default_profile_id is None


@pytest.mark.asyncio
async def test_new_title_is_added_with_the_configured_default_profile(monkeypatch):
    from bot.config import get_settings
    from bot.handlers.search.commands import _resolve_arr_entry
    from bot.models import MovieInfo

    monkeypatch.setenv("RADARR_DEFAULT_PROFILE_ID", "4")
    get_settings.cache_clear()

    add_service = MagicMock()
    add_service.radarr.get_quality_profiles = AsyncMock(return_value=[_ANY, _HD, _UHD])
    add_service.radarr.get_root_folders = AsyncMock(return_value=[RootFolder(id=1, path="/movies")])
    add_service.add_movie = AsyncMock(return_value=(MovieInfo(tmdb_id=1, title="F", year=2020, radarr_id=3), MagicMock()))
    from bot.services.add_service import AddService
    add_service.resolve_root_folder = AddService.resolve_root_folder

    await _resolve_arr_entry(
        add_service, ContentType.MOVIE, MovieInfo(tmdb_id=1, title="F", year=2020), User(tg_id=5),
    )
    assert add_service.add_movie.await_args.kwargs["quality_profile_id"] == 4


@pytest.mark.asyncio
async def test_new_series_without_any_default_prefers_the_4k_profile():
    from bot.handlers.search.commands import _resolve_arr_entry
    from bot.services.add_service import AddService

    add_service = MagicMock()
    add_service.sonarr.get_quality_profiles = AsyncMock(return_value=[_ANY, _UHD])
    add_service.sonarr.get_root_folders = AsyncMock(return_value=[RootFolder(id=1, path="/tv")])
    add_service.add_series = AsyncMock(return_value=(SeriesInfo(tvdb_id=1, title="S", sonarr_id=3), MagicMock()))
    add_service.resolve_root_folder = AddService.resolve_root_folder

    await _resolve_arr_entry(
        add_service, ContentType.SERIES, SeriesInfo(tvdb_id=1, title="S"), User(tg_id=5),
    )
    assert add_service.add_series.await_args.kwargs["quality_profile_id"] == 7


def test_settings_warn_when_the_first_profile_is_the_fallback():
    from bot.ui.formatters import Formatters

    text = Formatters.format_user_preferences(
        UserPreferences(), [_ANY, _HD], [], [_ANY, _UHD], [],
        radarr_default=(_ANY, "first"), sonarr_default=(_UHD, "name"),
    )
    radarr_part, sonarr_part = text.split("Sonarr", 1)
    assert "⚠️" in radarr_part and "Any" in radarr_part
    assert "⚠️" not in sonarr_part
    assert "4k/1080p" in sonarr_part


@pytest.mark.asyncio
async def test_settings_menu_passes_the_fallback_through(monkeypatch):
    from bot.handlers import settings as settings_handler

    add_service = MagicMock()
    add_service.get_radarr_profiles = AsyncMock(return_value=[_ANY, _HD])
    add_service.get_radarr_root_folders = AsyncMock(return_value=[])
    add_service.get_sonarr_profiles = AsyncMock(return_value=[_ANY, _UHD])
    add_service.get_sonarr_root_folders = AsyncMock(return_value=[])
    with patch.object(settings_handler, "_get_add_service", AsyncMock(return_value=add_service)):
        text, _kb = await settings_handler._render_settings_menu(User(tg_id=5))

    radarr_part, _sonarr_part = text.split("Sonarr", 1)
    assert "⚠️" in radarr_part
