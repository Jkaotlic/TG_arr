"""Audit 2026-09-23, Task C1: navigation semantics.

UI-07 «Закрыть» в /settings убивал сессию поиска; UI-04 «Назад» на карточке
из Топа с постером падал; UI-08 «Назад» в музыке → артисты стр. 1; UI-09
возврат терял страницу и фильтр; UI-17 выбор сезона — только 12 последних;
UI-18 «Обновить» календаря брал период из памяти.
"""

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import CallbackQuery
from aiogram.types import User as TgUser

from bot.models import (
    AlbumInfo,
    ArtistInfo,
    ContentType,
    MovieInfo,
    SearchSession,
    SeriesInfo,
    TorrentFilter,
    TorrentInfo,
    TorrentState,
    User,
)


# ---------------------------------------------------------------- helpers
def _buttons(kb):
    return [b for row in kb.inline_keyboard for b in row]


def _cb(user_id: int = 42) -> MagicMock:
    cb = MagicMock()
    cb.from_user = MagicMock(id=user_id)
    cb.message = MagicMock()
    cb.message.photo = None
    cb.message.edit_text = AsyncMock()
    cb.message.edit_reply_markup = AsyncMock()
    cb.message.delete = AsyncMock()
    cb.message.answer = AsyncMock()
    cb.answer = AsyncMock()
    return cb


def _db(session=None) -> MagicMock:
    db = MagicMock()
    db.get_session = AsyncMock(return_value=session)
    db.save_session = AsyncMock()
    db.delete_session = AsyncMock()
    db.log_action = AsyncMock()

    @contextlib.asynccontextmanager
    async def _lock(_uid):
        yield

    db.session_lock = _lock
    return db


async def _route(data: str):
    """The handler the real routers pick for ``data`` (setup_routers order)."""
    import bot.handlers as hp

    roots = [
        hp.start_router, hp.torrserver_router, hp.search_router, hp.music_router,
        hp.settings_router, hp.users_router, hp.status_router, hp.history_router,
        hp.downloads_router, hp.emby_router, hp.trending_router, hp.calendar_router,
        hp.titles_router,
    ]
    query = CallbackQuery(
        id="1", from_user=TgUser(id=42, is_bot=False, first_name="u"),
        chat_instance="c", data=data,
    )
    stack = list(roots)
    while stack:
        router = stack.pop(0)
        for h in router.callback_query.handlers:
            ok, kw = await h.check(query)
            if ok:
                return h, kw
        stack = list(router.sub_routers) + stack
    return None, {}


# ---------------------------------------------------------------- constants
def test_navigation_constants():
    from bot.ui.keyboards._constants import BTN_BACK, BTN_CANCEL, BTN_CLOSE, MSG_STALE, CallbackData
    from bot.ui.list_token import STALE_LIST_TEXT

    assert BTN_BACK == "◀️ Назад"
    assert BTN_CLOSE == "✖️ Закрыть"
    assert BTN_CANCEL == "❌ Отмена"
    assert MSG_STALE == "⌛ Список устарел — повторите поиск" == STALE_LIST_TEXT
    assert CallbackData.CLOSE == "close"


# ---------------------------------------------------------------- CLOSE (UI-07)
@pytest.mark.asyncio
async def test_close_deletes_message_and_keeps_session():
    from bot.ui.keyboards import CallbackData

    handler, _ = await _route(CallbackData.CLOSE)
    assert handler is not None, "no handler for CLOSE"
    cb = _cb()
    db = _db()
    await handler.callback(cb)
    cb.message.delete.assert_awaited_once()
    cb.answer.assert_awaited()
    db.delete_session.assert_not_called()


def test_settings_title_torrserver_close_with_close_not_cancel():
    from bot.models import UserPreferences
    from bot.ui.keyboards import CallbackData, Keyboards
    from bot.ui.keyboards._constants import BTN_CLOSE

    series = SeriesInfo(tvdb_id=1, title="S", sonarr_id=3, monitored=True)
    for kb in (
        Keyboards.settings_menu(prefs=UserPreferences()),
        Keyboards.title_actions(series),
        Keyboards.title_choices([series]),
        Keyboards.torrserver_panel(),
    ):
        buttons = _buttons(kb)
        assert all(b.callback_data != CallbackData.CANCEL for b in buttons)
        close = [b for b in buttons if b.callback_data == CallbackData.CLOSE]
        assert len(close) == 1 and close[0].text == BTN_CLOSE


