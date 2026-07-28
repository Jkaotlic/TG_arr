"""Round-4 C4-services cluster tests.

Covers:
- OBS-02: BaseAPIClient._post_no_retry slow-call instrumentation.
- OBS-03: add_service grab_* persists rejection reasons into action.details (JSON).
- DEAD-13: search_releases top_preview drops the dead hasattr(get_size_gb) guard.
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.clients.base import BaseAPIClient
from bot.models import (
    ContentType,
    MovieInfo,
    SearchResult,
    SeriesInfo,
)
from bot.services.search_service import SearchService
from tests.conftest import build_add_service as _build_add_service


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_PUBLIC_MAGNET = "magnet:?xt=urn:btih:abcdef0123456789abcdef0123456789abcdef01"


def _rejected_release(url: str = _PUBLIC_MAGNET) -> SearchResult:
    # Carries a candidate token so the queue call is actually reached — the
    # refusal under test comes from Scryer, not from a missing token.
    return SearchResult(
        candidate_token="cand-1",
        scryer_title_id="t1",
        queue_scope={"title": True},
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
async def test_blocked_release_records_the_reason_in_the_action():
    """OBS-03 (migrated): the *arr "rejections" array is gone; Scryer refuses a
    candidate with a queue status (or a GraphQL error). Either way the reason
    must survive into the ActionLog for forensics."""
    from bot.clients.scryer import ScryerGraphQLError

    scryer = AsyncMock()
    scryer.queue_existing_title_download = AsyncMock(
        side_effect=ScryerGraphQLError("Scryer вернул ошибку: release blocked by profile")
    )
    svc = _build_add_service(scryer=scryer)

    success, action, _msg = await svc.grab_release(
        MovieInfo(tmdb_id=123, title="Test Movie", year=2024, scryer_id="t1"),
        _rejected_release(),
        ContentType.MOVIE,
    )

    assert success is False
    assert action.error_message
    assert "release blocked by profile" in action.error_message


@pytest.mark.asyncio
async def test_queue_conflict_records_the_status_in_the_action():
    scryer = AsyncMock()
    scryer.queue_existing_title_download = AsyncMock(
        return_value=MagicMock(queued=False, status="CONFLICT", job_id=None)
    )
    svc = _build_add_service(scryer=scryer)

    success, action, _msg = await svc.grab_release(
        SeriesInfo(tvdb_id=654, title="Test Series", year=2020, scryer_id="t1"),
        _rejected_release(),
        ContentType.SERIES,
    )

    assert success is False
    assert "CONFLICT" in (action.error_message or "")


@pytest.mark.asyncio
async def test_search_releases_top_preview_includes_size_gb():
    """DEAD-13: size_gb in the search_completed top-preview is computed directly
    from SearchResult.get_size_gb() (no dead hasattr branch)."""
    scryer = AsyncMock()

    result = SearchResult(
        guid="g-1",
        indexer="Idx",
        title="Some.Movie.2024.1080p",
        size=2 * 1024 ** 3,  # 2 GiB
        seeders=10,
        protocol="torrent",
        detected_type=ContentType.MOVIE,
    )
    scryer.search_releases = AsyncMock(return_value=[result])

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
        svc = SearchService(scryer)
        results = await svc.search_releases("title-id", ContentType.MOVIE)
    finally:
        ss.logger = orig_logger

    assert len(results) == 1
    top = captured.get("top")
    assert top, "expected top preview in search_completed log"
    assert top[0]["size_gb"] == pytest.approx(2.0)
