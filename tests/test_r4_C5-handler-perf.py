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
    """The Scryer and Lidarr calendar calls must overlap (gather), not run
    strictly back-to-back.

    TEST-09: deterministic barrier instead of a real asyncio.sleep race — each
    fake call records "start", then blocks until both have started. If
    `_fetch_and_send_calendar` awaited them sequentially, the first would
    deadlock and the test would time out instead of completing.

    Migration 2026-07-28: two sources instead of three — Scryer's single
    `calendarEpisodes` covers movies, series and anime.
    """
    from bot.handlers import calendar
    from bot.models import ContentType, ScryerCalendarItem

    order: list[str] = []
    started = 0
    all_started = asyncio.Event()
    lock = asyncio.Lock()

    async def _barrier(name):
        nonlocal started
        order.append(f"{name}:start")
        async with lock:
            started += 1
            if started == 2:
                all_started.set()
        await asyncio.wait_for(all_started.wait(), timeout=5)
        order.append(f"{name}:end")

    scryer = MagicMock()

    async def scryer_calendar(start_date, end_date):
        await _barrier("scryer")
        return [
            ScryerCalendarItem(
                id="e1", title_id="t1", title_name="Show",
                content_type=ContentType.SERIES, season_number=1, episode_number=2,
                air_date="2026-07-05",
            ),
            ScryerCalendarItem(
                id="m1", title_id="t2", title_name="Film",
                content_type=ContentType.MOVIE, air_date="2026-07-06",
            ),
        ]

    scryer.get_calendar = scryer_calendar

    lidarr = MagicMock()

    async def lidarr_calendar(days):
        await _barrier("lidarr")
        return [{"a": 1}]

    lidarr.get_calendar = lidarr_calendar

    answer_func, captured = _answer_capture()

    with patch.object(calendar, "get_scryer", AsyncMock(return_value=scryer)), \
         patch.object(calendar, "get_lidarr", AsyncMock(return_value=lidarr)), \
         patch.object(calendar.Formatters, "format_calendar", return_value="OK") as fmt:
        await calendar._fetch_and_send_calendar(7, answer_func=answer_func)

    # Concurrency: both started before either finished.
    assert order[:2] == ["scryer:start", "lidarr:start"], order

    # Scryer's single calendar is split back into episodes/movies for the formatter.
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

    scryer = MagicMock()
    scryer.get_calendar = AsyncMock(side_effect=RuntimeError("scryer down"))
    lidarr = MagicMock()
    lidarr.get_calendar = AsyncMock(return_value=[{"a": 1}])

    answer_func, captured = _answer_capture()

    with patch.object(calendar, "get_scryer", AsyncMock(return_value=scryer)), \
         patch.object(calendar, "get_lidarr", AsyncMock(return_value=lidarr)), \
         patch.object(calendar.Formatters, "format_calendar", return_value="OK") as fmt:
        await calendar._fetch_and_send_calendar(7, answer_func=answer_func)

    kwargs = fmt.call_args.kwargs
    assert kwargs.get("albums") == [{"a": 1}]
    assert "⚠️" in captured["text"]
    assert "Scryer" in captured["text"]


@pytest.mark.asyncio
async def test_calendar_without_lidarr_omits_albums():
    from bot.handlers import calendar

    scryer = MagicMock()
    scryer.get_calendar = AsyncMock(return_value=[])

    answer_func, captured = _answer_capture()

    with patch.object(calendar, "get_scryer", AsyncMock(return_value=scryer)), \
         patch.object(calendar, "get_lidarr", AsyncMock(return_value=None)), \
         patch.object(calendar.Formatters, "format_calendar", return_value="OK") as fmt:
        await calendar._fetch_and_send_calendar(7, answer_func=answer_func)

    assert fmt.call_args.kwargs.get("albums") == []
    assert "⚠️" not in captured["text"]


@pytest.mark.asyncio
async def test_trending_add_series_goes_straight_to_scryer():
    """PERF-07 (migrated): the TVDB-resolution round-trip is gone entirely —
    Scryer keys on its own metadata id, so a trending add is one call."""
    from bot.handlers import trending
    from bot.models import ActionLog, ActionType, ContentType

    series = SeriesInfo(tvdb_id=0, tmdb_id=55, title="Some Series", year=2024)
    trending._trending_series_cache.clear()
    trending._cache_put(trending._trending_series_cache, 55, series)

    action = ActionLog(
        user_id=1, action_type=ActionType.ADD, content_type=ContentType.SERIES, success=True
    )
    add_service = AsyncMock()
    add_service.add_and_queue_best = AsyncMock(return_value=(True, action, "Добавлено"))

    cb, status_msg = _callback_with_status()
    db = AsyncMock()
    db_user = MagicMock()
    db_user.tg_id = 1
    db_user.preferences = MagicMock(scryer_quality_profile_id=None, scryer_root_folder_id=None)

    from bot.ui.callbacks import AddContentCB

    with patch.object(trending, "get_scryer", AsyncMock()), \
         patch.object(trending, "get_qbittorrent", AsyncMock()), \
         patch.object(trending, "AddService", return_value=add_service):
        await trending.handle_add_series_from_trending(
            cb, AddContentCB(kind="series", tmdb_id=55), db_user, db
        )

    add_service.add_and_queue_best.assert_awaited_once()
    assert status_msg.edit_text.await_count >= 1
    trending._trending_series_cache.clear()


@pytest.mark.asyncio
async def test_trending_add_movie_still_works():
    from bot.handlers import trending
    from bot.models import ActionLog, ActionType, ContentType

    movie = MovieInfo(tmdb_id=99, title="Some Movie", year=2024)
    trending._trending_movies_cache.clear()
    trending._cache_put(trending._trending_movies_cache, 99, movie)

    action = ActionLog(
        user_id=1, action_type=ActionType.ADD, content_type=ContentType.MOVIE, success=True
    )
    add_service = AsyncMock()
    add_service.add_and_queue_best = AsyncMock(return_value=(True, action, "Добавлено"))

    cb, status_msg = _callback_with_status()
    db = AsyncMock()
    db_user = MagicMock()
    db_user.tg_id = 1
    db_user.preferences = MagicMock(scryer_quality_profile_id=None, scryer_root_folder_id=None)

    from bot.ui.callbacks import AddContentCB

    with patch.object(trending, "get_scryer", AsyncMock()), \
         patch.object(trending, "get_qbittorrent", AsyncMock()), \
         patch.object(trending, "AddService", return_value=add_service):
        await trending.handle_add_movie_from_trending(
            cb, AddContentCB(kind="movie", tmdb_id=99), db_user, db
        )

    add_service.add_and_queue_best.assert_awaited_once()
    assert "Some Movie" in status_msg.edit_text.await_args.args[0]
    trending._trending_movies_cache.clear()

