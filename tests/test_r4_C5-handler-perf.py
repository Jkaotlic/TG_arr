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
    """The three get_calendar() calls must overlap in time (gather), not run
    strictly back-to-back.

    TEST-09: deterministic barrier instead of a real asyncio.sleep race —
    each fake get_calendar() records "start", then blocks until all three
    have recorded "start" before returning. If _fetch_and_send_calendar
    awaited them sequentially, the first call would deadlock waiting for the
    2nd/3rd to start and the test would time out instead of completing.
    """
    from bot.handlers import calendar

    order: list[str] = []
    started = 0
    all_started = asyncio.Event()
    lock = asyncio.Lock()

    def make_client(name, payload):
        async def get_calendar(days):
            nonlocal started
            order.append(f"{name}:start")
            async with lock:
                started += 1
                if started == 3:
                    all_started.set()
            await asyncio.wait_for(all_started.wait(), timeout=5)
            order.append(f"{name}:end")
            return payload

        client = MagicMock()
        client.get_calendar = get_calendar
        return client

    sonarr = make_client("sonarr", [{"e": 1}])
    radarr = make_client("radarr", [{"m": 1}])
    lidarr = make_client("lidarr", [{"a": 1}])

    answer_func, captured = _answer_capture()

    with patch.object(calendar, "get_sonarr", AsyncMock(return_value=sonarr)), \
         patch.object(calendar, "get_radarr", AsyncMock(return_value=radarr)), \
         patch.object(calendar, "get_lidarr", AsyncMock(return_value=lidarr)), \
         patch.object(calendar.Formatters, "format_calendar",
                      return_value="OK") as fmt:
        await calendar._fetch_and_send_calendar(7, answer_func=answer_func)

    # Concurrency: all three started before any finished.
    assert order[:3] == ["sonarr:start", "radarr:start", "lidarr:start"], order

    # Merged output preserved: the formatter received each source's payload.
    kwargs = fmt.call_args.kwargs
    args = fmt.call_args.args
    assert args[0] == [{"e": 1}]            # episodes
    assert args[1] == [{"m": 1}]            # movies
    assert kwargs.get("albums") == [{"a": 1}]
    assert kwargs.get("days") == 7
    assert captured["text"] == "OK"
    assert "⚠️" not in captured["text"]


@pytest.mark.asyncio
async def test_calendar_one_source_fails_others_survive():
    """If Radarr errors, episodes/albums still render and only Radarr is in the
    warning line (error tolerance preserved by return_exceptions=True)."""
    from bot.handlers import calendar

    sonarr = MagicMock()
    sonarr.get_calendar = AsyncMock(return_value=[{"e": 1}])
    radarr = MagicMock()
    radarr.get_calendar = AsyncMock(side_effect=RuntimeError("boom <bad>"))
    lidarr = MagicMock()
    lidarr.get_calendar = AsyncMock(return_value=[{"a": 1}])

    answer_func, captured = _answer_capture()

    with patch.object(calendar, "get_sonarr", AsyncMock(return_value=sonarr)), \
         patch.object(calendar, "get_radarr", AsyncMock(return_value=radarr)), \
         patch.object(calendar, "get_lidarr", AsyncMock(return_value=lidarr)), \
         patch.object(calendar.Formatters, "format_calendar",
                      return_value="BODY") as fmt:
        await calendar._fetch_and_send_calendar(14, answer_func=answer_func)

    # Good sources still passed through to the formatter.
    args = fmt.call_args.args
    assert args[0] == [{"e": 1}]                       # episodes survived
    assert args[1] == []                              # movies empty (radarr failed)
    assert fmt.call_args.kwargs.get("albums") == [{"a": 1}]

    text = captured["text"]
    assert "⚠️" in text
    assert "Radarr:" in text
    assert "Sonarr:" not in text and "Lidarr:" not in text
    # SEC-21: exception string is html-escaped before sending.
    assert "&lt;bad&gt;" in text
    assert "<bad>" not in text