def test_title_delete_cancel_returns_to_title_card():
    from bot.ui.callbacks import TitleActionCB
    from bot.ui.keyboards import Keyboards

    kb = Keyboards.confirm_title_delete("series", "3")
    back = _buttons(kb)[-1]
    assert TitleActionCB.unpack(back.callback_data) == TitleActionCB(action="pick", kind="series", title_id="3")


# ---------------------------------------------------------------- trending (UI-04)
@pytest.mark.asyncio
async def test_trending_back_from_photo_card_deletes_and_sends_list():
    from bot.handlers import trending

    cb = _cb()
    cb.message.photo = [MagicMock()]  # the card with a poster
    new_msg = MagicMock(edit_text=AsyncMock())
    cb.message.answer = AsyncMock(return_value=new_msg)
    tmdb = MagicMock()
    tmdb.get_trending_movies = AsyncMock(return_value=[MovieInfo(tmdb_id=1, title="M", year=2024)])

    with patch.object(trending, "get_tmdb", AsyncMock(return_value=tmdb)):
        await trending.handle_trending_movies(cb)

    cb.message.edit_text.assert_not_called()
    cb.message.delete.assert_awaited_once()
    assert new_msg.edit_text.await_args.kwargs.get("reply_markup") is not None


@pytest.mark.asyncio
async def test_trending_list_from_text_message_still_edits_in_place():
    from bot.handlers import trending

    cb = _cb()
    tmdb = MagicMock()
    tmdb.get_trending_series = AsyncMock(return_value=[SeriesInfo(tvdb_id=0, tmdb_id=2, title="S")])
    with patch.object(trending, "get_tmdb", AsyncMock(return_value=tmdb)):
        await trending.handle_trending_series(cb)
    cb.message.delete.assert_not_called()
    assert cb.message.edit_text.await_count >= 1


# ---------------------------------------------------------------- music (UI-08)
def _albums(n: int) -> list[AlbumInfo]:
    return [
        AlbumInfo(lidarr_id=100 + i, title=f"A{i}", release_date=f"20{10 + i:02d}-01-01")
        for i in range(n)
    ]


def test_album_sources_back_goes_to_album_page():
    from bot.ui.callbacks import AlbumPageCB
    from bot.ui.keyboards import Keyboards

    kb = Keyboards.album_sources(album_id=101, artist_id=7, page=2)
    back = [b for b in _buttons(kb) if b.text == "◀️ Назад"][0]
    assert AlbumPageCB.unpack(back.callback_data) == AlbumPageCB(page=2, artist_id=7)


