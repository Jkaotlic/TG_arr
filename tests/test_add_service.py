"""Tests for AddService (Scryer edition).

Rewritten for the 2026-07-28 migration. The push-release/auto-search flow this
file used to cover no longer exists — Scryer queues a *candidate token* instead
— but the invariants that mattered are kept and re-pointed at the new paths:

- the SSRF guard still gates every URL handed to qBittorrent (force-download),
- download URLs are still masked before they reach the logs,
- every grab still emits exactly one terminal `grab_completed` event,
- `ensure_title` must not duplicate titles or silently start monitoring,
- Lidarr artist adds are unchanged.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.clients.scryer import ScryerGraphQLError
from bot.models import (
    ActionType,
    ArtistInfo,
    ContentType,
    MovieInfo,
    QualityInfo,
    SearchResult,
    SeriesInfo,
)
from bot.services.add_service import AddService, _mask_url, _validate_download_url


def _release(**overrides) -> SearchResult:
    data = dict(
        guid="g1",
        title="Movie.2024.2160p.WEB-DL",
        indexer="RuTracker",
        size=8 * 1024**3,
        seeders=42,
        quality=QualityInfo(resolution="2160p", source="WEB-DL"),
        candidate_token="cand-token",
        scryer_title_id="t1",
        queue_scope={"title": True},
        scryer_allowed=True,
        scryer_score=2680,
    )
    data.update(overrides)
    return SearchResult(**data)


def _movie(**overrides) -> MovieInfo:
    data = dict(title="Apex", year=2026, scryer_id="t1", metadata_id="358476")
    data.update(overrides)
    return MovieInfo(**data)


def _service(scryer=None, qbt=None, lidarr=None) -> AddService:
    return AddService(scryer or AsyncMock(), qbittorrent=qbt, lidarr=lidarr)


def _queued(status="QUEUED", job_id="job-1"):
    return MagicMock(queued=status == "QUEUED", status=status, job_id=job_id)


# ---------------------------------------------------------------- ensure_title
@pytest.mark.asyncio
async def test_ensure_title_short_circuits_on_a_known_id():
    scryer = AsyncMock()
    service = _service(scryer)

    title, created = await service.ensure_title(_movie(scryer_id="known"), ContentType.MOVIE)

    assert title.scryer_id == "known"
    assert created is False
    scryer.find_title.assert_not_awaited()
    scryer.add_title.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_title_finds_before_adding():
    scryer = AsyncMock()
    scryer.find_title = AsyncMock(return_value=_movie(scryer_id="found"))
    service = _service(scryer)

    title, created = await service.ensure_title(
        MovieInfo(title="Apex", year=2026, metadata_id="358476"), ContentType.MOVIE
    )

    assert title.scryer_id == "found"
    assert created is False
    scryer.add_title.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_title_adds_anime_with_the_anime_facet():
    scryer = AsyncMock()
    scryer.find_title = AsyncMock(return_value=None)
    added = SeriesInfo(title="Frieren", year=2023, scryer_id="new", facet="ANIME")
    scryer.add_title = AsyncMock(return_value=MagicMock(title=added, reused_existing=False))
    service = _service(scryer)

    title, created = await service.ensure_title(
        SeriesInfo(title="Frieren", year=2023, facet="ANIME", metadata_id="424536"),
        ContentType.ANIME,
    )

    assert created is True
    assert title.facet == "ANIME"
    assert scryer.add_title.await_args.args[1] == ContentType.ANIME


# ----------------------------------------------------------------------- grab
@pytest.mark.asyncio
async def test_grab_queues_candidate_then_enables_monitoring():
    scryer = AsyncMock()
    scryer.queue_existing_title_download = AsyncMock(return_value=_queued())
    service = _service(scryer)

    ok, action, _msg = await service.grab_release(_movie(), _release(), ContentType.MOVIE)

    assert ok is True
    assert action.success is True
    assert action.action_type == ActionType.GRAB
    scryer.set_title_monitored.assert_awaited_once_with("t1", True)


@pytest.mark.asyncio
async def test_grab_does_not_enable_monitoring_when_the_queue_is_refused():
    """Monitoring is the commitment step — it must not happen if nothing queued."""
    scryer = AsyncMock()
    scryer.queue_existing_title_download = AsyncMock(return_value=_queued(status="CONFLICT", job_id=None))
    service = _service(scryer)

    ok, action, msg = await service.grab_release(_movie(), _release(), ContentType.MOVIE)

    assert ok is False
    assert action.success is False
    scryer.set_title_monitored.assert_not_awaited()
    assert msg


@pytest.mark.asyncio
async def test_grab_survives_a_failing_monitor_toggle():
    """The download is already queued — a failed monitor flip must not undo it."""
    scryer = AsyncMock()
    scryer.queue_existing_title_download = AsyncMock(return_value=_queued())
    scryer.set_title_monitored = AsyncMock(side_effect=RuntimeError("nope"))
    service = _service(scryer)

    ok, action, _msg = await service.grab_release(_movie(), _release(), ContentType.MOVIE)

    assert ok is True
    assert action.success is True


@pytest.mark.asyncio
async def test_grab_reports_a_scryer_error_without_raising():
    scryer = AsyncMock()
    scryer.queue_existing_title_download = AsyncMock(
        side_effect=ScryerGraphQLError("Scryer вернул ошибку: candidate token expired")
    )
    service = _service(scryer)

    ok, action, msg = await service.grab_release(_movie(), _release(), ContentType.MOVIE)

    assert ok is False
    assert action.success is False
    assert "candidate token expired" in msg


@pytest.mark.asyncio
async def test_grab_passes_the_episode_scope_through():
    """A single-episode candidate must queue that episode, not the whole show."""
    scryer = AsyncMock()
    scryer.queue_existing_title_download = AsyncMock(return_value=_queued())
    service = _service(scryer)

    release = _release(queue_scope={"episode": "ep-7"})
    await service.grab_release(
        SeriesInfo(title="Show", year=2024, scryer_id="t1"), release, ContentType.SERIES
    )

    assert scryer.queue_existing_title_download.await_args.kwargs["scope"] == {"episode": "ep-7"}


@pytest.mark.asyncio
async def test_grab_without_a_title_id_fails_cleanly():
    scryer = AsyncMock()
    service = _service(scryer)

    ok, _action, msg = await service.grab_release(
        MovieInfo(title="Apex", year=2026), _release(scryer_title_id=None), ContentType.MOVIE
    )

    assert ok is False
    scryer.queue_existing_title_download.assert_not_awaited()
    assert msg


# -------------------------------------------------------------- force download
@pytest.mark.asyncio
async def test_force_download_rejects_a_private_url():
    """SEC-16: the SSRF guard still gates everything handed to qBittorrent."""
    qbt = AsyncMock()
    service = _service(qbt=qbt)

    ok, _action, msg = await service.grab_release(
        _movie(),
        _release(download_url="http://192.168.1.50:8080/admin"),
        ContentType.MOVIE,
        force_download=True,
    )

    assert ok is False
    qbt.add_torrent_url.assert_not_awaited()
    assert "Небезопасный" in msg


@pytest.mark.asyncio
async def test_force_download_allows_a_magnet_uri():
    qbt = AsyncMock()
    qbt.add_torrent_url = AsyncMock(return_value=True)
    service = _service(qbt=qbt)

    ok, action, _msg = await service.grab_release(
        _movie(),
        _release(download_url=None, magnet_url="magnet:?xt=urn:btih:abcdef0123456789"),
        ContentType.MOVIE,
        force_download=True,
    )

    assert ok is True
    assert action.success is True
    qbt.add_torrent_url.assert_awaited_once()


@pytest.mark.asyncio
async def test_force_download_uses_the_anime_category():
    qbt = AsyncMock()
    qbt.add_torrent_url = AsyncMock(return_value=True)
    service = _service(qbt=qbt)

    await service.grab_release(
        SeriesInfo(title="Frieren", year=2023, scryer_id="t1", facet="ANIME"),
        _release(download_url="magnet:?xt=urn:btih:abcdef0123456789"),
        ContentType.ANIME,
        force_download=True,
    )

    assert qbt.add_torrent_url.await_args.kwargs["category"] == "anime"


@pytest.mark.asyncio
async def test_force_download_without_qbittorrent_reports_config_error():
    service = _service(qbt=None)

    ok, _action, msg = await service.grab_release(
        _movie(), _release(), ContentType.MOVIE, force_download=True
    )

    assert ok is False
    assert "qBittorrent" in msg


@pytest.mark.asyncio
async def test_qbittorrent_rejection_is_reported_as_failure():
    qbt = AsyncMock()
    qbt.add_torrent_url = AsyncMock(return_value=False)
    service = _service(qbt=qbt)

    ok, action, msg = await service.grab_release(
        _movie(),
        _release(download_url="magnet:?xt=urn:btih:abcdef0123456789"),
        ContentType.MOVIE,
        force_download=True,
    )

    assert ok is False
    assert action.success is False
    assert msg


# ------------------------------------------------------------------ auto grab
@pytest.mark.asyncio
async def test_add_and_queue_best_reports_success_even_without_a_match():
    """Scryer keeps hunting via its own wanted/RSS cycle — "added, nothing yet"
    is a success for the user, not an error."""
    scryer = AsyncMock()
    outcome = MagicMock(queued=False, reused_existing=False)
    outcome.title = _movie()
    scryer.add_title_and_queue_download = AsyncMock(return_value=outcome)
    service = _service(scryer)

    ok, action, msg = await service.add_and_queue_best(_movie(), ContentType.MOVIE)

    assert ok is True
    assert action.success is True
    assert "продолжит искать" in msg


@pytest.mark.asyncio
async def test_add_and_queue_best_surfaces_a_scryer_error():
    scryer = AsyncMock()
    scryer.add_title_and_queue_download = AsyncMock(
        side_effect=ScryerGraphQLError("Scryer вернул ошибку: unknown library")
    )
    service = _service(scryer)

    ok, action, msg = await service.add_and_queue_best(_movie(), ContentType.MOVIE)

    assert ok is False
    assert action.success is False
    assert "unknown library" in msg


# -------------------------------------------------------------- observability
@pytest.mark.asyncio
async def test_grab_completed_logged_exactly_once_on_success():
    scryer = AsyncMock()
    scryer.queue_existing_title_download = AsyncMock(return_value=_queued())
    service = _service(scryer)

    with patch("bot.services.add_service._log_grab_completed") as log_mock:
        await service.grab_release(_movie(), _release(), ContentType.MOVIE)

    log_mock.assert_called_once()
    assert log_mock.call_args.kwargs["success"] is True
    assert log_mock.call_args.kwargs["path"] == "queue"


@pytest.mark.asyncio
async def test_grab_completed_logged_on_a_blocked_queue():
    scryer = AsyncMock()
    scryer.queue_existing_title_download = AsyncMock(return_value=_queued(status="CONFLICT", job_id=None))
    service = _service(scryer)

    with patch("bot.services.add_service._log_grab_completed") as log_mock:
        await service.grab_release(_movie(), _release(), ContentType.MOVIE)

    log_mock.assert_called_once()
    assert log_mock.call_args.kwargs["success"] is False
    assert log_mock.call_args.kwargs["path"] == "blocked"


@pytest.mark.asyncio
async def test_grab_completed_carries_the_force_download_flag():
    qbt = AsyncMock()
    qbt.add_torrent_url = AsyncMock(return_value=True)
    service = _service(qbt=qbt)

    with patch("bot.services.add_service._log_grab_completed") as log_mock:
        await service.grab_release(
            _movie(),
            _release(download_url="magnet:?xt=urn:btih:abcdef0123456789"),
            ContentType.MOVIE,
            force_download=True,
        )

    assert log_mock.call_args.kwargs["force_download"] is True
    assert log_mock.call_args.kwargs["path"] == "qbit"


# ---------------------------------------------------------------- url masking
def test_mask_url_hides_apikey_and_passkey_path_segment():
    masked = _mask_url(
        "https://tracker.example/download/123/0123456789abcdef0123456789abcdef/x.torrent?apikey=SECRET"
    )
    assert "SECRET" not in masked
    assert "0123456789abcdef0123456789abcdef" not in masked
    assert "tracker.example" in masked


@pytest.mark.asyncio
async def test_validate_download_url_rejects_unknown_schemes():
    assert await _validate_download_url("file:///etc/passwd") is False
    assert await _validate_download_url("") is False


@pytest.mark.asyncio
async def test_validate_download_url_trusts_a_configured_service_host(monkeypatch):
    """SEC-01: Scryer hands back URLs pointing at Prowlarr's proxy on the LAN;
    the configured service host:port pair stays trusted, other ports don't."""
    monkeypatch.setenv("SCRYER_URL", "http://192.168.0.95:8088")
    from bot.config import get_settings

    get_settings.cache_clear()
    try:
        assert await _validate_download_url("http://192.168.0.95:8088/download?x=1") is True
        assert await _validate_download_url("http://192.168.0.95:22/") is False
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------- music
@pytest.mark.asyncio
async def test_add_artist_without_lidarr_returns_error():
    service = _service(lidarr=None)

    added, action = await service.add_artist(
        ArtistInfo(mb_id="mb-1", name="Metallica"), 1, 1, "/music"
    )

    assert added is None
    assert action.success is False


