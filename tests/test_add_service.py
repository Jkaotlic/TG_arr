"""Tests for add-service / grab flow monitor-type selection (BUG-32) and SSRF (SEC-16)."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.models import (
    ContentType,
    MovieInfo,
    QualityInfo,
    RootFolder,
    SearchResult,
    SearchSession,
    SeriesInfo,
    User,
    UserPreferences,
)
from bot.services.add_service import AddService
from tests.conftest import build_add_service as _build_add_service


def _make_series() -> SeriesInfo:
    return SeriesInfo(
        tvdb_id=42,
        title="Test Series",
        year=2020,
    )


def _make_result(**over) -> SearchResult:
    kwargs = dict(
        guid="r-1",
        title="Test.Series.S01E05.1080p",
        indexer="Test",
        size=1_000_000_000,
        quality=QualityInfo(resolution="1080p"),
        detected_season=1,
        detected_episode=5,
        is_season_pack=False,
    )
    kwargs.update(over)
    return SearchResult(**kwargs)


@pytest.mark.asyncio
async def test_grab_single_episode_sets_monitor_none():
    """BUG-04 (overrides BUG-32): a single-episode/season grab must NOT monitor
    every season. Sonarr's 'existing' returns True for all seasons of a brand-new
    series, so grabbing one episode would silently auto-monitor the whole show.
    Use 'none' — the picked release is still grabbed via push; we just don't
    auto-pull unwanted seasons later."""
    from bot.handlers.search import _execute_grab

    series = _make_series()
    release = _make_result()

    session = SearchSession(
        user_id=111,
        query="Test Series S01E05",
        content_type=ContentType.SERIES,
        selected_result=release,
        selected_content=series,
    )

    user = User(tg_id=111, preferences=UserPreferences())

    # Mock message with async edit_text
    message = MagicMock()
    message.edit_text = AsyncMock()

    # Mock DB
    db = AsyncMock()

    # Mock services
    search_service = AsyncMock()
    search_service.lookup_series = AsyncMock(return_value=[series])
    # parse_query is sync; AsyncMock would return a coroutine and break .get()
    search_service.parse_query = MagicMock(return_value={"title": "Test Series", "year": 2020, "season": 1, "episode": 5, "quality": None})

    add_service = AsyncMock()
    add_service.get_sonarr_profiles = AsyncMock(
        return_value=[MagicMock(id=1, name="HD-1080p")]
    )
    add_service.get_sonarr_root_folders = AsyncMock(
        return_value=[RootFolder(id=1, path="/tv")]
    )

    # Stub grab_series_release, capture monitor_type
    captured: dict = {}

    async def fake_grab(**kwargs):
        captured.update(kwargs)
        action = MagicMock()
        action.user_id = None
        return (True, action, "ok")

    add_service.grab_series_release = fake_grab

    await _execute_grab(message, session, user, db, search_service, add_service)

    assert captured.get("monitor_type") == "none"


@pytest.mark.asyncio
async def test_grab_season_pack_sets_monitor_all():
    """Season-pack release → monitor_type='all'."""
    from bot.handlers.search import _execute_grab

    series = _make_series()
    release = _make_result(detected_episode=None, is_season_pack=True)

    session = SearchSession(
        user_id=111,
        query="Test Series S01",
        content_type=ContentType.SERIES,
        selected_result=release,
        selected_content=series,
    )
    user = User(tg_id=111, preferences=UserPreferences())

    message = MagicMock()
    message.edit_text = AsyncMock()
    db = AsyncMock()

    search_service = AsyncMock()
    search_service.parse_query = MagicMock(return_value={"title": "Test Series", "year": 2020, "season": 1, "episode": None, "quality": None})
    add_service = AsyncMock()
    add_service.get_sonarr_profiles = AsyncMock(
        return_value=[MagicMock(id=1, name="HD-1080p")]
    )
    add_service.get_sonarr_root_folders = AsyncMock(
        return_value=[RootFolder(id=1, path="/tv")]
    )

    captured: dict = {}

    async def fake_grab(**kwargs):
        captured.update(kwargs)
        action = MagicMock()
        action.user_id = None
        return (True, action, "ok")

    add_service.grab_series_release = fake_grab

    await _execute_grab(message, session, user, db, search_service, add_service)

    assert captured.get("monitor_type") == "all"


# ============================================================================
# SEC-16: SSRF-via-*arr-push_release
# ============================================================================


def _private_release(url: str = "http://192.168.1.1/download/evil") -> SearchResult:
    return SearchResult(
        guid="evil-guid",
        indexer="EvilIndexer",
        indexer_id=0,  # 0 so the direct-grab branch is skipped too
        title="Fake.Release.2024.1080p",
        size=1_000_000,
        seeders=1,
        leechers=0,
        protocol="torrent",
        download_url=url,
        publish_date=datetime(2024, 1, 1),
        detected_type=ContentType.MOVIE,
    )


@pytest.mark.asyncio
async def test_push_release_rejects_private_url_movie():
    """SEC-16: grab_movie_release must NOT call radarr.push_release for a private URL."""
    movie = MovieInfo(
        tmdb_id=123456,
        imdb_id="tt1234567",
        title="Test Movie",
        year=2024,
        radarr_id=42,
    )

    radarr = AsyncMock()
    radarr.get_movie_by_tmdb = AsyncMock(return_value=movie)  # already in library
    radarr.push_release = AsyncMock()
    radarr.search_movie = AsyncMock()

    svc = _build_add_service(radarr=radarr)

    release = _private_release("http://192.168.1.1/x")
    await svc.grab_movie_release(
        movie=movie,
        release=release,
        quality_profile_id=1,
        root_folder_path="/movies",
    )

    radarr.push_release.assert_not_called()


@pytest.mark.asyncio
async def test_push_release_rejects_loopback_url_series():
    """SEC-16: grab_series_release must NOT call sonarr.push_release for a loopback URL."""
    series = SeriesInfo(
        tvdb_id=654321,
        imdb_id="tt7654321",
        title="Test Series",
        year=2020,
        sonarr_id=77,
    )

    sonarr = AsyncMock()
    sonarr.get_series_by_tvdb = AsyncMock(return_value=series)
    sonarr.push_release = AsyncMock()
    sonarr.search_series = AsyncMock()

    svc = _build_add_service(sonarr=sonarr)

    release = _private_release("http://127.0.0.1:8080/file.torrent")
    await svc.grab_series_release(
        series=series,
        release=release,
        quality_profile_id=1,
        root_folder_path="/tv",
    )

    sonarr.push_release.assert_not_called()


@pytest.mark.asyncio
async def test_grab_movie_push_error_no_direct_grab_falls_to_autosearch():
    """BUG-05: *arr.grab_release(prowlarr_guid) is a dead path — Prowlarr's
    guid/indexerId are meaningless to Radarr's own /release cache and always
    404. When push_release raises APIError (transient, not an explicit
    rejection) the code must NOT call radarr.grab_release with the Prowlarr
    guid; it must fall straight through to the auto-search fallback with an
    honest message, even though indexer_id > 0 (would have triggered the old
    direct-grab branch)."""
    from bot.clients.base import APIError

    movie = MovieInfo(
        tmdb_id=123456,
        imdb_id="tt1234567",
        title="Test Movie",
        year=2024,
        radarr_id=42,
    )

    radarr = AsyncMock()
    radarr.get_movie_by_tmdb = AsyncMock(return_value=movie)
    radarr.push_release = AsyncMock(side_effect=APIError("boom"))
    radarr.search_movie = AsyncMock()

    svc = _build_add_service(radarr=radarr)

    release = _private_release("magnet:?xt=urn:btih:abcdef0123456789abcdef0123456789abcdef01")
    release = release.model_copy(update={"indexer_id": 5})  # positive on purpose
    ok, action, msg = await svc.grab_movie_release(
        movie=movie,
        release=release,
        quality_profile_id=1,
        root_folder_path="/movies",
    )

    assert not hasattr(radarr, "grab_release") or not radarr.grab_release.called
    radarr.search_movie.assert_awaited_once_with(42)
    assert ok is True
    assert action.success is True
    assert "автопоиск" in msg.lower()


@pytest.mark.asyncio
async def test_grab_series_push_error_no_direct_grab_falls_to_autosearch():
    """BUG-05: same as above for the series path — even with a positive
    indexer_id (would have triggered the old direct-grab branch), no
    sonarr.grab_release call must be made — the code goes straight to the
    auto-search fallback."""
    from bot.clients.base import APIError

    series = SeriesInfo(
        tvdb_id=654321,
        imdb_id="tt7654321",
        title="Test Series",
        year=2020,
        sonarr_id=77,
    )

    sonarr = AsyncMock()
    sonarr.get_series_by_tvdb = AsyncMock(return_value=series)
    sonarr.push_release = AsyncMock(side_effect=APIError("boom"))
    sonarr.search_series = AsyncMock()

    svc = _build_add_service(sonarr=sonarr)

    release = _make_result(
        guid="g-2",
        indexer_id=5,  # positive on purpose — old code would have tried direct grab
        download_url="magnet:?xt=urn:btih:abcdef0123456789abcdef0123456789abcdef01",
        detected_type=ContentType.SERIES,
    )
    ok, action, msg = await svc.grab_series_release(
        series=series,
        release=release,
        quality_profile_id=1,
        root_folder_path="/tv",
    )

    assert not hasattr(sonarr, "grab_release") or not sonarr.grab_release.called
    assert ok is True
    assert "автопоиск" in msg.lower() or "поиск" in msg.lower()


@pytest.mark.asyncio
async def test_push_release_allowed_for_magnet_url():
    """Sanity: a magnet URL (treated as public) still reaches radarr.push_release."""
    movie = MovieInfo(
        tmdb_id=123456,
        imdb_id="tt1234567",
        title="Test Movie",
        year=2024,
        radarr_id=42,
    )

    radarr = AsyncMock()
    radarr.get_movie_by_tmdb = AsyncMock(return_value=movie)
    radarr.push_release = AsyncMock(return_value={"approved": True})
    radarr.search_movie = AsyncMock()

    svc = _build_add_service(radarr=radarr)

    release = _private_release("magnet:?xt=urn:btih:abcdef0123456789abcdef0123456789abcdef01")
    success, _action, _msg = await svc.grab_movie_release(
        movie=movie,
        release=release,
        quality_profile_id=1,
        root_folder_path="/movies",
    )

    radarr.push_release.assert_awaited_once()
    assert success is True


# ============================================================================
# TEST-01: AddService.add_movie / add_series
# ============================================================================


def _make_movie(**over) -> MovieInfo:
    kwargs = dict(tmdb_id=555, imdb_id="tt5550000", title="Add Movie Test", year=2023)
    kwargs.update(over)
    return MovieInfo(**kwargs)


def _make_add_series(**over) -> SeriesInfo:
    kwargs = dict(tvdb_id=888, imdb_id="tt8880000", title="Add Series Test", year=2019)
    kwargs.update(over)
    return SeriesInfo(**kwargs)


@pytest.mark.asyncio
async def test_add_movie_existing_returns_without_calling_add():
    """TEST-01: if the movie is already in Radarr, add_movie must return it
    as-is and must NOT call radarr.add_movie."""
    movie = _make_movie()
    existing = _make_movie(radarr_id=101)

    radarr = AsyncMock()
    radarr.get_movie_by_tmdb = AsyncMock(return_value=existing)
    radarr.add_movie = AsyncMock()

    svc = _build_add_service(radarr=radarr)
    result, action = await svc.add_movie(
        movie=movie, quality_profile_id=1, root_folder_path="/movies",
    )

    assert result is existing
    assert action.success is True
    radarr.add_movie.assert_not_called()


@pytest.mark.asyncio
async def test_add_movie_success_calls_radarr_add():
    """TEST-01: not in library → radarr.add_movie is called and its result
    is returned with a successful ActionLog."""
    movie = _make_movie()
    added = _make_movie(radarr_id=202)

    radarr = AsyncMock()
    radarr.get_movie_by_tmdb = AsyncMock(return_value=None)
    radarr.add_movie = AsyncMock(return_value=added)

    svc = _build_add_service(radarr=radarr)
    result, action = await svc.add_movie(
        movie=movie, quality_profile_id=1, root_folder_path="/movies", tags=[1, 2],
    )

    assert result is added
    assert action.success is True
    assert action.content_type == ContentType.MOVIE
    radarr.add_movie.assert_awaited_once()
    call_kwargs = radarr.add_movie.await_args.kwargs
    assert call_kwargs["quality_profile_id"] == 1
    assert call_kwargs["root_folder_path"] == "/movies"
    assert call_kwargs["tags"] == [1, 2]


@pytest.mark.asyncio
async def test_add_movie_api_error_returns_failed_action():
    """TEST-01: APIError from radarr.add_movie → (None, ActionLog(success=False, error_message=...))."""
    from bot.clients.base import APIError

    movie = _make_movie()

    radarr = AsyncMock()
    radarr.get_movie_by_tmdb = AsyncMock(return_value=None)
    radarr.add_movie = AsyncMock(side_effect=APIError("Radarr rejected the payload"))

    svc = _build_add_service(radarr=radarr)
    result, action = await svc.add_movie(
        movie=movie, quality_profile_id=1, root_folder_path="/movies",
    )

    assert result is None
    assert action.success is False
    assert "Radarr rejected the payload" in action.error_message


@pytest.mark.asyncio
async def test_add_series_existing_returns_without_calling_add():
    """TEST-01: if the series is already in Sonarr, add_series must return it
    as-is and must NOT call sonarr.add_series."""
    series = _make_add_series()
    existing = _make_add_series(sonarr_id=303)

    sonarr = AsyncMock()
    sonarr.get_series_by_tvdb = AsyncMock(return_value=existing)
    sonarr.add_series = AsyncMock()

    svc = _build_add_service(sonarr=sonarr)
    result, action = await svc.add_series(
        series=series, quality_profile_id=1, root_folder_path="/tv",
    )

    assert result is existing
    assert action.success is True
    sonarr.add_series.assert_not_called()


@pytest.mark.asyncio
async def test_add_series_success_calls_sonarr_add():
    """TEST-01: not in library → sonarr.add_series is called and its result
    is returned with a successful ActionLog."""
    series = _make_add_series()
    added = _make_add_series(sonarr_id=404)

    sonarr = AsyncMock()
    sonarr.get_series_by_tvdb = AsyncMock(return_value=None)
    sonarr.add_series = AsyncMock(return_value=added)

    svc = _build_add_service(sonarr=sonarr)
    result, action = await svc.add_series(
        series=series, quality_profile_id=1, root_folder_path="/tv", monitor_type="future",
    )

    assert result is added
    assert action.success is True
    assert action.content_type == ContentType.SERIES
    sonarr.add_series.assert_awaited_once()
    call_kwargs = sonarr.add_series.await_args.kwargs
    assert call_kwargs["monitor_type"] == "future"
    assert call_kwargs["root_folder_path"] == "/tv"


@pytest.mark.asyncio
async def test_add_series_api_error_returns_failed_action():
    """TEST-01: APIError from sonarr.add_series → (None, ActionLog(success=False, error_message=...))."""
    from bot.clients.base import APIError

    series = _make_add_series()

    sonarr = AsyncMock()
    sonarr.get_series_by_tvdb = AsyncMock(return_value=None)
    sonarr.add_series = AsyncMock(side_effect=APIError("Sonarr rejected the payload"))

    svc = _build_add_service(sonarr=sonarr)
    result, action = await svc.add_series(
        series=series, quality_profile_id=1, root_folder_path="/tv",
    )

    assert result is None
    assert action.success is False
    assert "Sonarr rejected the payload" in action.error_message


# ============================================================================
# OBS-05: единое терминальное событие grab_completed
# ============================================================================


@pytest.mark.asyncio
async def test_grab_completed_logged_on_push_success():
    import structlog.testing

    movie = _make_movie(radarr_id=42)
    radarr = AsyncMock()
    radarr.get_movie_by_tmdb = AsyncMock(return_value=movie)
    radarr.push_release = AsyncMock(return_value={"approved": True})

    svc = _build_add_service(radarr=radarr)
    release = _make_result(
        guid="g-push",
        download_url="magnet:?xt=urn:btih:abcdef0123456789abcdef0123456789abcdef01",
        detected_type=ContentType.MOVIE,
    )

    with structlog.testing.capture_logs() as logs:
        await svc.grab_movie_release(
            movie=movie, release=release, quality_profile_id=1, root_folder_path="/movies",
        )

    completed = [e for e in logs if e.get("event") == "grab_completed"]
    assert len(completed) == 1
    assert completed[0]["success"] is True
    assert completed[0]["path"] == "push"
    assert completed[0]["force_download"] is False
    assert completed[0]["content_type"] == "movie"


@pytest.mark.asyncio
async def test_grab_completed_logged_on_rejected_no_fallback():
    import structlog.testing

    movie = _make_movie(radarr_id=42)
    radarr = AsyncMock()
    radarr.get_movie_by_tmdb = AsyncMock(return_value=movie)
    radarr.push_release = AsyncMock(
        return_value={"approved": False, "rejections": ["Quality not wanted"]}
    )

    svc = _build_add_service(radarr=radarr)  # no qBittorrent → rejected terminal
    release = _make_result(
        guid="g-rej",
        download_url="magnet:?xt=urn:btih:abcdef0123456789abcdef0123456789abcdef01",
        detected_type=ContentType.MOVIE,
    )

    with structlog.testing.capture_logs() as logs:
        await svc.grab_movie_release(
            movie=movie, release=release, quality_profile_id=1, root_folder_path="/movies",
        )

    completed = [e for e in logs if e.get("event") == "grab_completed"]
    assert len(completed) == 1
    assert completed[0]["success"] is False
    assert completed[0]["path"] == "rejected"
    assert completed[0]["rejections"] == ["Quality not wanted"]


@pytest.mark.asyncio
async def test_grab_completed_logged_on_auto_search_fallback():
    import structlog.testing

    series = _make_series()
    series = series.model_copy(update={"sonarr_id": 77})
    sonarr = AsyncMock()
    sonarr.get_series_by_tvdb = AsyncMock(return_value=series)
    sonarr.push_release = AsyncMock(side_effect=Exception("unused"))
    sonarr.search_series = AsyncMock()

    svc = _build_add_service(sonarr=sonarr)
    release = _make_result(guid="g-auto")  # no download_url → skips straight to auto-search

    with structlog.testing.capture_logs() as logs:
        await svc.grab_series_release(
            series=series, release=release, quality_profile_id=1, root_folder_path="/tv",
        )

    completed = [e for e in logs if e.get("event") == "grab_completed"]
    assert len(completed) == 1
    assert completed[0]["success"] is True
    assert completed[0]["path"] == "auto_search"
    assert completed[0]["content_type"] == "series"


@pytest.mark.asyncio
async def test_grab_completed_carries_force_download_flag():
    import structlog.testing

    movie = _make_movie(radarr_id=42)
    radarr = AsyncMock()
    radarr.get_movie_by_tmdb = AsyncMock(return_value=movie)
    radarr.push_release = AsyncMock(
        return_value={"approved": False, "rejections": ["quality not allowed"]}
    )
    qbt = AsyncMock()
    qbt.add_torrent_url = AsyncMock(return_value=True)

    svc = _build_add_service(radarr=radarr, qbt=qbt)
    release = _make_result(
        guid="g-force",
        download_url="magnet:?xt=urn:btih:abcdef0123456789abcdef0123456789abcdef01",
        detected_type=ContentType.MOVIE,
    )

    with structlog.testing.capture_logs() as logs:
        await svc.grab_movie_release(
            movie=movie, release=release, quality_profile_id=1,
            root_folder_path="/movies", force_download=True,
        )

    completed = [e for e in logs if e.get("event") == "grab_completed"]
    assert len(completed) == 1
    assert completed[0]["path"] == "qbit"
    assert completed[0]["force_download"] is True


# ============================================================================
# LOGIC-10c: AddService.prowlarr is accepted but not stored
# ============================================================================


def test_add_service_does_not_store_prowlarr():
    """LOGIC-10c: prowlarr is accepted for backward-compat with all existing
    call sites but is dead weight — nothing in AddService reads it (grabs go
    through radarr/sonarr/lidarr/qbittorrent). It must not be stashed on the
    instance, and passing None for it must not raise."""
    svc = AddService(None, AsyncMock(), AsyncMock())
    assert not hasattr(svc, "prowlarr")

    svc2 = AddService(AsyncMock(), AsyncMock(), AsyncMock())
    assert not hasattr(svc2, "prowlarr")
