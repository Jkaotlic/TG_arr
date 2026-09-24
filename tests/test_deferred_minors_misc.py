"""Round 2 / D3 (search/torrserver/breaker part): multi-episode releases in
the episode filter, double-tap guards on "📺 Весь сериал" and TorrServer add,
one half-open probe per breaker."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from bot.models import ContentType, SearchResult, SearchSession, SeriesInfo, User


# ------------------------------------------------------ episode parser
@pytest.mark.parametrize("title,expected", [
    ("Show.S01E04-E06.1080p.WEB-DL", [4, 5, 6]),
    ("Show.S01E04E05E06.1080p.WEB-DL", [4, 5, 6]),
    ("Show.S01E04-06.1080p.WEB-DL", [4, 5, 6]),
    ("Show.S01E04E05.720p", [4, 5]),
    ("Show.S01E05.720p.HDTV", [5]),
    ("Show.S01E05-720p.WEB-DL.x264", [5]),   # resolution after a dash
    ("Show.S01E05-4HD", [5]),                # release group
    ("Movie.2021.1080p", []),
])
def test_parse_episode_numbers(title, expected):
    from bot.services.release_parser import parse_episode_numbers

    assert parse_episode_numbers(title) == expected


def test_arr_release_fills_detected_episodes():
    from bot.clients.sonarr import SonarrClient

    client = SonarrClient("http://sonarr.example.com", "key")
    from_arr = client._parse_release({"guid": "g1", "title": "Show.S01E04E05.720p",
                                      "seasonNumber": 1, "episodeNumbers": [4, 5]})
    from_title = client._parse_release({"guid": "g2", "title": "Show.S01E04E05.720p", "seasonNumber": 1})
    assert from_arr is not None and from_arr.detected_episodes == [4, 5]
    assert from_title is not None and from_title.detected_episodes == [4, 5]


def test_prowlarr_result_fills_detected_episodes():
    from bot.clients.prowlarr import ProwlarrClient

    r = ProwlarrClient("http://prowlarr.example.com", "key")._normalize_result({
        "guid": "g", "title": "Show.S01E04E05E06.1080p.WEB-DL", "size": 1, "indexer": "idx",
    })
    assert r is not None and r.detected_episodes == [4, 5, 6]


def _rel(guid, *, episode=None, episodes=None, pack=False) -> SearchResult:
    return SearchResult(
        guid=guid, origin="arr", title=f"Show {guid}", size=1, detected_season=1,
        detected_episode=episode, detected_episodes=episodes or ([episode] if episode else []),
        is_season_pack=pack,
    )


@pytest.mark.asyncio
async def test_episode_filter_keeps_multi_episode_releases_that_contain_it():
    from bot.handlers import search

    results = [
        _rel("e5", episode=5),
        _rel("e4-6", episodes=[4, 5, 6]),
        _rel("e4e5", episodes=[4, 5]),
        _rel("e6e7", episodes=[6, 7]),
        _rel("e6", episode=6),
    ]
    search_service = MagicMock()
    search_service.parse_query = MagicMock(return_value={
        "title": "Show", "year": None, "season": 1, "episode": 5, "quality": None,
    })
    search_service.search_releases_for_title = AsyncMock(return_value=results)
    status_msg = MagicMock()
    status_msg.edit_text = AsyncMock()
    message = MagicMock()
    message.answer = AsyncMock(return_value=status_msg)
    db = MagicMock()
    db.save_session = AsyncMock()
    db.log_action = AsyncMock()
    show = SeriesInfo(tvdb_id=1, title="Show", sonarr_id=77,
                      seasons=[{"seasonNumber": n} for n in range(0, 4)])

    with patch.object(search, "get_services", AsyncMock(return_value=(search_service, MagicMock()))), \
            patch.object(search, "_render_results_page", AsyncMock()):
        await search.process_search(message, "Show S01E05", ContentType.SERIES, User(tg_id=5), db,
                                    chosen_title=show)

    saved = db.save_session.await_args.args[1]
    assert [r.guid for r in saved.results] == ["e5", "e4-6", "e4e5"]


# ------------------------------------------------ whole series double tap
@pytest.mark.asyncio
async def test_whole_series_double_tap_runs_one_series_search():
    from bot.handlers import search as search_mod
    from bot.handlers.search import results as results_mod
    from bot.ui.callbacks import SeasonScopeCB

    session = SearchSession(
        user_id=9, query="Show", content_type=ContentType.SERIES,
        selected_content=SeriesInfo(tvdb_id=1, title="Show", sonarr_id=77),
    )
    db = MagicMock()
    db.get_session = AsyncMock(return_value=session)

    async def slow_search(*_a, **_k):
        await asyncio.sleep(0.05)

    def _cb():
        cb = MagicMock()
        cb.from_user = MagicMock(id=9)
        cb.answer = AsyncMock()
        cb.message = MagicMock()
        cb.message.edit_reply_markup = AsyncMock()
        return cb

    first, second = _cb(), _cb()
    with patch.object(results_mod, "accessible_message", side_effect=lambda c: c.message), \
            patch.object(search_mod, "process_search", AsyncMock(side_effect=slow_search)) as spied:
        await asyncio.gather(
            results_mod.handle_season_scope(first, SeasonScopeCB(season=0, title_id="77"), User(tg_id=9), db),
            results_mod.handle_season_scope(second, SeasonScopeCB(season=0, title_id="77"), User(tg_id=9), db),
        )

    assert spied.await_count == 1
    assert "⏳" in second.answer.await_args.args[0]
    # the slot is released afterwards: a later tap works again
    from bot.handlers.search import _claim_grab, _release_grab
    assert await _claim_grab(9)
    _release_grab(9)


# --------------------------------------------------- TorrServer double tap
@pytest.mark.asyncio
async def test_torrserver_add_double_tap_adds_once():
    from bot.handlers import torrserver as ts
    from bot.models import TorrServerRelease
    from bot.ui.callbacks import TsAddCB

    ts._results[31] = [TorrServerRelease(title="Dune", link="magnet:?xt=urn:btih:" + "a" * 40)]
    tok = ts._tokens.issue(31)

    async def slow_add(*_a):
        await asyncio.sleep(0.05)
        raise ts.TorrServerError("stop here")

    service = MagicMock()
    service.add_and_publish = AsyncMock(side_effect=slow_add)

    def _cb():
        cb = MagicMock()
        cb.from_user = MagicMock(id=31)
        cb.answer = AsyncMock()
        cb.message = MagicMock()
        cb.message.edit_text = AsyncMock()
        return cb

    first, second = _cb(), _cb()
    try:
        with patch.object(ts, "get_torrserver_service", AsyncMock(return_value=service)), \
                patch.object(ts, "accessible_message", side_effect=lambda c: c.message):
            await asyncio.gather(
                ts.handle_add(first, TsAddCB(tok=tok, idx=0)),
                ts.handle_add(second, TsAddCB(tok=tok, idx=0)),
            )
    finally:
        ts._results.pop(31, None)

    assert service.add_and_publish.await_count == 1
    assert "⏳" in second.answer.await_args.args[0]
    from bot.handlers.search import _claim_grab, _release_grab
    assert await _claim_grab(31)  # released even though the add failed
    _release_grab(31)


# ------------------------------------------------------- breaker half-open
def test_breaker_half_open_lets_one_background_probe_through():
    from bot.clients.base import CircuitBreaker

    clock = MagicMock(return_value=1000.0)
    breaker = CircuitBreaker("Svc", clock=clock)
    breaker.record_failure()
    clock.return_value = 1031.0  # backoff (30 s) expired
    assert breaker.allow(background=True) is True   # the probe
    assert breaker.allow(background=True) is False  # everyone else fails fast
    assert breaker.allow(background=False) is True  # interactive still tries
    breaker.record_success()
    assert breaker.allow(background=True) is True
    assert breaker.allow(background=True) is True   # closed: no limit


@pytest.mark.asyncio
async def test_concurrent_background_calls_send_one_probe(monkeypatch):
    from bot.clients.base import BaseAPIClient, ServiceUnavailable, background_calls

    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    client = BaseAPIClient("http://svc.example.com", "key", "Svc")
    clock = MagicMock(return_value=1000.0)
    client.breaker._clock = clock
    client.breaker.record_failure("down")
    clock.return_value = 1031.0

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_refuse(*_a, **_k):
        started.set()
        await release.wait()
        raise httpx.ConnectError("refused")

    http = MagicMock()
    http.is_closed = False
    http.request = AsyncMock(side_effect=slow_refuse)
    client._client = http

    async def call():
        with background_calls():
            return await client.get("/x")

    probe = asyncio.create_task(call())
    await started.wait()
    try:
        # Without the half-open limit every caller goes to the network and
        # waits on ``release`` — the timeout turns that into a clean failure.
        others = await asyncio.wait_for(
            asyncio.gather(*(call() for _ in range(3)), return_exceptions=True), timeout=1.0,
        )
    finally:
        release.set()
    assert all(isinstance(e, ServiceUnavailable) for e in others)
    with pytest.raises(Exception):
        await probe
    assert http.request.await_count == 1


@pytest.mark.asyncio
async def test_probe_slot_is_freed_when_the_probe_dies_unexpectedly(monkeypatch):
    """A probe that raises something the breaker does not record (not a
    transport error, not an APIError) must not wedge the half-open slot."""
    from bot.clients.base import BaseAPIClient, background_calls

    client = BaseAPIClient("http://svc.example.com", "key", "Svc")
    clock = MagicMock(return_value=1000.0)
    client.breaker._clock = clock
    client.breaker.record_failure("down")
    clock.return_value = 1031.0

    http = MagicMock()
    http.is_closed = False
    http.request = AsyncMock(side_effect=RuntimeError("boom"))
    client._client = http

    with background_calls():
        with pytest.raises(RuntimeError):
            await client.get("/x")
    assert client.breaker.allow(background=True) is True


# ============================================================ fix round 1
@pytest.mark.parametrize("title,expected", [
    ("Show.S01E04 - 12 из 20 [WEB-DL]", [4]),   # spaced dash + bare number: not a range
    ("Show S01E04 - E06 1080p", [4, 5, 6]),     # spaced dash with E: a range
    ("Show.S01E04-06.1080p", [4, 5, 6]),        # tight dash: a range
])
def test_episode_range_needs_e_or_a_tight_dash(title, expected):
    from bot.services.release_parser import parse_episode_numbers

    assert parse_episode_numbers(title) == expected


def test_stale_probe_cannot_free_a_newer_probe_slot():
    """Probe A outlives a failure + backoff; probe B takes the slot; A ending
    must not free B's slot (per-probe token)."""
    from bot.clients.base import CircuitBreaker

    clock = MagicMock(return_value=1000.0)
    breaker = CircuitBreaker("Svc", clock=clock)
    breaker.record_failure()
    clock.return_value = 1031.0
    assert breaker.allow(background=True)          # probe A
    token_a = breaker.probe_token
    breaker.record_failure()                       # an interactive call failed meanwhile
    clock.return_value = 1100.0                    # backoff (60 s) over
    assert breaker.allow(background=True)          # probe B
    breaker.end_probe(token_a)                     # A finishes late
    assert breaker.allow(background=True) is False  # B still holds the slot
