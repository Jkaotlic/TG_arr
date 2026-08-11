"""C5-handler-perf: parallelize calendar + emby fetches, cache resolved trending series.

PERF-03/LOGIC-05  bot/handlers/calendar.py  — Sonarr/Radarr/Lidarr calendars fetched
                  concurrently via asyncio.gather(return_exceptions=True); merged output
                  and per-source error tolerance preserved.
PERF-04           bot/handlers/emby.py      — server_info/libraries/sessions fetched
                  concurrently via asyncio.gather; same rendered status.
PERF-07           bot/handlers/trending.py  — a series whose tvdb_id is resolved on the
                  add path is written back to the module cache so a second add does not
                  re-run the Sonarr lookup.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.models import MovieInfo, SeriesInfo
from tests.conftest import callback_with_status as _callback_with_status

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _answer_capture():
    """An async answer_func that records the kwargs it was called with."""
    captured: dict = {}

    async def answer_func(**kwargs):
        captured.update(kwargs)

    return answer_func, captured


# ---------------------------------------------------------------------------
# PERF-03 / LOGIC-05: calendar fetched concurrently, merged + error-tolerant
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_calendar_runs_fetches_concurrently():
    """The Radarr and Sonarr (and Lidarr) calendar calls must overlap
    (gather), not run strictly back-to-back.

    TEST-09: deterministic barrier instead of a real asyncio.sleep race — each
    fake call records "start", then blocks until all three have started. If
    `_fetch_and_send_calendar` awaited them sequentially, the first would
    deadlock and the test would time out instead of completing.

    Rollback 2026-08-10: three sources again (Radarr movies, Sonarr episodes,
    Lidarr albums) — Scryer's single `calendarEpisodes` query covering every
    video facet is gone; each *arr client answers its own facet.
    """
    from bot.handlers import calendar

    order: list[str] = []
    started = 0
    all_started = asyncio.Event()
    lock = asyncio.Lock()

    async def _barrier(name):
        nonlocal started
        order.append(f"{name}:start")
        async with lock:
            started += 1
            if started == 3:
                all_started.set()
        await asyncio.wait_for(all_started.wait(), timeout=5)
        order.append(f"{name}:end")

    radarr = MagicMock()

    async def radarr_calendar(days):
        await _barrier("radarr")
        return [{"title": "Film", "release_date": "2026-07-06"}]

    radarr.get_calendar = radarr_calendar

    sonarr = MagicMock()

    async def sonarr_calendar(days):
        await _barrier("sonarr")
        return [{"series_title": "Show", "title": "", "season": 1, "episode": 2, "air_date": "2026-07-05"}]

    sonarr.get_calendar = sonarr_calendar

    lidarr = MagicMock()

    async def lidarr_calendar(days):
        await _barrier("lidarr")
        return [{"a": 1}]

    lidarr.get_calendar = lidarr_calendar

    answer_func, captured = _answer_capture()

    with patch.object(calendar, "get_radarr", AsyncMock(return_value=radarr)), \
         patch.object(calendar, "get_sonarr", AsyncMock(return_value=sonarr)), \
         patch.object(calendar, "get_lidarr", AsyncMock(return_value=lidarr)), \
         patch.object(calendar.Formatters, "format_calendar", return_value="OK") as fmt:
        await calendar._fetch_and_send_calendar(7, answer_func=answer_func)

    # Concurrency: all three started before any finished.
    assert set(order[:3]) == {"radarr:start", "sonarr:start", "lidarr:start"}, order

    # Radarr's calendar feeds "movies", Sonarr's feeds "episodes" — no split
    # step needed any more (that was Scryer's job).
    args = fmt.call_args.args
    kwargs = fmt.call_args.kwargs
    assert args[0][0]["series_title"] == "Show"
    assert args[1][0]["title"] == "Film"
    assert kwargs.get("albums") == [{"a": 1}]
    assert kwargs.get("days") == 7
    assert captured["text"] == "OK"
    assert "⚠️" not in captured["text"]


@pytest.mark.asyncio
async def test_calendar_one_source_fails_others_survive():
    """A failing source contributes a warning, the other still renders."""
    from bot.handlers import calendar

    radarr = MagicMock()
    radarr.get_calendar = AsyncMock(side_effect=RuntimeError("radarr down"))
    sonarr = MagicMock()
    sonarr.get_calendar = AsyncMock(return_value=[])
    lidarr = MagicMock()
    lidarr.get_calendar = AsyncMock(return_value=[{"a": 1}])

    answer_func, captured = _answer_capture()

    with patch.object(calendar, "get_radarr", AsyncMock(return_value=radarr)), \
         patch.object(calendar, "get_sonarr", AsyncMock(return_value=sonarr)), \
         patch.object(calendar, "get_lidarr", AsyncMock(return_value=lidarr)), \
         patch.object(calendar.Formatters, "format_calendar", return_value="OK") as fmt:
        await calendar._fetch_and_send_calendar(7, answer_func=answer_func)

    kwargs = fmt.call_args.kwargs
    assert kwargs.get("albums") == [{"a": 1}]
    assert "⚠️" in captured["text"]
    assert "Radarr" in captured["text"]


@pytest.mark.asyncio
async def test_calendar_without_lidarr_omits_albums():
    from bot.handlers import calendar

    radarr = MagicMock()
    radarr.get_calendar = AsyncMock(return_value=[])
    sonarr = MagicMock()
    sonarr.get_calendar = AsyncMock(return_value=[])

    answer_func, captured = _answer_capture()

    with patch.object(calendar, "get_radarr", AsyncMock(return_value=radarr)), \
         patch.object(calendar, "get_sonarr", AsyncMock(return_value=sonarr)), \
         patch.object(calendar, "get_lidarr", AsyncMock(return_value=None)), \
         patch.object(calendar.Formatters, "format_calendar", return_value="OK") as fmt:
        await calendar._fetch_and_send_calendar(7, answer_func=answer_func)

    assert fmt.call_args.kwargs.get("albums") == []
    assert "⚠️" not in captured["text"]


@pytest.mark.asyncio
async def test_collect_calendar_merges_both_services():
    """Task 13 brief's mandated test — the shared `_collect_calendar(radarr,
    sonarr, days)` helper merges both services' results into one date-sorted
    list."""
    from bot.handlers.calendar import _collect_calendar

    radarr, sonarr = AsyncMock(), AsyncMock()
    radarr.get_calendar.return_value = [{"title": "Dune", "release_date": "2026-08-11"}]
    sonarr.get_calendar.return_value = [{"title": "Fargo", "release_date": "2026-08-12"}]

    items = await _collect_calendar(radarr, sonarr, days=7)

    titles = {item["title"] for item in items}
    assert titles == {"Dune", "Fargo"}
    # Sorted by date: Dune (08-11) before Fargo (08-12).
    assert [item["title"] for item in items] == ["Dune", "Fargo"]


@pytest.mark.asyncio
async def test_trending_add_series_resolves_tvdb_id_via_sonarr_lookup():
    """Rollback 2026-08-10: TMDb trending carries no tvdb_id (Scryer used to
    resolve titles by name/metadata id instead) — the handler must resolve
    one via a guarded Sonarr lookup before Sonarr's own `add_series`."""
    from bot.handlers import trending
    from bot.models import ActionLog, ActionType, ContentType, QualityProfile, RootFolder

    series = SeriesInfo(tvdb_id=0, tmdb_id=55, title="Some Series", year=2024)
    trending._trending_series_cache.clear()
    trending._cache_put(trending._trending_series_cache, 55, series)

    resolved = SeriesInfo(tvdb_id=999, tmdb_id=55, title="Some Series", year=2024)
    added = SeriesInfo(tvdb_id=999, tmdb_id=55, title="Some Series", year=2024, sonarr_id=3)
    action = ActionLog(
        user_id=1, action_type=ActionType.ADD, content_type=ContentType.SERIES, success=True
    )
    add_service = AsyncMock()
    add_service.radarr = AsyncMock()
    add_service.get_sonarr_profiles = AsyncMock(return_value=[QualityProfile(id=1, name="HD")])
    add_service.get_sonarr_root_folders = AsyncMock(return_value=[RootFolder(id=1, path="/tv")])
    add_service.add_series = AsyncMock(return_value=(added, action))

    search_service = AsyncMock()
    search_service.lookup_series = AsyncMock(return_value=[resolved])

    cb, status_msg = _callback_with_status()
    db = AsyncMock()
    db_user = MagicMock()
    db_user.tg_id = 1
    db_user.preferences = MagicMock(sonarr_quality_profile_id=None, sonarr_root_folder_id=None)

    from bot.ui.callbacks import AddContentCB

    with patch.object(trending, "get_radarr", AsyncMock()), \
         patch.object(trending, "get_sonarr", AsyncMock()), \
         patch.object(trending, "get_qbittorrent", AsyncMock()), \
         patch.object(trending, "AddService", return_value=add_service), \
         patch.object(trending, "SearchService", return_value=search_service):
        await trending.handle_add_series_from_trending(
            cb, AddContentCB(kind="series", tmdb_id=55), db_user, db
        )

    add_service.add_series.assert_awaited_once()
    assert status_msg.edit_text.await_count >= 1
    trending._trending_series_cache.clear()


@pytest.mark.asyncio
async def test_trending_add_movie_still_works():
    from bot.handlers import trending
    from bot.models import ActionLog, ActionType, ContentType, QualityProfile, RootFolder

    movie = MovieInfo(tmdb_id=99, title="Some Movie", year=2024)
    trending._trending_movies_cache.clear()
    trending._cache_put(trending._trending_movies_cache, 99, movie)

    added = MovieInfo(tmdb_id=99, title="Some Movie", year=2024, radarr_id=5)
    action = ActionLog(
        user_id=1, action_type=ActionType.ADD, content_type=ContentType.MOVIE, success=True
    )
    add_service = AsyncMock()
    add_service.get_radarr_profiles = AsyncMock(return_value=[QualityProfile(id=1, name="HD")])
    add_service.get_radarr_root_folders = AsyncMock(return_value=[RootFolder(id=1, path="/movies")])
    add_service.add_movie = AsyncMock(return_value=(added, action))

    cb, status_msg = _callback_with_status()
    db = AsyncMock()
    db_user = MagicMock()
    db_user.tg_id = 1
    db_user.preferences = MagicMock(radarr_quality_profile_id=None, radarr_root_folder_id=None)

    from bot.ui.callbacks import AddContentCB

    with patch.object(trending, "get_radarr", AsyncMock()), \
         patch.object(trending, "get_sonarr", AsyncMock()), \
         patch.object(trending, "get_qbittorrent", AsyncMock()), \
         patch.object(trending, "AddService", return_value=add_service):
        await trending.handle_add_movie_from_trending(
            cb, AddContentCB(kind="movie", tmdb_id=99), db_user, db
        )

    add_service.add_movie.assert_awaited_once()
    assert "Some Movie" in status_msg.edit_text.await_args.args[0]
    trending._trending_movies_cache.clear()

