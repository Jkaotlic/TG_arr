"""Round-4 C4-services cluster tests.

Covers:
- OBS-02: BaseAPIClient._post_no_retry slow-call instrumentation.
- OBS-03: add_service grab_* persists rejection reasons into action.details (JSON).
- DEAD-13: search_releases top_preview drops the dead hasattr(get_size_gb) guard.
"""

import json
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from bot.clients.base import BaseAPIClient
from bot.models import (
    ArtistInfo,
    ContentType,
    MovieInfo,
    SearchResult,
    SeriesInfo,
)
from bot.services.add_service import AddService
from bot.services.search_service import SearchService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_PUBLIC_MAGNET = "magnet:?xt=urn:btih:abcdef0123456789abcdef0123456789abcdef01"


def _build_add_service(radarr=None, sonarr=None, lidarr=None, qbt=None) -> AddService:
    return AddService(
        prowlarr=AsyncMock(),
        radarr=radarr or AsyncMock(),
        sonarr=sonarr or AsyncMock(),
        qbittorrent=qbt,
        lidarr=lidarr,
    )


def _rejected_release(url: str = _PUBLIC_MAGNET) -> SearchResult:
    # indexer_id=0 so the direct-grab branch is skipped, magnet URL is "public"
    # so push_release is reached; with no qBittorrent fallback we land on the
    # rejected-return branch where details must be populated.
    return SearchResult(
        guid="rej-guid",
        indexer="TestIndexer",
        indexer_id=0,
        title="Fake.Release.2024.1080p",
        size=1_000_000,
        seeders=1,
        leechers=0,
        protocol="torrent",
        magnet_url=url,
        download_url=url,
        publish_date=datetime(2024, 1, 1),
        detected_type=ContentType.MOVIE,
    )


# ---------------------------------------------------------------------------
# OBS-02: _post_no_retry slow-call instrumentation
# ---------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, status_code: int = 200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"ok": True}
        self.text = "{}"

    def json(self):
        return self._payload


@pytest.mark.asyncio
async def test_post_no_retry_warns_on_slow_call(monkeypatch):
    """OBS-02: a >2000ms _post_no_retry must emit a slow_api_call WARNING with
    elapsed_ms, mirroring _request."""
    client = BaseAPIClient(base_url="http://svc", api_key="k", service_name="Radarr")

    # Stub the httpx client so request() returns instantly.
    fake_http = AsyncMock()
    fake_http.request = AsyncMock(return_value=_FakeResponse())

    async def _get_client():
        return fake_http

    monkeypatch.setattr(client, "_get_client", _get_client)

    # Force the monotonic clock to advance 3s across the single timed call so
    # the elapsed computation crosses the 2000ms threshold deterministically.
    ticks = iter([100.0, 103.0])

    def _fake_monotonic():
        try:
            return next(ticks)
        except StopIteration:
            return 103.0

    monkeypatch.setattr("bot.clients.base.time.monotonic", _fake_monotonic)

    warnings: list[tuple[str, dict]] = []

    class _Log:
        def bind(self, **kw):
            return self

        def debug(self, *a, **kw):
            pass

        def info(self, *a, **kw):
            pass

        def warning(self, event, *a, **kw):
            warnings.append((event, kw))

    monkeypatch.setattr("bot.clients.base.logger", _Log())

    result = await client._post_no_retry("/api/v3/release/push", json_data={"x": 1})
    assert result == {"ok": True}

    slow = [w for w in warnings if w[0] == "slow_api_call"]
    assert slow, f"expected slow_api_call warning, got {warnings}"
    assert slow[0][1].get("elapsed_ms") is not None
    assert slow[0][1]["elapsed_ms"] >= 2000


@pytest.mark.asyncio
async def test_post_no_retry_no_warn_when_fast(monkeypatch):
    """OBS-02: a fast _post_no_retry must NOT emit slow_api_call."""
    client = BaseAPIClient(base_url="http://svc", api_key="k", service_name="Radarr")

    fake_http = AsyncMock()
    fake_http.request = AsyncMock(return_value=_FakeResponse())

    async def _get_client():
        return fake_http

    monkeypatch.setattr(client, "_get_client", _get_client)

    ticks = iter([100.0, 100.05])  # 50ms

    def _fake_monotonic():
        try:
            return next(ticks)
        except StopIteration:
            return 100.05

    monkeypatch.setattr("bot.clients.base.time.monotonic", _fake_monotonic)

    warnings: list[str] = []

    class _Log:
        def bind(self, **kw):
            return self

        def debug(self, *a, **kw):
            pass

        def info(self, *a, **kw):
            pass

        def warning(self, event, *a, **kw):
            warnings.append(event)

    monkeypatch.setattr("bot.clients.base.logger", _Log())

    await client._post_no_retry("/api/v3/release/push", json_data={"x": 1})
    assert "slow_api_call" not in warnings


