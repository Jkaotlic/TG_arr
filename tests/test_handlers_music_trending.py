"""Tests for bot/handlers/music.py and bot/handlers/trending.py.

Covers (Task D):
- BUG-10/PERF-12: LRU eviction (not clear()) of per-user in-memory caches +
  6h TTL for trending caches.
- BUG-12b: html.escape(error_msg) before interpolating *arr error text into
  an HTML-parsed Telegram message.
- TEST-08b: art_page: handler with a valid and a garbage page.
- LOGIC-22: trending add_series flow reuses the already-fetched `sonarr`
  client instead of calling get_sonarr() a second time.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.models import ArtistInfo


def _make_artist(i: int) -> ArtistInfo:
    return ArtistInfo(mb_id=f"mb-{i}", name=f"Artist {i}")


def _make_callback(data: str, user_id: int = 111) -> MagicMock:
    cb = MagicMock()
    cb.data = data
    cb.answer = AsyncMock()
    cb.from_user = MagicMock(id=user_id)
    message = MagicMock()
    message.edit_text = AsyncMock()
    cb.message = message
    return cb


# ---------------------------------------------------------------------------
# BUG-10/PERF-12 — music.py: _artist_candidates / _trending_artists_cache
# ---------------------------------------------------------------------------


def test_cleanup_if_overflow_evicts_oldest_not_everything():
    """RED: at MAX capacity, inserting one more entry must evict only the
    single oldest key — every other (fresher) user's cache entry survives.
    A naive cache.clear() would wipe all of them.
    """
    from bot.handlers import music

    cache: dict[int, list] = {}
    for uid in range(music._MAX_ARTIST_CANDIDATES):
        music._remember(cache, uid, [_make_artist(uid)])

    # Cache is now exactly at capacity; every uid 0..MAX-1 present.
    assert len(cache) == music._MAX_ARTIST_CANDIDATES
    assert 0 in cache
    assert (music._MAX_ARTIST_CANDIDATES - 1) in cache

    # One more insert (a new user) must evict exactly the oldest (uid=0),
    # NOT clear the whole cache.
    new_uid = music._MAX_ARTIST_CANDIDATES
    music._remember(cache, new_uid, [_make_artist(new_uid)])

    assert len(cache) == music._MAX_ARTIST_CANDIDATES, "cache grew or was wiped instead of evicting one"
    assert 0 not in cache, "oldest entry should have been evicted"
    assert new_uid in cache, "newly inserted entry must be present"
    # A "recently active" user (not the oldest) must survive the overflow —
    # this is the regression BUG-10 describes (cache.clear() would kick them
    # out with 'Список истёк').
    recent_uid = music._MAX_ARTIST_CANDIDATES - 1
    assert recent_uid in cache, "a fresh (non-oldest) user's entry must survive overflow eviction"


def test_remember_refresh_moves_key_to_freshest():
    """Re-inserting an existing key should reset its eviction order (LRU touch),
    not just be a no-op update."""
    from bot.handlers import music

    cache: dict[int, list] = {}
    for uid in range(music._MAX_ARTIST_CANDIDATES):
        music._remember(cache, uid, [_make_artist(uid)])

    # Touch uid=0 (the otherwise-oldest) so it becomes freshest.
    music._remember(cache, 0, [_make_artist(0)])

    # Now insert a new user — this should evict uid=1 (now the oldest),
    # not uid=0 (which was just refreshed).
    new_uid = music._MAX_ARTIST_CANDIDATES
    music._remember(cache, new_uid, [_make_artist(new_uid)])

    assert 0 in cache, "refreshed key must not be evicted"
    assert 1 not in cache, "the new oldest key (uid=1) should be evicted"


@pytest.mark.asyncio
async def test_trending_music_cache_uses_remember_not_clear():
    """handle_trending_music must populate _trending_artists_cache via the
    LRU-safe _remember helper (BUG-10), not a bare assignment/clear pattern."""
    from bot.handlers import music

    music._trending_artists_cache.clear()

    deezer = AsyncMock()
    deezer.get_trending_artists = AsyncMock(return_value=[{"name": "Metallica", "id": 1}])

    cb = _make_callback("trending_music", user_id=42)

    with patch.object(music, "get_deezer", AsyncMock(return_value=deezer)):
        await music.handle_trending_music(cb)

    assert 42 in music._trending_artists_cache
    music._trending_artists_cache.clear()


# ---------------------------------------------------------------------------
# TEST-08b — music.py: art_page: handler (valid / garbage page)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_art_page_valid_page_renders():
    """A valid page index re-renders the artist list and acks exactly once."""
    from bot.handlers import music

    user_id = 777
    artists = [_make_artist(i) for i in range(12)]  # 3 pages @ per_page=5
    music._artist_candidates[user_id] = artists

    cb = _make_callback(f"{music.CallbackData.ARTIST_PAGE}1", user_id=user_id)

    await music.handle_artist_pagination(cb, db_user=MagicMock(), db=MagicMock())

    cb.message.edit_text.assert_awaited_once()
    assert cb.answer.call_count == 1
    music._artist_candidates.pop(user_id, None)


@pytest.mark.asyncio
async def test_art_page_garbage_page_string_rejected():
    """Non-integer page suffix must be rejected with an alert, no edit_text call."""
    from bot.handlers import music

    user_id = 778
    music._artist_candidates[user_id] = [_make_artist(0)]

    cb = _make_callback(f"{music.CallbackData.ARTIST_PAGE}not-a-number", user_id=user_id)

    await music.handle_artist_pagination(cb, db_user=MagicMock(), db=MagicMock())

    cb.answer.assert_awaited_once_with("Неверная страница", show_alert=True)
    cb.message.edit_text.assert_not_awaited()
    music._artist_candidates.pop(user_id, None)


@pytest.mark.asyncio
async def test_art_page_out_of_range_rejected():
    """A syntactically valid but out-of-range page index is rejected."""
    from bot.handlers import music

    user_id = 779
    music._artist_candidates[user_id] = [_make_artist(0)]  # 1 page only (page 0)

    cb = _make_callback(f"{music.CallbackData.ARTIST_PAGE}99", user_id=user_id)

    await music.handle_artist_pagination(cb, db_user=MagicMock(), db=MagicMock())

    cb.answer.assert_awaited_once_with("Неверная страница", show_alert=True)
    cb.message.edit_text.assert_not_awaited()
    music._artist_candidates.pop(user_id, None)


@pytest.mark.asyncio
async def test_art_page_expired_list_rejected():
    """No cached candidates (session expired) → alert, not a crash."""
    from bot.handlers import music

    user_id = 780
    music._artist_candidates.pop(user_id, None)

    cb = _make_callback(f"{music.CallbackData.ARTIST_PAGE}0", user_id=user_id)

    await music.handle_artist_pagination(cb, db_user=MagicMock(), db=MagicMock())

    cb.answer.assert_awaited_once_with("Список истёк. Начните новый поиск.", show_alert=True)
    cb.message.edit_text.assert_not_awaited()


# ---------------------------------------------------------------------------
# LOGIC-14a — music.py: _render_artist_list dedup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_artist_list_used_by_pagination_and_back():
    """handle_music_back and handle_artist_pagination must render identical
    text/keyboard construction via the shared _render_artist_list helper."""
    from bot.handlers import music

    user_id = 781
    artists = [_make_artist(i) for i in range(3)]
    music._artist_candidates[user_id] = artists

    cb_back = _make_callback(music.CallbackData.MUSIC_BACK, user_id=user_id)
    await music.handle_music_back(cb_back, db_user=MagicMock(), db=MagicMock())

    cb_page = _make_callback(f"{music.CallbackData.ARTIST_PAGE}0", user_id=user_id)
    await music.handle_artist_pagination(cb_page, db_user=MagicMock(), db=MagicMock())

    # Both should have produced the same rendered text (same page=0 content).
    back_text = cb_back.message.edit_text.call_args.args[0]
    page_text = cb_page.message.edit_text.call_args.args[0]
    assert back_text == page_text


# ---------------------------------------------------------------------------
# LOGIC-14b — music.py: artist-list pagination respects settings.results_per_page
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_artist_list_respects_results_per_page_setting(monkeypatch):
    """Artist-list page size must follow settings.results_per_page (like search
    pagination already does) instead of a hardcoded 5."""
    monkeypatch.setenv("RESULTS_PER_PAGE", "3")
    from bot.config import get_settings

    get_settings.cache_clear()
    try:
        from bot.handlers import music

        assert get_settings().results_per_page == 3

        message = MagicMock()
        message.edit_text = AsyncMock()
        artists = [_make_artist(i) for i in range(5)]

        await music._render_artist_list(message, artists, page=0)

        keyboard = message.edit_text.call_args.kwargs["reply_markup"]
        # One button row per artist on the page, plus a nav row (since
        # total_pages > 1 for 5 artists @ per_page=3) — count artist rows only.
        artist_button_rows = [
            row for row in keyboard.inline_keyboard
            if len(row) == 1 and row[0].callback_data.startswith("art:")
        ]
        assert len(artist_button_rows) == 3
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# BUG-10/PERF-12 — trending.py: TTL + LRU eviction
# ---------------------------------------------------------------------------


def test_trending_cache_put_evicts_oldest_on_overflow():
    """RED: overflow must evict only the single oldest entry, not clear()
    the whole trending cache (which used to reset every other user's active
    trending list).

    Uses the real module-level `_trending_movies_cache` + its timestamp
    side-table, since `_cache_put`/`_cache_get` key the side-table by the
    cache dict's identity (registered once at module import).
    """
    from bot.handlers import trending

    cache = trending._trending_movies_cache
    cache.clear()
    try:
        for i in range(trending._MAX_CACHE_SIZE):
            trending._cache_put(cache, i, f"item-{i}")

        assert len(cache) == trending._MAX_CACHE_SIZE
        assert 0 in cache

        trending._cache_put(cache, trending._MAX_CACHE_SIZE, "new-item")

        assert len(cache) == trending._MAX_CACHE_SIZE, "overflow must not grow the cache unbounded"
        assert 0 not in cache, "oldest entry must be evicted"
        # A "fresh" entry near the end must survive — this is exactly the
        # regression BUG-10 flags: clear() would wipe it too.
        assert (trending._MAX_CACHE_SIZE - 1) in cache
    finally:
        cache.clear()


def test_trending_cache_get_respects_ttl():
    """PERF-12: an entry older than the 6h TTL is treated as a cache miss
    and purged, instead of being served stale forever."""
    from bot.handlers import trending

    cache = trending._trending_series_cache
    cache.clear()
    try:
        # Insert directly with a fabricated "stale" timestamp — simulate an
        # item inserted more than TTL seconds ago.
        with patch("time.monotonic", return_value=0.0):
            trending._cache_put(cache, 1, "stale-item")
        # Manually backdate the recorded insertion timestamp (avoids reliance
        # on wall clock in test).
        trending._trending_series_inserted_at[1] = -(trending._CACHE_TTL_SECONDS + 10)

        result = trending._cache_get(cache, 1)
        assert result is None, "TTL-expired entry must be treated as a miss"
        assert 1 not in cache, "TTL-expired entry should be purged on lookup"
    finally:
        cache.clear()


def test_trending_cache_get_fresh_entry_returned():
    """A freshly-inserted entry (well within TTL) must be returned."""
    from bot.handlers import trending

    cache = trending._trending_movies_cache
    cache.clear()
    try:
        trending._cache_put(cache, 5, "fresh-item")
        assert trending._cache_get(cache, 5) == "fresh-item"
    finally:
        cache.clear()


@pytest.mark.asyncio
async def test_handle_trending_movies_cache_survives_overflow_for_recent_items():
    """Full-handler check: after MAX_CACHE_SIZE+1 distinct trending fetches,
    the most recently cached movie must still be resolvable (not wiped by a
    clear())."""
    from bot.handlers import trending

    trending._trending_movies_cache.clear()

    class FakeMovie:
        def __init__(self, tmdb_id):
            self.tmdb_id = tmdb_id
            self.title = f"Movie {tmdb_id}"
            self.year = 2024

    # Pre-fill the cache near capacity directly (faster than N handler calls).
    for i in range(trending._MAX_CACHE_SIZE):
        trending._cache_put(trending._trending_movies_cache, i, FakeMovie(i))

    tmdb = AsyncMock()
    tmdb.get_trending_movies = AsyncMock(return_value=[FakeMovie(trending._MAX_CACHE_SIZE)])

    cb = _make_callback("trending_movies")

    with patch.object(trending, "get_tmdb", AsyncMock(return_value=tmdb)), \
         patch.object(trending.Formatters, "format_trending_movies", return_value="TXT"), \
         patch.object(trending.Keyboards, "trending_movies", return_value="KB"):
        await trending.handle_trending_movies(cb)

    assert len(trending._trending_movies_cache) == trending._MAX_CACHE_SIZE
    assert 0 not in trending._trending_movies_cache, "oldest movie must have been evicted"
    assert trending._MAX_CACHE_SIZE in trending._trending_movies_cache, "newly fetched movie must be cached"
    trending._trending_movies_cache.clear()


# ---------------------------------------------------------------------------
# BUG-12b — trending.py: html.escape(error_msg)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_movie_from_trending_escapes_error_message():
    """RED: action.error_message with raw '<'/'>' must not break HTML parsing
    — it must be html.escape()'d before interpolation."""
    from bot.handlers import trending
    from bot.models import ActionLog, ActionType, ContentType

    class FakeMovie:
        tmdb_id = 99
        title = "Some Movie"
        year = 2024
        poster_url = None

    movie = FakeMovie()
    trending._trending_movies_cache.clear()
    trending._cache_put(trending._trending_movies_cache, movie.tmdb_id, movie)

    action = ActionLog(
        user_id=1,
        action_type=ActionType.ADD,
        content_type=ContentType.MOVIE,
        success=False,
        error_message="Radarr rejected: <script>bad</script> quality profile missing",
    )

    add_service = AsyncMock()
    add_service.get_radarr_profiles = AsyncMock(return_value=[MagicMock(id=1)])
    add_service.get_radarr_root_folders = AsyncMock(return_value=[MagicMock(id=1, path="/movies")])
    add_service.add_movie = AsyncMock(return_value=(None, action))

    db = AsyncMock()
    db_user = MagicMock()
    db_user.tg_id = 1
    db_user.preferences = MagicMock(radarr_quality_profile_id=None, radarr_root_folder_id=None)

    cb = _make_callback(None)
    status_msg = MagicMock()
    status_msg.edit_text = AsyncMock()
    cb.message.answer = AsyncMock(return_value=status_msg)

    from bot.ui.callbacks import AddContentCB

    with patch.object(trending, "get_prowlarr", AsyncMock()), \
         patch.object(trending, "get_radarr", AsyncMock()), \
         patch.object(trending, "get_sonarr", AsyncMock()), \
         patch.object(trending, "get_qbittorrent", AsyncMock()), \
         patch.object(trending, "AddService", return_value=add_service):
        await trending.handle_add_movie_from_trending(cb, AddContentCB(kind="movie", tmdb_id=99), db_user, db)

    sent_text = status_msg.edit_text.call_args.args[0]
    assert "<script>" not in sent_text, "raw HTML in error_message must be escaped (BUG-12b)"
    assert "&lt;script&gt;" in sent_text
    trending._trending_movies_cache.clear()