@pytest.mark.asyncio
async def test_add_artist_reuses_an_existing_lidarr_artist():
    lidarr = AsyncMock()
    lidarr.get_artist_by_mbid = AsyncMock(
        return_value=ArtistInfo(mb_id="mb-1", name="Metallica", lidarr_id=7)
    )
    service = _service(lidarr=lidarr)

    added, action = await service.add_artist(
        ArtistInfo(mb_id="mb-1", name="Metallica"), 1, 1, "/music"
    )

    assert added.lidarr_id == 7
    assert action.success is True
    lidarr.add_artist.assert_not_awaited()


# --------------------------------------------------------------- profiles etc
def test_resolve_profile_matches_a_string_id():
    """Scryer profile ids are slugs; a stored preference may be either type."""
    from bot.models import QualityProfile

    profiles = [QualityProfile(id="1080p", name="1080P"), QualityProfile(id="4k", name="4K")]
    assert AddService.resolve_profile(profiles, "4k").name == "4K"
    assert AddService.resolve_profile(profiles, None).name == "1080P"
    assert AddService.resolve_profile(profiles, "missing").name == "1080P"


def test_resolve_root_folder_prefers_the_default_when_unset():
    from bot.models import RootFolder

    folders = [
        RootFolder(id="H:\\radarr", path="H:\\radarr", is_default=False),
        RootFolder(id="G:\\radarr", path="G:\\radarr", is_default=True),
    ]
    assert AddService.resolve_root_folder(folders, None) == "G:\\radarr"
    assert AddService.resolve_root_folder(folders, "H:\\radarr") == "H:\\radarr"


def test_resolve_root_folder_raises_without_folders():
    with pytest.raises(ValueError):
        AddService.resolve_root_folder([], None)
