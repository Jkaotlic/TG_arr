"""Tests for round-4 audit fixes (SEC-01/02/03, BUG-01, RACE-01, RACE-02/DB-01)."""

import asyncio
import html
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.models import (
    ContentType,
    MovieInfo,
    SearchResult,
    SearchSession,
    SeriesInfo,
    TorrentInfo,
    TorrentState,
    User,
)
from tests.conftest import callback_with_status as _callback_with_status


@pytest.fixture(autouse=True)
def _clear_trending_caches():
    """TEST-16: this module pokes bot.handlers.trending's module-level
    _trending_movies_cache/_trending_series_cache (and their TTL timestamp
    side-tables) directly in several tests. Without cleanup those entries
    leak into whichever test runs next, creating a latent test-order
    dependency. Clear before and after every test in this module."""
    from bot.handlers import trending

    def _clear():
        trending._trending_movies_cache.clear()
        trending._trending_series_cache.clear()
        trending._trending_movies_inserted_at.clear()
        trending._trending_series_inserted_at.clear()

    _clear()
    yield
    _clear()


# ---------------------------------------------------------------------------
# SEC-03: passkey in push-result logs must be stripped before logging
# ---------------------------------------------------------------------------
def test_push_result_never_carries_the_download_url_into_logs():
    """SEC-03: a release URL carries Prowlarr's apikey and the tracker passkey
    and must never reach the logs verbatim.

    Rollback 2026-08-10: the previous backend masked the URL in place
    (`mask_release_secrets`). The *arr push response is back, and it echoes the
    whole pushed release including `downloadUrl` — so the invariant is now held
    by dropping the field outright rather than redacting it. Same guarantee,
    stricter mechanism. The end-to-end log assertion lives in
    tests/test_add_service.py::test_push_release_response_is_never_logged_raw.
    """
    from bot.services.add_service import _safe_push_result

    safe = _safe_push_result({
        "approved": True,
        "title": "Movie 2021 2160p",
        "downloadUrl": "http://127.0.0.1:9696/2/download?apikey=6b7b4a9e4c7e&link=ZWVK",
    })

    assert "6b7b4a9e4c7e" not in str(safe)
    assert "ZWVK" not in str(safe)
    assert "downloadUrl" not in safe
    assert safe.get("approved") is True


def test_safe_push_result_handles_a_non_dict_response():
    """*arr can answer with an empty body — that must not raise.

    Characterizes current behaviour: the helper always returns the safe-field
    skeleton (`approved`/`rejections`), never the raw payload, so a caller can
    read `.get("approved")` unconditionally.
    """
    from bot.services.add_service import _safe_push_result

    for payload in (None, {}):
        safe = _safe_push_result(payload)
        assert "downloadUrl" not in safe
        assert safe.get("approved") is None
        assert safe.get("rejections") == []

_DANGEROUS_NAME = "Tom & Jerry <group>"


def _fake_torrent(name: str) -> TorrentInfo:
    return TorrentInfo(
        hash="abc123def456789012345678901234567890abcd",
        name=name,
        size=1_000_000,
        progress=0.5,
        state=TorrentState.DOWNLOADING,
    )


@pytest.mark.asyncio
async def test_cmd_pause_escapes_torrent_name():
    """SEC-01: a torrent name with & / < must be escaped so the HTML-mode
    confirmation message does not break (400 can't parse entities)."""
    from bot.handlers import downloads

    qbt = AsyncMock()
    qbt.get_torrent_by_short_hash = AsyncMock(return_value=_fake_torrent(_DANGEROUS_NAME))
    qbt.pause = AsyncMock()

    message = MagicMock()
    message.text = "/pause abc123de"
    message.answer = AsyncMock()

    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)):
        await downloads.cmd_pause(message, db_user=MagicMock())

    sent = message.answer.await_args_list[-1].args[0]
    assert html.escape(_DANGEROUS_NAME) in sent
    assert _DANGEROUS_NAME not in sent  # raw, unescaped name must NOT appear


@pytest.mark.asyncio
async def test_cmd_resume_escapes_torrent_name():
    """SEC-01: same as pause, for /resume."""
    from bot.handlers import downloads

    qbt = AsyncMock()
    qbt.get_torrent_by_short_hash = AsyncMock(return_value=_fake_torrent(_DANGEROUS_NAME))
    qbt.resume = AsyncMock()

    message = MagicMock()
    message.text = "/resume abc123de"
    message.answer = AsyncMock()

    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)):
        await downloads.cmd_resume(message, db_user=MagicMock())

    sent = message.answer.await_args_list[-1].args[0]
    assert html.escape(_DANGEROUS_NAME) in sent
    assert _DANGEROUS_NAME not in sent


# ---------------------------------------------------------------------------
# SEC-02: TMDB titles must be html-escaped in trending add confirmations
# ---------------------------------------------------------------------------
_DANGEROUS_TITLE = "Fast & Furious <hd>"