# ---------------------------------------------------------------------------
# OBS-03: rejection reasons persisted into action.details
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_grab_movie_persists_rejections_into_details():
    """OBS-03: when Radarr rejects a movie release, action.details must contain
    the rejection reasons as JSON for forensics."""
    movie = MovieInfo(tmdb_id=123, imdb_id="tt1", title="Test Movie", year=2024, radarr_id=42)

    rejections = ["Quality not wanted", "Release rejected by profile"]
    radarr = AsyncMock()
    radarr.get_movie_by_tmdb = AsyncMock(return_value=movie)
    radarr.push_release = AsyncMock(return_value={"approved": False, "rejections": rejections})
    radarr.grab_release = AsyncMock()
    radarr.search_movie = AsyncMock()

    svc = _build_add_service(radarr=radarr)  # no qBittorrent → rejected-return branch
    release = _rejected_release()

    success, action, _msg = await svc.grab_movie_release(
        movie=movie, release=release, quality_profile_id=1, root_folder_path="/movies",
    )

    assert success is False
    assert action.details is not None
    parsed = json.loads(action.details)
    assert parsed["rejections"] == rejections


@pytest.mark.asyncio
async def test_grab_series_persists_rejections_into_details():
    """OBS-03: Sonarr rejection reasons land in action.details."""
    series = SeriesInfo(tvdb_id=654, imdb_id="tt7", title="Test Series", year=2020, sonarr_id=77)

    rejections = ["Episode already downloaded"]
    sonarr = AsyncMock()
    sonarr.get_series_by_tvdb = AsyncMock(return_value=series)
    sonarr.push_release = AsyncMock(return_value={"approved": False, "rejections": rejections})
    sonarr.grab_release = AsyncMock()
    sonarr.search_series = AsyncMock()

    svc = _build_add_service(sonarr=sonarr)
    release = _rejected_release()

    success, action, _msg = await svc.grab_series_release(
        series=series, release=release, quality_profile_id=1, root_folder_path="/tv",
    )

    assert success is False
    assert action.details is not None
    parsed = json.loads(action.details)
    assert parsed["rejections"] == rejections


@pytest.mark.asyncio
async def test_grab_music_persists_rejections_into_details():
    """OBS-03: Lidarr rejection reasons land in action.details."""
    artist = ArtistInfo(
        mb_id="9c9f1380-2516-4fc9-a3e6-f9f61941d090", name="Test Artist", lidarr_id=11,
    )

    rejections = ["Not an album release"]
    lidarr = AsyncMock()
    lidarr.get_artist_by_mbid = AsyncMock(return_value=artist)
    lidarr.push_release = AsyncMock(return_value={"approved": False, "rejections": rejections})
    lidarr.grab_release = AsyncMock()
    lidarr.search_artist = AsyncMock()

    svc = _build_add_service(lidarr=lidarr)
    release = _rejected_release()

    success, action, _msg = await svc.grab_music_release(
        artist=artist,
        release=release,
        quality_profile_id=1,
        metadata_profile_id=1,
        root_folder_path="/music",
    )

    assert success is False
    assert action.details is not None
    parsed = json.loads(action.details)
    assert parsed["rejections"] == rejections


# ---------------------------------------------------------------------------
# DEAD-13: top_preview size_gb is always computed (no hasattr guard)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_search_releases_top_preview_includes_size_gb():
    """DEAD-13: size_gb in the search_completed top-preview is computed directly
    from SearchResult.get_size_gb() (no dead hasattr branch)."""
    prowlarr = AsyncMock()
    radarr = AsyncMock()
    sonarr = AsyncMock()

    result = SearchResult(
        guid="g-1",
        indexer="Idx",
        title="Some.Movie.2024.1080p",
        size=2 * 1024 ** 3,  # 2 GiB
        seeders=10,
        protocol="torrent",
        detected_type=ContentType.MOVIE,
    )
    prowlarr.search = AsyncMock(return_value=[result])

    captured: dict = {}

    class _Log:
        def bind(self, **kw):
            return self

        def info(self, event, *a, **kw):
            if event == "search_completed":
                captured.update(kw)

        def warning(self, *a, **kw):
            pass

    import bot.services.search_service as ss

    orig_logger = ss.logger
    ss.logger = _Log()
    try:
        svc = SearchService(prowlarr, radarr, sonarr)
        results = await svc.search_releases("some movie", ContentType.MOVIE, sort_by_score=False)
    finally:
        ss.logger = orig_logger

    assert len(results) == 1
    top = captured.get("top")
    assert top, "expected top preview in search_completed log"
    assert top[0]["size_gb"] == pytest.approx(2.0)
