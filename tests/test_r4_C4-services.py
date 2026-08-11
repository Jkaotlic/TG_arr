"""Round-4 C4-services cluster tests.

Covers:
- OBS-02: BaseAPIClient._post_no_retry slow-call instrumentation.
- OBS-03: add_service grab_* persists rejection reasons into action.details (JSON).
- DEAD-13: search_releases top_preview drops the dead hasattr(get_size_gb) guard.
"""

from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from bot.clients.base import BaseAPIClient
from bot.models import (
    ContentType,
    SearchResult,
)
from bot.services.search_service import SearchService
from tests.conftest import build_add_service as _build_add_service


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_PUBLIC_MAGNET = "magnet:?xt=urn:btih:abcdef0123456789abcdef0123456789abcdef01"


def _rejected_release(url: str = _PUBLIC_MAGNET) -> SearchResult:
    """A release *arr will refuse, shaped like a Prowlarr free-text hit.

    `indexer_id=0` plus the default `origin="prowlarr"` keeps it off the native
    grab path, so the refusal under test comes from `push_release`'s verdict.
    """
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
async def test_blocked_release_records_the_reason_in_the_action():
    """OBS-03 (migrated): the *arr "rejections" array is gone; Scryer refuses a
    candidate with a queue status (or a GraphQL error). Either way the reason
    must survive into the ActionLog for forensics."""
    radarr = AsyncMock()
    radarr.push_release = AsyncMock(
        return_value={"approved": False, "rejections": ["release blocked by profile"]}
    )
    svc = _build_add_service(radarr=radarr)

    success, action = await svc.grab_release(
        _rejected_release(), ContentType.MOVIE, arr_id=15,
    )

    assert success is False
    assert action.error_message
    assert "release blocked by profile" in action.error_message
    # OBS-03: the structured reasons must also survive for history forensics.
    assert "release blocked by profile" in (action.details or "")


@pytest.mark.asyncio
async def test_queue_conflict_records_the_status_in_the_action():
    """A refusal with no stated reason must still read as a refusal, not silence."""
    sonarr = AsyncMock()
    sonarr.push_release = AsyncMock(return_value={"approved": False, "rejections": []})
    svc = _build_add_service(sonarr=sonarr)

    success, action = await svc.grab_release(
        _rejected_release(), ContentType.SERIES, arr_id=3,
    )

    assert success is False
    assert action.error_message == "Отклонено"


@pytest.mark.asyncio
async def test_search_releases_top_preview_includes_size_gb():
    """DEAD-13: size_gb in the search_completed top-preview is computed directly
    from SearchResult.get_size_gb() (no dead hasattr branch)."""
    radarr = AsyncMock()

    result = SearchResult(
        guid="g-1",
        indexer="Idx",
        title="Some.Movie.2024.1080p",
        size=2 * 1024 ** 3,  # 2 GiB
        seeders=10,
        protocol="torrent",
        detected_type=ContentType.MOVIE,
        origin="arr",
    )
    radarr.get_releases = AsyncMock(return_value=[result])

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
        svc = SearchService(radarr, AsyncMock())
        results = await svc.search_releases_for_title(ContentType.MOVIE, arr_id=15)
    finally:
        ss.logger = orig_logger

    assert len(results) == 1
    top = captured.get("top")
    assert top, "expected top preview in search_completed log"
    assert top[0]["size_gb"] == pytest.approx(2.0)