@pytest.mark.asyncio
async def test_album_scope_source_choice_knows_album_page():
    from bot.handlers import music
    from bot.ui.callbacks import AlbumPageCB, AlbumScopeCB

    from bot.config import get_settings

    albums = _albums(12)  # newest first: A11..A0; A0 is the 12th → page 2
    music._album_candidates[42] = albums
    artist = ArtistInfo(mb_id="m", name="Art", lidarr_id=7)
    session = SearchSession(user_id=42, query="a", content_type=ContentType.MUSIC, selected_content=artist)
    cb = _cb()
    try:
        await music.handle_album_scope(cb, AlbumScopeCB(album_id=100, artist_id=7), User(tg_id=42), _db(session))
    finally:
        music._album_candidates.pop(42, None)

    kb = cb.message.edit_text.await_args.kwargs["reply_markup"]
    back = [b for b in _buttons(kb) if b.text == "◀️ Назад"][0]
    per_page = get_settings().results_per_page
    assert AlbumPageCB.unpack(back.callback_data) == AlbumPageCB(page=11 // per_page, artist_id=7)


@pytest.mark.asyncio
async def test_music_back_keeps_artist_list_page():
    from bot.config import get_settings
    from bot.handlers import music

    per_page = get_settings().results_per_page
    artists = [ArtistInfo(mb_id=f"m{i}", name=f"Art{i}") for i in range(per_page * 2 + 1)]
    chosen = artists[per_page + 1]  # page 1
    music._artist_candidates[42] = artists
    session = SearchSession(user_id=42, query="a", content_type=ContentType.MUSIC, selected_content=chosen)
    cb = _cb()
    try:
        with patch.object(music, "_render_artist_list", AsyncMock()) as render:
            await music.handle_music_back(cb, User(tg_id=42), _db(session))
    finally:
        music._artist_candidates.pop(42, None)
    assert render.await_args.kwargs.get("page") == 1


# ---------------------------------------------------------------- downloads (UI-09)
def _torrent(i: int = 0) -> TorrentInfo:
    return TorrentInfo(
        hash=f"{i:040x}", name=f"T{i}", size=1, progress=0.5, state=TorrentState.DOWNLOADING,
    )


def test_torrent_details_back_carries_filter_and_page():
    from bot.ui.callbacks import TorrentPageCB
    from bot.ui.keyboards import Keyboards

    kb = Keyboards.torrent_details(_torrent(), TorrentFilter.SEEDING, page=3)
    back = _buttons(kb)[-1]
    assert TorrentPageCB.unpack(back.callback_data) == TorrentPageCB(page=3, flt="seeding")


def test_filter_menu_marks_current_and_back_keeps_view():
    from bot.ui.callbacks import TorrentPageCB
    from bot.ui.keyboards import Keyboards

    kb = Keyboards.torrent_filters(TorrentFilter.COMPLETED, page=2)
    marked = [b.text for b in _buttons(kb) if b.text.startswith("•")]
    assert marked == ["• ✅ Готово"]
    back = _buttons(kb)[-1]
    assert TorrentPageCB.unpack(back.callback_data) == TorrentPageCB(page=2, flt="completed")


def test_speed_menu_back_keeps_view():
    from bot.ui.callbacks import TorrentPageCB
    from bot.ui.keyboards import Keyboards

    kb = Keyboards.speed_limits_menu(0, 0, current_filter=TorrentFilter.PAUSED, page=1)
    back = _buttons(kb)[-1]
    assert TorrentPageCB.unpack(back.callback_data) == TorrentPageCB(page=1, flt="paused")


@pytest.mark.asyncio
async def test_card_opened_from_filtered_page_returns_there():
    from bot.handlers import downloads
    from bot.ui.callbacks import TorrentActionCB, TorrentPageCB

    torrents = [_torrent(i) for i in range(12)]
    qbt = MagicMock()
    qbt.get_torrents = AsyncMock(return_value=torrents)
    qbt.get_torrent = AsyncMock(return_value=torrents[7])
    cb = _cb()
    with patch.object(downloads, "get_qbittorrent", AsyncMock(return_value=qbt)):
        await downloads.handle_torrent_page(cb, TorrentPageCB(page=1, flt="downloading"))
        cb2 = _cb()
        await downloads.handle_torrent_action(cb2, TorrentActionCB(action="view", h=torrents[7].hash))
        cb3 = _cb()
        await downloads.handle_filter_menu(cb3)

    kb = cb2.message.edit_text.await_args.kwargs["reply_markup"]
    assert TorrentPageCB.unpack(_buttons(kb)[-1].callback_data) == TorrentPageCB(page=1, flt="downloading")
    fkb = cb3.message.edit_text.await_args.kwargs["reply_markup"]
    assert [b.text for b in _buttons(fkb) if b.text.startswith("•")] == ["• ⬇️ Загрузка"]


# ---------------------------------------------------------------- /find & TorrServer back keeps page
def test_free_release_back_keeps_page():
    from bot.ui.callbacks import FindPageCB
    from bot.ui.keyboards import Keyboards

    kb = Keyboards.free_release(idx=7, list_token="t", page=1)
    back = [b for b in _buttons(kb) if b.text == "◀️ Назад"][0]
    assert FindPageCB.unpack(back.callback_data).page == 1


@pytest.mark.asyncio
async def test_free_release_card_back_is_the_page_of_the_release():
    from bot.config import get_settings
    from bot.handlers.search import free
    from bot.models import SearchResult
    from bot.ui.callbacks import FindPageCB, FindReleaseCB

    per_page = get_settings().results_per_page
    results = [SearchResult(guid=str(i), title=f"R{i}", indexer_id=1, size=1) for i in range(per_page * 2)]
    idx = per_page + 1
    session = SearchSession(
        user_id=42, query="q", content_type=ContentType.MOVIE, results=results,
        free_search=True, list_token="tok123",
    )
    cb = _cb()
    await free.handle_find_release(cb, FindReleaseCB(tok="tok123", idx=idx), User(tg_id=42), _db(session))
    kb = cb.message.edit_text.await_args.kwargs["reply_markup"]
    back = [b for b in _buttons(kb) if b.text == "◀️ Назад"][0]
    assert FindPageCB.unpack(back.callback_data).page == 1


def test_torrserver_release_back_keeps_page():
    from bot.ui.callbacks import TsPageCB
    from bot.ui.keyboards import Keyboards

    kb = Keyboards.torrserver_release(idx=7, list_token="t", page=1)
    back = _buttons(kb)[-1]
    assert TsPageCB.unpack(back.callback_data).page == 1
    assert back.text == "◀️ Назад"


# ---------------------------------------------------------------- season picker (UI-17)
def test_season_picker_pages_beyond_twelve():
    from bot.ui.callbacks import SeasonPageCB, SeasonScopeCB
    from bot.ui.keyboards import Keyboards

    seasons = list(range(1, 21))
    first = _buttons(Keyboards.season_scope(seasons, "9"))
    picked = [SeasonScopeCB.unpack(b.callback_data).season for b in first if b.callback_data.startswith("ss:")]
    assert picked[0] == 0 and 20 in picked and 9 in picked and 8 not in picked
    nxt = [b for b in first if b.text == "▶️"]
    assert SeasonPageCB.unpack(nxt[0].callback_data) == SeasonPageCB(page=1, title_id="9")

    second = _buttons(Keyboards.season_scope(seasons, "9", page=1))
    picked2 = {SeasonScopeCB.unpack(b.callback_data).season for b in second if b.callback_data.startswith("ss:")}
    assert set(range(1, 9)) <= picked2
    assert any(b.text == "◀️" for b in second)
    assert len(SeasonPageCB(page=99, title_id="9" * 12).pack()) <= 64


@pytest.mark.asyncio
async def test_season_page_handler_renders_requested_page():
    from bot.handlers.search import results as results_mod
    from bot.ui.callbacks import SeasonPageCB

    series = SeriesInfo(tvdb_id=1, title="S", sonarr_id=9, season_count=20,
                        seasons=[{"seasonNumber": n} for n in range(1, 21)])
    session = SearchSession(user_id=42, query="s", content_type=ContentType.SERIES, selected_content=series)
    cb = _cb()
    await results_mod.handle_season_page(cb, SeasonPageCB(page=1, title_id="9"), _db(session))
    kb = cb.message.edit_reply_markup.await_args.kwargs["reply_markup"]
    assert any(b.text == "S01" for b in _buttons(kb))


@pytest.mark.asyncio
async def test_season_page_for_another_title_is_stale():
    from bot.handlers.search import results as results_mod
    from bot.ui.callbacks import SeasonPageCB

    series = SeriesInfo(tvdb_id=1, title="S", sonarr_id=9, seasons=[{"seasonNumber": n} for n in range(1, 21)])
    session = SearchSession(user_id=42, query="s", content_type=ContentType.SERIES, selected_content=series)
    cb = _cb()
    await results_mod.handle_season_page(cb, SeasonPageCB(page=1, title_id="3"), _db(session))
    cb.message.edit_reply_markup.assert_not_called()
    assert cb.answer.await_args.kwargs.get("show_alert")


# ---------------------------------------------------------------- calendar (UI-18)
def test_calendar_refresh_carries_days():
    from bot.ui.callbacks import CalCB
    from bot.ui.keyboards import Keyboards

    kb = Keyboards.calendar_controls(current_days=14)
    refresh = [b for b in _buttons(kb) if "Обновить" in b.text][0]
    assert CalCB.unpack(refresh.callback_data).days == 14


@pytest.mark.asyncio
async def test_calendar_period_memory_evicts_oldest_not_everyone():
    from bot.handlers import calendar

    calendar._user_period.clear()
    for uid in range(calendar._MAX_USER_PERIOD_ENTRIES):
        calendar._user_period[uid] = 14
    msg = MagicMock(from_user=MagicMock(id=10_000), answer=AsyncMock())
    with patch.object(calendar, "_fetch_and_send_calendar", AsyncMock()):
        await calendar.handle_calendar_menu(msg)
    assert 0 not in calendar._user_period
    assert calendar._user_period[1] == 14
    assert 10_000 in calendar._user_period
    calendar._user_period.clear()
