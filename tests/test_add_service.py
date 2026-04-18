"""Tests for add-service / grab flow monitor-type selection (BUG-32) and SSRF (SEC-16)."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.models import (
    ArtistInfo,
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
async def test_grab_single_episode_sets_monitor_existing():
    """BUG-32: single-episode grab must pass monitor_type='existing', not 'none'."""
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

    assert captured.get("monitor_type") == "existing"


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


def _build_add_service(radarr=None, sonarr=None, lidarr=None, qbt=None):
    return AddService(
        prowlarr=AsyncMock(),
        radarr=radarr or AsyncMock(),
        sonarr=sonarr or AsyncMock(),
        qbittorrent=qbt,
        lidarr=lidarr,
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
async def test_push_release_rejects_private_url_music():
    """SEC-16: grab_music_release must NOT call lidarr.push_release for a private URL."""
    artist = ArtistInfo(
        mb_id="9c9f1380-2516-4fc9-a3e6-f9f61941d090",
        name="Test Artist",
        lidarr_id=11,
    )

    lidarr = AsyncMock()
    lidarr.get_artist_by_mbid = AsyncMock(return_value=artist)
    lidarr.push_release = AsyncMock()
    lidarr.search_artist = AsyncMock()

    svc = _build_add_service(lidarr=lidarr)

    release = _private_release("http://10.0.0.5/album.torrent")
    await svc.grab_music_release(
        artist=artist,
        release=release,
        quality_profile_id=1,
        metadata_profile_id=1,
        root_folder_path="/music",
    )

    lidarr.push_release.assert_not_called()


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