# ---------------------------------------------------------------------------
# LOGIC-22 — trending.py: reuse `sonarr` instead of a second get_sonarr()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_series_from_trending_does_not_call_get_sonarr_twice():
    """The TVDB-resolution branch must reuse the `sonarr` client obtained
    earlier in the handler, not call get_sonarr() again."""
    from bot.handlers import trending
    from bot.models import ActionLog, ActionType, ContentType

    class FakeSeries:
        tmdb_id = 55
        tvdb_id = 0  # triggers the resolve-TVDB branch
        title = "Some Series"
        year = 2024
        poster_url = None

    class FakeLookupResult:
        tmdb_id = 55
        tvdb_id = 12345
        title = "Some Series"

    series = FakeSeries()
    trending._trending_series_cache.clear()
    trending._cache_put(trending._trending_series_cache, series.tmdb_id, series)

    sonarr_client = AsyncMock()
    sonarr_client.lookup_series = AsyncMock(return_value=[FakeLookupResult()])
    get_sonarr_mock = AsyncMock(return_value=sonarr_client)

    action = ActionLog(
        user_id=1,
        action_type=ActionType.ADD,
        content_type=ContentType.SERIES,
        success=True,
    )
    add_service = AsyncMock()
    add_service.get_sonarr_profiles = AsyncMock(return_value=[MagicMock(id=1)])
    add_service.get_sonarr_root_folders = AsyncMock(return_value=[MagicMock(id=1, path="/tv")])
    add_service.add_series = AsyncMock(return_value=(MagicMock(title="Some Series", year=2024), action))

    db = AsyncMock()
    db_user = MagicMock()
    db_user.tg_id = 1
    db_user.preferences = MagicMock(sonarr_quality_profile_id=None, sonarr_root_folder_id=None)

    cb = _make_callback(None)
    status_msg = MagicMock()
    status_msg.edit_text = AsyncMock()
    cb.message.answer = AsyncMock(return_value=status_msg)

    from bot.ui.callbacks import AddContentCB

    with patch.object(trending, "get_prowlarr", AsyncMock()), \
         patch.object(trending, "get_radarr", AsyncMock()), \
         patch.object(trending, "get_sonarr", get_sonarr_mock), \
         patch.object(trending, "get_qbittorrent", AsyncMock()), \
         patch.object(trending, "AddService", return_value=add_service):
        await trending.handle_add_series_from_trending(cb, AddContentCB(kind="series", tmdb_id=55), db_user, db)

    # get_sonarr() is called exactly once (to build AddService's sonarr client),
    # not a second time inside the TVDB-resolution branch (LOGIC-22).
    assert get_sonarr_mock.await_count == 1, (
        f"get_sonarr() called {get_sonarr_mock.await_count} times; expected 1 "
        "(TVDB-resolution branch must reuse the existing `sonarr` variable)"
    )
    sonarr_client.lookup_series.assert_awaited_once()
    trending._trending_series_cache.clear()