@pytest.mark.asyncio
async def test_calendar_without_lidarr_omits_albums():
    """When Lidarr is not configured (None), no album fetch is attempted and no
    Lidarr error appears."""
    from bot.handlers import calendar

    sonarr = MagicMock()
    sonarr.get_calendar = AsyncMock(return_value=[{"e": 1}])
    radarr = MagicMock()
    radarr.get_calendar = AsyncMock(return_value=[{"m": 1}])

    answer_func, captured = _answer_capture()

    with patch.object(calendar, "get_sonarr", AsyncMock(return_value=sonarr)), \
         patch.object(calendar, "get_radarr", AsyncMock(return_value=radarr)), \
         patch.object(calendar, "get_lidarr", AsyncMock(return_value=None)), \
         patch.object(calendar.Formatters, "format_calendar",
                      return_value="BODY") as fmt:
        await calendar._fetch_and_send_calendar(30, answer_func=answer_func)

    assert fmt.call_args.kwargs.get("albums") == []
    assert "Lidarr" not in captured["text"]
    assert "⚠️" not in captured["text"]


# ---------------------------------------------------------------------------
# PERF-04: emby status fetches run concurrently, same rendered output
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_emby_status_runs_fetches_concurrently():
    """server_info / libraries / sessions must overlap (gather), and the same
    status text/keyboard must be produced.

    TEST-09: deterministic barrier instead of a real asyncio.sleep race —
    each fake fetch records "start", then blocks until all three have
    recorded "start" before returning. A sequential await chain would
    deadlock here instead of completing.
    """
    from bot.handlers import emby as emby_handler

    order: list[str] = []
    started = 0
    all_started = asyncio.Event()
    lock = asyncio.Lock()

    async def _mark_started_and_wait():
        nonlocal started
        async with lock:
            started += 1
            if started == 3:
                all_started.set()
        await asyncio.wait_for(all_started.wait(), timeout=5)

    info = MagicMock(
        server_name="S", version="4.8", operating_system="Linux",
        has_pending_restart=False, has_update_available=True,
        can_self_restart=True, can_self_update=False,
    )

    async def get_server_info():
        order.append("info:start")
        await _mark_started_and_wait()
        order.append("info:end")
        return info

    async def get_libraries():
        order.append("lib:start")
        await _mark_started_and_wait()
        order.append("lib:end")
        return ["lib1", "lib2"]

    async def get_sessions():
        order.append("sess:start")
        await _mark_started_and_wait()
        order.append("sess:end")
        return ["s1", "s2", "s3"]

    emby_client = MagicMock()
    emby_client.get_server_info = get_server_info
    emby_client.get_libraries = get_libraries
    emby_client.get_sessions = get_sessions

    message = MagicMock()
    message.answer = AsyncMock()

    with patch.object(emby_handler, "get_emby", AsyncMock(return_value=emby_client)), \
         patch.object(emby_handler.Formatters, "format_emby_status",
                      return_value="STATUS") as fmt, \
         patch.object(emby_handler.Keyboards, "emby_main", return_value="KB"):
        await emby_handler.show_emby_status(message)

    # Concurrency: all three started before any finished.
    assert set(order[:3]) == {"info:start", "lib:start", "sess:start"}, order

    # LOGIC-19: format_emby_status now takes the EmbyServerInfo positionally.
    fmt_args, fmt_kwargs = fmt.call_args.args, fmt.call_args.kwargs
    assert fmt_args[0] is info
    assert fmt_kwargs["active_sessions"] == 3
    assert fmt_kwargs["libraries"] == ["lib1", "lib2"]

    message.answer.assert_awaited_once()
    sent = message.answer.await_args
    assert sent.args[0] == "STATUS"
    assert sent.kwargs["reply_markup"] == "KB"


