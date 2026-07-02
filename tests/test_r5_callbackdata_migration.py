"""r5 follow-up: migration of data-carrying callback families to typed
CallbackData (analysis/r5/05-logic-issues.md "Карта миграции CallbackData").

Each family gets: a pack/unpack round-trip test, and a keyboard->handler
round-trip test that catches the "keyboard generated one shape, handler
expects another" class of bug (the actual risk in this kind of migration).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.models import ArtistInfo, MovieInfo, SearchResult, SeriesInfo, TorrentInfo


def _cbs(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]


# ---------------------------------------------------------------------------
# ReleaseCB (rel:N)
# ---------------------------------------------------------------------------


def test_release_cb_roundtrip():
    from bot.ui.callbacks import ReleaseCB

    packed = ReleaseCB(idx=7).pack()
    got = ReleaseCB.unpack(packed)
    assert got.idx == 7


def test_search_results_release_buttons_use_typed_cb():
    from bot.ui.callbacks import ReleaseCB
    from bot.ui.keyboards import Keyboards

    results = [SearchResult(guid=str(i), title=f"t{i}") for i in range(3)]
    kb = Keyboards.search_results(results, 0, 1, 5, False, 0)
    cbs = _cbs(kb)
    rel_cbs = [c for c in cbs if c.startswith("rel:")]
    assert len(rel_cbs) == 3
    unpacked = sorted(ReleaseCB.unpack(c).idx for c in rel_cbs)
    assert unpacked == [0, 1, 2]


@pytest.mark.asyncio
async def test_handle_release_selection_reads_callback_data():
    from bot.handlers.search import results as results_mod
    from bot.models import ContentType, SearchSession
    from bot.ui.callbacks import ReleaseCB

    session = SearchSession(
        user_id=1, query="q", content_type=ContentType.MOVIE,
        results=[SearchResult(guid=str(i), title=f"t{i}") for i in range(3)],
    )
    db = AsyncMock()
    db.get_session = AsyncMock(return_value=session)
    db.save_session = AsyncMock()
    import asyncio
    db.session_lock = MagicMock(return_value=asyncio.Lock())

    db_user = MagicMock()
    cb = MagicMock()
    cb.data = None
    cb.from_user = MagicMock(id=1)
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(results_mod._search, "get_services", AsyncMock(return_value=(MagicMock(), MagicMock(qbittorrent=None))))
        await results_mod.handle_release_selection(cb, ReleaseCB(idx=1), db_user, db)

    assert session.selected_result.guid == "1"


# ---------------------------------------------------------------------------
# ArtistCB (artist:N)
# ---------------------------------------------------------------------------


def test_artist_cb_roundtrip():
    from bot.ui.callbacks import ArtistCB

    packed = ArtistCB(idx=4).pack()
    got = ArtistCB.unpack(packed)
    assert got.idx == 4


def test_artist_list_buttons_use_typed_cb():
    from bot.ui.callbacks import ArtistCB
    from bot.ui.keyboards import Keyboards

    artists = [ArtistInfo(name=f"A{i}", mb_id=str(i)) for i in range(3)]
    kb = Keyboards.artist_list(artists, current_page=0, per_page=5)
    cbs = _cbs(kb)
    artist_cbs = [c for c in cbs if c.startswith("art:")]
    assert len(artist_cbs) == 3
    unpacked = sorted(ArtistCB.unpack(c).idx for c in artist_cbs)
    assert unpacked == [0, 1, 2]


@pytest.mark.asyncio
async def test_handle_artist_selection_reads_callback_data():
    from bot.handlers import music
    from bot.ui.callbacks import ArtistCB

    artists = [ArtistInfo(name="Metallica", mb_id="m1"), ArtistInfo(name="Slayer", mb_id="m2")]
    music._artist_candidates[42] = artists

    db = AsyncMock()
    db.get_session = AsyncMock(return_value=None)
    db.save_session = AsyncMock()
    import asyncio
    db.session_lock = MagicMock(return_value=asyncio.Lock())

    db_user = MagicMock()
    cb = MagicMock()
    cb.data = None
    cb.from_user = MagicMock(id=42)
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()

    await music.handle_artist_selection(cb, ArtistCB(idx=1), db_user, db)

    saved_session = db.save_session.call_args.args[1]
    assert saved_session.selected_content.name == "Slayer"
    music._artist_candidates.pop(42, None)


# ---------------------------------------------------------------------------
# AddContentCB (add_movie:ID / add_series:ID)
# ---------------------------------------------------------------------------


def test_add_content_cb_roundtrip():
    from bot.ui.callbacks import AddContentCB

    packed = AddContentCB(kind="movie", tmdb_id=603).pack()
    got = AddContentCB.unpack(packed)
    assert got.kind == "movie"
    assert got.tmdb_id == 603


def test_trending_movie_series_details_buttons_use_typed_cb():
    from bot.ui.callbacks import AddContentCB
    from bot.ui.keyboards import Keyboards

    movie = MovieInfo(title="The Matrix", tmdb_id=603, year=1999)
    kb = Keyboards.movie_details(movie)
    cbs = _cbs(kb)
    add_cbs = [c for c in cbs if c.startswith("addc:")]
    assert len(add_cbs) == 1
    got = AddContentCB.unpack(add_cbs[0])
    assert got.kind == "movie" and got.tmdb_id == 603

    series = SeriesInfo(title="Breaking Bad", tvdb_id=1396, tmdb_id=1396)
    kb2 = Keyboards.series_details(series)
    cbs2 = _cbs(kb2)
    add_cbs2 = [c for c in cbs2 if c.startswith("addc:")]
    assert len(add_cbs2) == 1
    got2 = AddContentCB.unpack(add_cbs2[0])
    assert got2.kind == "series" and got2.tmdb_id == 1396


@pytest.mark.asyncio
async def test_handle_add_movie_from_trending_reads_callback_data():
    from bot.handlers import trending
    from bot.ui.callbacks import AddContentCB

    movie_c = MovieInfo(title="The Matrix", tmdb_id=603, year=1999)
    trending._cache_put(trending._trending_movies_cache, 603, movie_c)

    db = AsyncMock()
    db.log_action = AsyncMock()
    db_user = MagicMock()
    db_user.tg_id = 1
    db_user.preferences = MagicMock(radarr_quality_profile_id=None, radarr_root_folder_id=None)

    cb = MagicMock()
    cb.data = None
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    status_msg = MagicMock()
    status_msg.edit_text = AsyncMock()
    cb.message.answer = AsyncMock(return_value=status_msg)

    add_service = MagicMock()
    add_service.get_radarr_profiles = AsyncMock(return_value=[MagicMock(id=1)])
    add_service.get_radarr_root_folders = AsyncMock(return_value=[MagicMock(path="/movies")])
    added_movie = MagicMock(title="The Matrix", year=1999)
    action = MagicMock(success=True, error_message=None)
    add_service.add_movie = AsyncMock(return_value=(added_movie, action))

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(trending, "AddService", MagicMock(return_value=add_service))
        mp.setattr(trending, "get_prowlarr", AsyncMock(return_value=None))
        mp.setattr(trending, "get_radarr", AsyncMock(return_value=None))
        mp.setattr(trending, "get_sonarr", AsyncMock(return_value=None))
        mp.setattr(trending, "get_qbittorrent", AsyncMock(return_value=None))

        await trending.handle_add_movie_from_trending(cb, AddContentCB(kind="movie", tmdb_id=603), db_user, db)

    status_msg.edit_text.assert_awaited()
    assert "Матрица" in status_msg.edit_text.call_args.args[0] or "Matrix" in status_msg.edit_text.call_args.args[0]
    trending._trending_movies_cache.pop(603, None)
    trending._trending_movies_inserted_at.pop(603, None)


# ---------------------------------------------------------------------------
# SettingCB (set:rp:/rf:/sp:/sf:/lp:/lm:/lf:/res:/ag:)
# ---------------------------------------------------------------------------


def test_setting_cb_roundtrip():
    from bot.ui.callbacks import SettingCB

    packed = SettingCB(key="radarr_quality_profile_id", value="3").pack()
    got = SettingCB.unpack(packed)
    assert got.key == "radarr_quality_profile_id"
    assert got.value == "3"


def test_quality_profiles_keyboard_uses_typed_cb():
    from bot.models import QualityProfile
    from bot.ui.callbacks import SettingCB
    from bot.ui.keyboards import Keyboards

    profiles = [QualityProfile(id=5, name="HD-1080p")]
    kb = Keyboards.quality_profiles(profiles, key="radarr_quality_profile_id")
    cbs = _cbs(kb)
    set_cbs = [c for c in cbs if c.startswith("set:")]
    assert len(set_cbs) == 1
    got = SettingCB.unpack(set_cbs[0])
    assert got.key == "radarr_quality_profile_id"
    assert got.value == "5"


@pytest.mark.asyncio
async def test_handle_settings_set_reads_callback_data():
    from bot.handlers import settings as settings_mod
    from bot.ui.callbacks import SettingCB

    db = AsyncMock()
    db.update_user_preference = AsyncMock()
    db_user = MagicMock()
    db_user.preferences = MagicMock(radarr_quality_profile_id=None)

    cb = MagicMock()
    cb.data = None
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            settings_mod, "_render_settings_menu",
            AsyncMock(return_value=("text", MagicMock())),
        )
        await settings_mod.handle_settings_set(
            cb, SettingCB(key="radarr_quality_profile_id", value="9"), db_user, db
        )

    db.update_user_preference.assert_awaited_with(db_user.tg_id, "radarr_quality_profile_id", 9)
    assert db_user.preferences.radarr_quality_profile_id == 9


def test_resolution_and_auto_grab_keyboards_use_typed_setting_cb():
    from bot.ui.callbacks import SettingCB
    from bot.ui.keyboards import Keyboards

    kb = Keyboards.resolution_selection()
    cbs = [c for c in _cbs(kb) if c.startswith("set:")]
    assert cbs
    for c in cbs:
        got = SettingCB.unpack(c)
        assert got.key == "preferred_resolution"

    kb2 = Keyboards.auto_grab_toggle(current=False)
    cbs2 = [c for c in _cbs(kb2) if c.startswith("set:")]
    assert len(cbs2) == 1
    got2 = SettingCB.unpack(cbs2[0])
    assert got2.key == "auto_grab_enabled" and got2.value == "1"


# ---------------------------------------------------------------------------
# SeasonPresetCB (season_set:preset)
# ---------------------------------------------------------------------------


def test_season_preset_cb_roundtrip():
    from bot.ui.callbacks import SeasonPresetCB

    packed = SeasonPresetCB(preset="firstSeason").pack()
    got = SeasonPresetCB.unpack(packed)
    assert got.preset == "firstSeason"


def test_season_presets_keyboard_uses_typed_cb():
    from bot.ui.callbacks import SeasonPresetCB
    from bot.ui.keyboards import Keyboards

    kb = Keyboards.season_presets()
    cbs = [c for c in _cbs(kb) if c.startswith("ssn:")]
    presets = {SeasonPresetCB.unpack(c).preset for c in cbs}
    assert presets == {"all", "future", "firstSeason", "latestSeason"}


@pytest.mark.asyncio
async def test_handle_season_preset_reads_callback_data():
    from bot.handlers.search import grab as grab_mod
    from bot.models import ContentType, SearchResult, SearchSession
    from bot.ui.callbacks import SeasonPresetCB

    session = SearchSession(
        user_id=1, query="q", content_type=ContentType.SERIES,
        selected_result=SearchResult(guid="g", title="t"),
    )
    db = AsyncMock()
    db.get_session = AsyncMock(return_value=session)
    db.update_session = AsyncMock(return_value=True)
    import asyncio
    db.session_lock = MagicMock(return_value=asyncio.Lock())

    db_user = MagicMock()
    cb = MagicMock()
    cb.data = None
    cb.from_user = MagicMock(id=1)
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(grab_mod._search, "get_services", AsyncMock(return_value=(MagicMock(), MagicMock(qbittorrent=None))))
        await grab_mod.handle_season_preset(cb, SeasonPresetCB(preset="future"), db_user, db)

    assert session.monitor_type == "future"


# ---------------------------------------------------------------------------
# TrendingItemCB (trend_m:/trend_s:/trend_a:)
# ---------------------------------------------------------------------------


def test_trending_item_cb_roundtrip():
    from bot.ui.callbacks import TrendingItemCB

    packed = TrendingItemCB(kind="artist", item_id="3").pack()
    got = TrendingItemCB.unpack(packed)
    assert got.kind == "artist"
    assert got.item_id == "3"


def test_trending_lists_use_typed_trending_item_cb():
    from bot.ui.callbacks import TrendingItemCB
    from bot.ui.keyboards import Keyboards

    movies = [MovieInfo(title="M", tmdb_id=1, year=2020)]
    kb = Keyboards.trending_movies(movies)
    cbs = [c for c in _cbs(kb) if c.startswith("tri:")]
    assert len(cbs) == 1
    got = TrendingItemCB.unpack(cbs[0])
    assert got.kind == "movie" and got.item_id == "1"

    series = [SeriesInfo(title="S", tvdb_id=2, tmdb_id=2)]
    kb2 = Keyboards.trending_series(series)
    cbs2 = [c for c in _cbs(kb2) if c.startswith("tri:")]
    got2 = TrendingItemCB.unpack(cbs2[0])
    assert got2.kind == "series" and got2.item_id == "2"

    kb3 = Keyboards.trending_artists([{"name": "Artist0"}])
    cbs3 = [c for c in _cbs(kb3) if c.startswith("tri:")]
    got3 = TrendingItemCB.unpack(cbs3[0])
    assert got3.kind == "artist" and got3.item_id == "0"


@pytest.mark.asyncio
async def test_handle_movie_from_trending_reads_callback_data():
    from bot.handlers import trending
    from bot.ui.callbacks import TrendingItemCB

    movie = MovieInfo(title="The Matrix", tmdb_id=603, year=1999, poster_url=None)
    trending._cache_put(trending._trending_movies_cache, 603, movie)

    cb = MagicMock()
    cb.data = None
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.answer = AsyncMock()

    await trending.handle_movie_from_trending(cb, TrendingItemCB(kind="movie", item_id="603"))

    cb.message.answer.assert_awaited()
    trending._trending_movies_cache.pop(603, None)
    trending._trending_movies_inserted_at.pop(603, None)


@pytest.mark.asyncio
async def test_handle_trending_artist_click_reads_callback_data():
    from bot.handlers import music
    from bot.ui.callbacks import TrendingItemCB

    music._trending_artists_cache[7] = [{"name": "Metallica"}]

    db = AsyncMock()
    db_user = MagicMock()
    cb = MagicMock()
    cb.data = None
    cb.from_user = MagicMock(id=7)
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()

    mock_search = AsyncMock()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(music, "process_music_search", mock_search)
        await music.handle_trending_artist_click(cb, TrendingItemCB(kind="artist", item_id="0"), db_user, db)

    mock_search.assert_awaited()
    music._trending_artists_cache.pop(7, None)


# ---------------------------------------------------------------------------
# TorrentActionCB (t:/t_pause:/t_resume:/t_delete:/t_delf:/t_delfc:)
# ---------------------------------------------------------------------------


def test_torrent_action_cb_roundtrip():
    from bot.ui.callbacks import TorrentActionCB

    h = "a" * 40
    packed = TorrentActionCB(action="pause", h=h).pack()
    got = TorrentActionCB.unpack(packed)
    assert got.action == "pause"
    assert got.h == h
    # PERF-05: byte-budget guard for the worst-case action name + full hash.
    assert len(TorrentActionCB(action="delfc", h=h).pack().encode()) <= 64


def test_torrent_list_and_details_buttons_use_typed_action_cb():
    from bot.models import TorrentFilter, TorrentState
    from bot.ui.callbacks import TorrentActionCB
    from bot.ui.keyboards import Keyboards

    torrents = [TorrentInfo(hash=f"{'a' * 39}{i}", name=f"T{i}", progress=0.1) for i in range(2)]
    kb = Keyboards.torrent_list(torrents, current_page=0, total_pages=1, current_filter=TorrentFilter.ALL)
    cbs = [c for c in _cbs(kb) if c.startswith("ta:")]
    assert len(cbs) == 2
    for c in cbs:
        got = TorrentActionCB.unpack(c)
        assert got.action == "view"

    torrent = TorrentInfo(hash="b" * 40, name="T", progress=0.5, state=TorrentState.DOWNLOADING)
    details_kb = Keyboards.torrent_details(torrent, TorrentFilter.ALL)
    detail_cbs = [c for c in _cbs(details_kb) if c.startswith("ta:")]
    actions = {TorrentActionCB.unpack(c).action for c in detail_cbs}
    assert "pause" in actions
    assert "delete" in actions
    assert "delf" in actions


@pytest.mark.asyncio
async def test_handle_torrent_action_dispatches_by_action_field():
    from bot.handlers import downloads
    from bot.ui.callbacks import TorrentActionCB

    h = "c" * 40
    torrent = MagicMock(hash=h, name="Some.Torrent", state=None)
    qbt = AsyncMock()
    qbt.get_torrent = AsyncMock(return_value=torrent)
    qbt.get_torrent_by_short_hash = AsyncMock(return_value=torrent)
    qbt.pause = AsyncMock()

    cb = MagicMock()
    cb.data = None
    cb.message = MagicMock()
    cb.answer = AsyncMock()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(downloads, "check_qbt_enabled", AsyncMock(return_value=qbt))
        mp.setattr(downloads, "_render_torrent_details", AsyncMock())
        await downloads.handle_torrent_action(cb, TorrentActionCB(action="pause", h=h))

    qbt.pause.assert_awaited_with([h])


# ---------------------------------------------------------------------------
# CalCB (cal_7/cal_14/cal_30)
# ---------------------------------------------------------------------------


def test_cal_cb_roundtrip():
    from bot.ui.callbacks import CalCB

    packed = CalCB(days=14).pack()
    got = CalCB.unpack(packed)
    assert got.days == 14


def test_calendar_controls_keyboard_uses_typed_cb():
    from bot.ui.callbacks import CalCB
    from bot.ui.keyboards import Keyboards

    kb = Keyboards.calendar_controls(current_days=14)
    cal_cbs = [c for c in _cbs(kb) if c.startswith("cal:")]
    days = sorted(CalCB.unpack(c).days for c in cal_cbs)
    assert days == [7, 14, 30]


@pytest.mark.asyncio
async def test_handle_calendar_period_reads_callback_data():
    from bot.handlers import calendar
    from bot.ui.callbacks import CalCB

    cb = MagicMock()
    cb.data = None
    cb.from_user = MagicMock(id=1)
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()

    mock_fetch = AsyncMock()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(calendar, "_fetch_and_send_calendar", mock_fetch)
        await calendar.handle_calendar_period(cb, CalCB(days=30))

    mock_fetch.assert_awaited()
    assert mock_fetch.await_args.args[0] == 30
    assert calendar._user_period[1] == 30
    calendar._user_period.pop(1, None)