def _trending_add_service(add_result, kind: str):
    """An `AddService` double shaped like the trending add flow expects.

    Rollback 2026-08-10: the trending handlers no longer call a single
    `add_and_queue_best`. They resolve the user's per-service profile and root
    folder first (Radarr's and Sonarr's id spaces are independent), then call
    `add_movie`/`add_series`, which return `(added, ActionLog)`.
    """
    svc = MagicMock()
    profile = MagicMock(id=7, name="4k/1080p")
    folder = MagicMock(id=1, path="G:\\radarr\\Films")
    svc.get_radarr_profiles = AsyncMock(return_value=[profile])
    svc.get_radarr_root_folders = AsyncMock(return_value=[folder])
    svc.get_sonarr_profiles = AsyncMock(return_value=[profile])
    svc.get_sonarr_root_folders = AsyncMock(return_value=[folder])
    if kind == "movie":
        svc.add_movie = AsyncMock(return_value=add_result)
    else:
        svc.add_series = AsyncMock(return_value=add_result)
    return svc


@pytest.mark.asyncio
async def test_trending_add_movie_escapes_title():
    """SEC-02: a trending movie title with & / < must be escaped in the success edit."""
    from bot.handlers import trending

    added = MovieInfo(tmdb_id=123, title=_DANGEROUS_TITLE, year=2024)
    action = MagicMock(success=True, error_message=None)
    add_service = _trending_add_service(add_result=(added, action), kind="movie")

    db = AsyncMock()
    db_user = MagicMock()
    db_user.tg_id = 123
    db_user.preferences = MagicMock(
        radarr_quality_profile_id=None, radarr_root_folder_id=None,
        sonarr_quality_profile_id=None, sonarr_root_folder_id=None,
    )

    cb, status_msg = _callback_with_status()
    cb.data = None

    trending._trending_movies_cache.clear()
    trending._trending_movies_cache[123] = added

    from bot.ui.callbacks import AddContentCB

    with patch.object(trending, "get_radarr", AsyncMock()), \
         patch.object(trending, "get_sonarr", AsyncMock()), \
         patch.object(trending, "get_qbittorrent", AsyncMock()), \
         patch.object(trending, "AddService", return_value=add_service):
        await trending.handle_add_movie_from_trending(
            cb, AddContentCB(kind="movie", tmdb_id=123), db_user=db_user, db=db
        )

    sent = status_msg.edit_text.await_args_list[-1].args[0]
    assert html.escape(_DANGEROUS_TITLE) in sent
    assert _DANGEROUS_TITLE not in sent


# ---------------------------------------------------------------------------
# RACE-02 / DB-01: explicit BEGIN..commit on the shared connection must be
# serialized so concurrent writers don't clobber each other's transaction.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_concurrent_writes_do_not_race_transactions():
    """RACE-02/DB-01: many concurrent writers + a cleanup pass on the single
    shared connection must all complete without 'cannot start a transaction
    within a transaction'.

    2026-07-30: the writer here used to be `save_search`, which no production
    code called any more — a race test over a dead path proves nothing. It now
    exercises `log_action`, the writer that actually runs on every search and
    grab, so a regression in the write lock would fail this test for real.
    """
    from bot.db import Database
    from bot.models import ActionLog, ActionType

    db = Database(":memory:")
    await db.connect()
    await db.create_user(User(tg_id=1, username="u", first_name="f"))

    async def write():
        await db.log_action(ActionLog(
            user_id=1,
            action_type=ActionType.SEARCH,
            content_type=ContentType.MOVIE,
            query="q",
        ))

    async def clean():
        await db.cleanup_old_searches(days=7)

    tasks = []
    for _ in range(12):
        tasks.append(asyncio.create_task(write()))
        tasks.append(asyncio.create_task(clean()))

    # Must not raise sqlite3.OperationalError (nested transaction) and must not lose data.
    await asyncio.gather(*tasks)

    actions = await db.get_user_actions(1, limit=50)
    assert len(actions) == 12, "every concurrent write must have landed"
    await db.close()


# ---------------------------------------------------------------------------
# RACE-01: rapid double-tap on grab must not execute the grab twice.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_grab_guard_claims_and_releases():
    """RACE-01: a per-user claim must reject a second in-flight grab and free up
    after release."""
    from bot.handlers import search

    search._grab_in_progress.clear()
    assert await search._claim_grab(1) is True
    assert await search._claim_grab(1) is False  # second concurrent claim rejected
    assert await search._claim_grab(2) is True    # other users unaffected
    search._release_grab(1)
    assert await search._claim_grab(1) is True     # freed after release
    search._release_grab(1)
    search._release_grab(2)