@pytest.mark.asyncio
async def test_emby_status_error_renders_error_text():
    """An EmbyError from any of the parallel fetches must still surface the
    formatted error message (error tolerance preserved)."""
    from bot.clients.emby import EmbyError
    from bot.handlers import emby as emby_handler

    emby_client = MagicMock()
    emby_client.get_server_info = AsyncMock(side_effect=EmbyError("down"))
    emby_client.get_libraries = AsyncMock(return_value=[])
    emby_client.get_sessions = AsyncMock(return_value=[])

    message = MagicMock()
    message.answer = AsyncMock()

    with patch.object(emby_handler, "get_emby", AsyncMock(return_value=emby_client)), \
         patch.object(emby_handler.Formatters, "format_error",
                      return_value="ERR") as fmt_err:
        await emby_handler.show_emby_status(message)

    fmt_err.assert_called_once()
    message.answer.assert_awaited_once()
    assert message.answer.await_args.args[0] == "ERR"


# ---------------------------------------------------------------------------
# PERF-07: resolved series is cached so a 2nd add does not re-lookup
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_trending_add_series_caches_resolved_tvdb():
    """First add resolves tvdb via AddService.resolve_series_tvdb_id; the
    resolved object is written back to the cache so the second add skips
    resolution entirely (still unresolved series never even calls it again
    because the cache now holds the resolved one).

    LOGIC-11: TVDB resolution now lives in AddService.resolve_series_tvdb_id
    (shared with grab.py) instead of an inline `sonarr.lookup_series` call in
    the handler.
    """
    from bot.handlers import trending

    tmdb_id = 555
    # Cached series from trending has no tvdb_id (TMDb trending returns 0).
    unresolved = SeriesInfo(tvdb_id=0, tmdb_id=tmdb_id, title="Resolved Show", year=2021)
    resolved = SeriesInfo(tvdb_id=98765, tmdb_id=tmdb_id, title="Resolved Show", year=2021)

    action = MagicMock(success=True, error_message=None)
    add_service = MagicMock()
    add_service.get_sonarr_profiles = AsyncMock(return_value=[MagicMock(id=1)])
    add_service.get_sonarr_root_folders = AsyncMock(return_value=[MagicMock(id=1, path="/tv")])
    add_service.add_series = AsyncMock(return_value=(resolved, action))
    # Mimics the real AddService.resolve_series_tvdb_id: a no-op passthrough
    # once the series already carries a tvdb_id, otherwise resolves it.
    add_service.resolve_series_tvdb_id = AsyncMock(
        side_effect=lambda s: resolved if not s.tvdb_id else s
    )

    db = AsyncMock()
    db_user = MagicMock()
    db_user.tg_id = 123
    db_user.preferences = MagicMock(sonarr_quality_profile_id=None, sonarr_root_folder_id=None)

    trending._trending_series_cache.clear()
    trending._trending_series_cache[tmdb_id] = unresolved

    cb, _ = _callback_with_status()
    cb.data = None

    from bot.ui.callbacks import AddContentCB

    with patch.object(trending, "get_prowlarr", AsyncMock()), \
         patch.object(trending, "get_radarr", AsyncMock()), \
         patch.object(trending, "get_sonarr", AsyncMock()), \
         patch.object(trending, "get_qbittorrent", AsyncMock()), \
         patch.object(trending, "AddService", return_value=add_service):
        # First add: must resolve tvdb via AddService.
        await trending.handle_add_series_from_trending(
            cb, AddContentCB(kind="series", tmdb_id=tmdb_id), db_user=db_user, db=db
        )
        assert add_service.resolve_series_tvdb_id.await_count == 1

        # Cache must now hold the *resolved* series (with a real tvdb_id).
        cached = trending._trending_series_cache[tmdb_id]
        assert cached.tvdb_id == 98765

        # Second add: cache already resolved → resolve_series_tvdb_id is a
        # passthrough (no lookup performed inside it), called once more but
        # cheaply since the cached series already has a tvdb_id.
        cb2, _ = _callback_with_status()
        cb2.data = None
        await trending.handle_add_series_from_trending(
            cb2, AddContentCB(kind="series", tmdb_id=tmdb_id), db_user=db_user, db=db
        )
        assert add_service.resolve_series_tvdb_id.await_count == 1  # not called again

    trending._trending_series_cache.clear()


