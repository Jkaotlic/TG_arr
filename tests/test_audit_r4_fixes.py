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


# ---------------------------------------------------------------------------
# SEC-03: passkey in push-result logs must be stripped before logging
# ---------------------------------------------------------------------------
def test_safe_push_result_strips_download_url_with_passkey():
    """SEC-03: _safe_push_result must keep only approved/rejections, never the
    raw downloadUrl that carries a private-tracker passkey."""
    from bot.services.add_service import _safe_push_result

    raw = {
        "approved": False,
        "rejections": ["Quality not wanted"],
        "downloadUrl": "https://tracker.example/rss/?passkey=SUPERSECRET123",
        "title": "Some.Release.2024",
    }
    safe = _safe_push_result(raw)

    serialized = str(safe)
    assert "SUPERSECRET123" not in serialized
    assert "passkey" not in serialized
    assert safe.get("approved") is False
    assert safe.get("rejections") == ["Quality not wanted"]


def test_safe_push_result_handles_none():
    """SEC-03: must not blow up when *arr returns no/empty body."""
    from bot.services.add_service import _safe_push_result

    assert _safe_push_result(None) == {"approved": None, "rejections": []}


# ---------------------------------------------------------------------------
# SEC-01: torrent.name must be html-escaped in /pause and /resume confirmations
# ---------------------------------------------------------------------------
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


def _callback_with_status():
    """Build a callback whose message.answer returns a status_msg with edit_text."""
    status_msg = MagicMock()
    status_msg.edit_text = AsyncMock()
    cb = MagicMock()
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.answer = AsyncMock(return_value=status_msg)
    return cb, status_msg


@pytest.mark.asyncio
async def test_trending_add_movie_escapes_title():
    """SEC-02: a trending movie title with & / < must be escaped in the success edit."""
    from bot.handlers import trending

    added = MovieInfo(tmdb_id=123, title=_DANGEROUS_TITLE, year=2024)
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
    trending._trending_movies_cache[123] = added

    from bot.ui.callbacks import AddContentCB

    with patch.object(trending, "get_prowlarr", AsyncMock()), \
         patch.object(trending, "get_radarr", AsyncMock()), \
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
    """RACE-02/DB-01: many concurrent save_search + cleanup_old_searches calls
    on the single shared connection must all complete without
    'cannot start a transaction within a transaction'."""
    from bot.db import Database

    db = Database(":memory:")
    await db.connect()
    await db.create_user(User(tg_id=1, username="u", first_name="f"))

    results = [SearchResult(guid="g", title="t")]

    async def save():
        await db.save_search(1, "q", ContentType.MOVIE, results)

    async def clean():
        await db.cleanup_old_searches(days=7)

    tasks = []
    for _ in range(12):
        tasks.append(asyncio.create_task(save()))
        tasks.append(asyncio.create_task(clean()))

    # Must not raise sqlite3.OperationalError (nested transaction) and must not lose data.
    await asyncio.gather(*tasks)
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

    async def slow_grab(*a, **k):
        await asyncio.sleep(0.05)

    services = (MagicMock(), MagicMock())

    with patch.object(search, "get_services", AsyncMock(return_value=services)), \
         patch.object(search, "grab_release", AsyncMock(side_effect=slow_grab)) as gr:
        await asyncio.gather(
            search.handle_grab_best(cb1, db_user, db),
            search.handle_grab_best(cb2, db_user, db),
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

    added = SeriesInfo(tvdb_id=999, title=_DANGEROUS_TITLE, year=2020)
    action = MagicMock(success=True, error_message=None)
    add_service = MagicMock()
    add_service.get_sonarr_profiles = AsyncMock(return_value=[MagicMock(id=1)])
    add_service.get_sonarr_root_folders = AsyncMock(return_value=[MagicMock(id=1, path="/tv")])
    add_service.add_series = AsyncMock(return_value=(added, action))

    db = AsyncMock()
    db_user = MagicMock()
    db_user.tg_id = 123
    db_user.preferences = MagicMock(sonarr_quality_profile_id=None, sonarr_root_folder_id=None)

    cb, status_msg = _callback_with_status()
    cb.data = None

    cached = SeriesInfo(tvdb_id=999, tmdb_id=123, title=_DANGEROUS_TITLE, year=2020)
    trending._trending_series_cache.clear()
    trending._trending_series_cache[123] = cached

    from bot.ui.callbacks import AddContentCB

    with patch.object(trending, "get_prowlarr", AsyncMock()), \
         patch.object(trending, "get_radarr", AsyncMock()), \
         patch.object(trending, "get_sonarr", AsyncMock()), \
         patch.object(trending, "get_qbittorrent", AsyncMock()), \
         patch.object(trending, "AddService", return_value=add_service):
        await trending.handle_add_series_from_trending(
            cb, AddContentCB(kind="series", tmdb_id=123), db_user=db_user, db=db
        )

    sent = status_msg.edit_text.await_args_list[-1].args[0]
    assert html.escape(_DANGEROUS_TITLE) in sent
    assert _DANGEROUS_TITLE not in sent