@pytest.mark.asyncio
async def test_double_tap_grab_best_executes_once():
    """RACE-01: two concurrent handle_grab_best for the same user must call the
    actual grab exactly once; the loser is told it's already processing."""
    from bot.handlers import search

    search._grab_in_progress.clear()

    session = SearchSession(
        user_id=1, query="q", content_type=ContentType.MOVIE,
        results=[SearchResult(guid="g", title="t")],
    )
    db = AsyncMock()
    db.get_session = AsyncMock(return_value=session)
    db.save_session = AsyncMock()
    db_user = MagicMock()

    def make_cb():
        cb = MagicMock()
        cb.from_user = MagicMock(id=1)
        cb.message = MagicMock()
        cb.message.edit_text = AsyncMock()
        cb.answer = AsyncMock()
        return cb

    cb1, cb2 = make_cb(), make_cb()

    # TEST-09: deterministic gate instead of a real asyncio.sleep race. The
    # winner's grab_release() blocks on this Event so the loser's
    # handle_grab_best (racing in via asyncio.gather) has to observe the
    # guard as still claimed — no wall-clock timing involved.
    release_grab = asyncio.Event()

    async def gated_grab(*a, **k):
        await release_grab.wait()

    async def release_after_both_attempted():
        # Cooperative yields let both handle_grab_best coroutines run up to
        # their _claim_grab checkpoint before the winner's grab is unblocked.
        for _ in range(10):
            await asyncio.sleep(0)
        release_grab.set()

    services = (MagicMock(), MagicMock())

    with patch.object(search, "get_services", AsyncMock(return_value=services)), \
         patch.object(search, "grab_release", AsyncMock(side_effect=gated_grab)) as gr:
        await asyncio.gather(
            search.handle_grab_best(cb1, db_user, db),
            search.handle_grab_best(cb2, db_user, db),
            release_after_both_attempted(),
        )

    assert gr.await_count == 1, "grab ran more than once on a double-tap"
    answers = [c.args[0] for cb in (cb1, cb2) for c in cb.answer.await_args_list if c.args]
    assert any("обраб" in a.lower() for a in answers), f"no busy answer seen: {answers}"


# ---------------------------------------------------------------------------
# BUG-01: trending "Назад" must NOT collide with search.handle_back ("back")
# ---------------------------------------------------------------------------
def test_trending_keyboards_use_dedicated_back_callback():
    """BUG-01: trending list back buttons must use TRENDING_BACK, not the shared
    CallbackData.BACK that search.handle_back claims first."""
    from bot.ui.keyboards import CallbackData, Keyboards

    assert hasattr(CallbackData, "TRENDING_BACK")
    assert CallbackData.TRENDING_BACK != CallbackData.BACK

    movie = MovieInfo(tmdb_id=1, title="X", year=2020)
    series = SeriesInfo(tvdb_id=1, title="Y", year=2020)
    for kb in (Keyboards.trending_movies([movie]), Keyboards.trending_series([series])):
        back_btn = kb.inline_keyboard[-1][0]
        assert back_btn.callback_data == CallbackData.TRENDING_BACK


@pytest.mark.asyncio
async def test_handle_trending_back_renders_menu():
    """BUG-01: a dedicated handler must re-render the trending menu on TRENDING_BACK."""
    from bot.handlers import trending

    cb = MagicMock()
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()

    await trending.handle_trending_back(cb)

    cb.message.edit_text.assert_awaited()
    sent = cb.message.edit_text.await_args.args[0] if cb.message.edit_text.await_args.args \
        else cb.message.edit_text.await_args.kwargs.get("text", "")
    assert "Популярное" in sent
    assert cb.message.edit_text.await_args.kwargs.get("reply_markup") is not None


@pytest.mark.asyncio
async def test_trending_add_series_escapes_title():
    """SEC-02: a trending series title with & / < must be escaped in the success edit."""
    from bot.handlers import trending

    action = MagicMock(success=True, error_message=None)
    cached = SeriesInfo(tvdb_id=999, tmdb_id=123, title=_DANGEROUS_TITLE, year=2020)
    add_service = _trending_add_service(add_result=(cached, action), kind="series")

    db = AsyncMock()
    db_user = MagicMock()
    db_user.tg_id = 123
    db_user.preferences = MagicMock(
        radarr_quality_profile_id=None, radarr_root_folder_id=None,
        sonarr_quality_profile_id=None, sonarr_root_folder_id=None,
    )

    cb, status_msg = _callback_with_status()
    cb.data = None

    trending._trending_series_cache.clear()
    trending._trending_series_cache[123] = cached

    from bot.ui.callbacks import AddContentCB

    with patch.object(trending, "get_radarr", AsyncMock()), \
         patch.object(trending, "get_sonarr", AsyncMock()), \
         patch.object(trending, "get_qbittorrent", AsyncMock()), \
         patch.object(trending, "AddService", return_value=add_service):
        await trending.handle_add_series_from_trending(
            cb, AddContentCB(kind="series", tmdb_id=123), db_user=db_user, db=db
        )

    sent = status_msg.edit_text.await_args_list[-1].args[0]
    assert html.escape(_DANGEROUS_TITLE) in sent
    assert _DANGEROUS_TITLE not in sent