@pytest.mark.asyncio
async def test_trending_add_series_already_resolved_skips_lookup():
    """A series already carrying a tvdb_id must never trigger tvdb resolution.

    LOGIC-11: resolution now goes through AddService.resolve_series_tvdb_id;
    the handler must not even call it when series.tvdb_id is already set.
    """
    from bot.handlers import trending

    tmdb_id = 777
    resolved = SeriesInfo(tvdb_id=111, tmdb_id=tmdb_id, title="Has TVDB", year=2022)

    action = MagicMock(success=True, error_message=None)
    add_service = MagicMock()
    add_service.get_sonarr_profiles = AsyncMock(return_value=[MagicMock(id=1)])
    add_service.get_sonarr_root_folders = AsyncMock(return_value=[MagicMock(id=1, path="/tv")])
    add_service.add_series = AsyncMock(return_value=(resolved, action))
    add_service.resolve_series_tvdb_id = AsyncMock(return_value=resolved)

    db = AsyncMock()
    db_user = MagicMock()
    db_user.tg_id = 123
    db_user.preferences = MagicMock(sonarr_quality_profile_id=None, sonarr_root_folder_id=None)

    trending._trending_series_cache.clear()
    trending._trending_series_cache[tmdb_id] = resolved

    cb, _ = _callback_with_status()
    cb.data = None

    from bot.ui.callbacks import AddContentCB

    with patch.object(trending, "get_prowlarr", AsyncMock()), \
         patch.object(trending, "get_radarr", AsyncMock()), \
         patch.object(trending, "get_sonarr", AsyncMock()), \
         patch.object(trending, "get_qbittorrent", AsyncMock()), \
         patch.object(trending, "AddService", return_value=add_service):
        await trending.handle_add_series_from_trending(
            cb, AddContentCB(kind="series", tmdb_id=tmdb_id), db_user=db_user, db=db
        )

    add_service.resolve_series_tvdb_id.assert_not_awaited()
    trending._trending_series_cache.clear()


@pytest.mark.asyncio
async def test_trending_add_movie_still_works():
    """Behaviour-preserving guard: movie add path is untouched by PERF-07."""
    from bot.handlers import trending

    added = MovieInfo(tmdb_id=321, title="A Movie", year=2024)
    action = MagicMock(success=True, error_message=None)
    add_service = MagicMock()
    add_service.get_radarr_profiles = AsyncMock(return_value=[MagicMock(id=1)])
    add_service.get_radarr_root_folders = AsyncMock(return_value=[MagicMock(id=1, path="/movies")])
    add_service.add_movie = AsyncMock(return_value=(added, action))

    db = AsyncMock()
    db_user = MagicMock()
    db_user.tg_id = 123
    db_user.preferences = MagicMock(radarr_quality_profile_id=None, radarr_root_folder_id=None)

    cb, status_msg = _callback_with_status()
    cb.data = None

    trending._trending_movies_cache.clear()
    trending._trending_movies_cache[321] = added

    from bot.ui.callbacks import AddContentCB

    with patch.object(trending, "get_prowlarr", AsyncMock()), \
         patch.object(trending, "get_radarr", AsyncMock()), \
         patch.object(trending, "get_sonarr", AsyncMock()), \
         patch.object(trending, "get_qbittorrent", AsyncMock()), \
         patch.object(trending, "AddService", return_value=add_service):
        await trending.handle_add_movie_from_trending(
            cb, AddContentCB(kind="movie", tmdb_id=321), db_user=db_user, db=db
        )

    add_service.add_movie.assert_awaited_once()
    sent = status_msg.edit_text.await_args_list[-1].args[0]
    assert "A Movie" in sent
    trending._trending_movies_cache.clear()
