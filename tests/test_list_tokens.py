"""Audit 2026-09-23, Task B2: list tokens against stale buttons.

GRAB-03 / UI-01 / UI-02: list buttons carried only an index into "the user's
current list" — after a new search the same index in the NEW list was grabbed
from a button in an OLD message. Every list now carries a short token; a
mismatch answers "Список устарел" and does nothing.
"""

import contextlib
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import CallbackQuery
from aiogram.types import User as TgUser

from bot.models import (
    ArtistInfo,
    ContentType,
    MovieInfo,
    SearchResult,
    SearchSession,
    SeriesInfo,
    User,
    UserPreferences,
)
from bot.ui.list_token import STALE_LIST_TEXT


# ---------------------------------------------------------------- helpers
def _rel(guid: str) -> SearchResult:
    return SearchResult(guid=guid, title=f"Release.{guid}", origin="arr", indexer_id=1, size=1)


def _movie() -> MovieInfo:
    return MovieInfo(tmdb_id=1, title="Movie", year=2024, radarr_id=15)


def _session(results, token: str, **kw) -> SearchSession:
    return SearchSession(
        user_id=42, query="q", content_type=kw.pop("content_type", ContentType.MOVIE),
        results=results, selected_content=kw.pop("selected_content", _movie()),
        list_token=token, **kw,
    )


def _db(session=None) -> MagicMock:
    db = MagicMock()
    db.get_session = AsyncMock(return_value=session)
    db.save_session = AsyncMock()
    db.update_session = AsyncMock(return_value=True)
    db.delete_session = AsyncMock()
    db.log_action = AsyncMock()

    @contextlib.asynccontextmanager
    async def _lock(_uid):
        yield

    db.session_lock = _lock
    return db


def _cb() -> MagicMock:
    cb = MagicMock()
    cb.from_user = MagicMock(id=42)
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.message.edit_reply_markup = AsyncMock()
    cb.answer = AsyncMock()
    return cb


def _user() -> User:
    return User(tg_id=42, preferences=UserPreferences(auto_grab_enabled=True))


def _answered_stale(cb) -> bool:
    return any(
        c.args and c.args[0] == STALE_LIST_TEXT and c.kwargs.get("show_alert")
        for c in cb.answer.await_args_list
    )


@pytest.fixture(autouse=True)
def _clear_guard():
    from bot.handlers import search

    search._grab_in_progress.clear()
    yield
    search._grab_in_progress.clear()


# ---------------------------------------------------------------- helper API
def test_new_token_is_six_base36_chars_and_varies():
    from bot.ui.list_token import new_list_token

    tokens = {new_list_token() for _ in range(50)}
    assert all(re.fullmatch(r"[0-9a-z]{6}", t) for t in tokens)
    assert len(tokens) > 1


def test_token_matches_rejects_empty_expected():
    from bot.ui.list_token import token_matches

    assert token_matches("abc123", "abc123")
    assert not token_matches("abc123", "zzz999")
    assert not token_matches("", "")
    assert not token_matches(None, "abc123")


def test_registry_issue_check_drop():
    from bot.ui.list_token import ListTokenRegistry

    reg = ListTokenRegistry(max_users=2)
    t1 = reg.issue(1)
    assert reg.check(1, t1) and reg.current(1) == t1
    t1b = reg.issue(1)
    assert not reg.check(1, t1) and reg.check(1, t1b)
    reg.issue(2)
    reg.issue(3)  # evicts user 1 (oldest)
    assert reg.current(1) == ""
    reg.drop(2)
    assert not reg.check(2, "x")


def test_packed_lengths_fit_telegram_limit():
    from bot.ui.callbacks import (
        AlbumGrabCB,
        FindGrabCB,
        FindReleaseCB,
        ForceGrabCB,
        GrabBestCB,
        GrabCB,
        MusicAddCB,
        ReleaseCB,
        SeasonScopeCB,
        SlskdCB,
        TsAddCB,
        TsReleaseCB,
    )

    tok = "zzzzzz"
    big = 499
    packed = [
        ReleaseCB(tok=tok, idx=big).pack(),
        GrabBestCB(tok=tok).pack(),
        GrabCB(tok=tok, idx=big, ok=1).pack(),
        ForceGrabCB(tok=tok, idx=big).pack(),
        MusicAddCB(tok=tok).pack(),
        FindReleaseCB(tok=tok, idx=big).pack(),
        FindGrabCB(tok=tok, idx=big).pack(),
        SlskdCB(tok=tok, idx=big).pack(),
        TsReleaseCB(tok=tok, idx=big).pack(),
        TsAddCB(tok=tok, idx=big).pack(),
        AlbumGrabCB(tok=tok, idx=big, album_id=2**31 - 1).pack(),
        SeasonScopeCB(season=99, title_id=str(2**31 - 1)).pack(),
    ]
    for p in packed:
        assert len(p.encode()) <= 64, p


# ---------------------------------------------------------------- catalogue flow
@pytest.mark.asyncio
async def test_old_release_button_after_a_new_search_does_nothing():
    """Search A, search B, press A's button → no selection, "устарел"."""
    from bot.handlers import search
    from bot.ui.callbacks import ReleaseCB

    session_b = _session([_rel("b0"), _rel("b1")], "bbbbbb")
    db = _db(session_b)
    cb = _cb()

    with patch.object(search, "get_services", AsyncMock(return_value=(MagicMock(), MagicMock()))):
        await search.handle_release_selection(cb, ReleaseCB(tok="aaaaaa", idx=1), _user(), db)

    assert _answered_stale(cb)
    db.save_session.assert_not_awaited()
    cb.message.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_old_grab_button_after_a_new_search_does_not_grab():
    from bot.handlers import search
    from bot.ui.callbacks import GrabCB

    session_b = _session([_rel("b0")], "bbbbbb", selected_result=_rel("b0"))
    db = _db(session_b)
    cb = _cb()

    with patch.object(search, "get_services", AsyncMock(return_value=(MagicMock(), MagicMock()))), \
         patch.object(search, "grab_release", AsyncMock()) as gr:
        await search.handle_confirm_grab(cb, GrabCB(tok="aaaaaa", idx=0), _user(), db)

    gr.assert_not_awaited()
    assert _answered_stale(cb)


@pytest.mark.asyncio
async def test_grab_button_grabs_the_release_its_index_names():
    """The card's index decides, not whatever `selected_result` points at now."""
    from bot.handlers import search
    from bot.ui.callbacks import GrabCB

    session = _session([_rel("r0"), _rel("r1")], "tok123", selected_result=_rel("r0"))
    db = _db(session)
    cb = _cb()

    with patch.object(search, "get_services", AsyncMock(return_value=(MagicMock(), MagicMock()))), \
         patch.object(search, "grab_release", AsyncMock()) as gr:
        await search.handle_confirm_grab(cb, GrabCB(tok="tok123", idx=1), _user(), db)

    gr.assert_awaited_once()
    grabbed_session = gr.await_args.args[1]
    assert grabbed_session.selected_result.guid == "r1"


@pytest.mark.asyncio
async def test_old_grab_best_button_does_not_grab():
    from bot.handlers import search
    from bot.ui.callbacks import GrabBestCB

    db = _db(_session([_rel("b0")], "bbbbbb"))
    cb = _cb()
    with patch.object(search, "get_services", AsyncMock(return_value=(MagicMock(), MagicMock()))), \
         patch.object(search, "grab_release", AsyncMock()) as gr:
        await search.handle_grab_best(cb, GrabBestCB(tok="aaaaaa"), _user(), db)

    gr.assert_not_awaited()
    assert _answered_stale(cb)


@pytest.mark.asyncio
async def test_old_force_grab_button_does_not_grab():
    from bot.handlers import search
    from bot.ui.callbacks import ForceGrabCB

    db = _db(_session([_rel("b0")], "bbbbbb", selected_result=_rel("b0")))
    cb = _cb()
    add_service = MagicMock()
    add_service.qbittorrent = MagicMock()
    with patch.object(search, "get_services", AsyncMock(return_value=(MagicMock(), add_service))), \
         patch.object(search, "_execute_grab", AsyncMock()) as ex:
        await search.handle_force_grab(cb, ForceGrabCB(tok="aaaaaa", idx=0), _user(), db)

    ex.assert_not_awaited()
    assert _answered_stale(cb)


@pytest.mark.asyncio
async def test_new_search_issues_a_new_token_and_buttons_carry_it():
    """_render_results_page puts the session's token into every list button."""
    from bot.handlers.search import services
    from bot.ui.callbacks import GrabBestCB, ReleaseCB

    results = [_rel("a"), _rel("b")]
    with patch.object(services, "safe_edit", AsyncMock()) as edit:
        await services._render_results_page(
            MagicMock(), results, 0, 1, "q", ContentType.MOVIE, 5, _user(),
            MagicMock(auto_grab_score_threshold=80), list_token="tok777",
        )
    markup = edit.await_args.kwargs["reply_markup"]
    datas = [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]
    rel = [d for d in datas if d.startswith("rel:")]
    assert rel and all(ReleaseCB.unpack(d).tok == "tok777" for d in rel)
    assert GrabBestCB(tok="tok777").pack() in datas


def test_release_card_buttons_carry_token_and_index():
    from bot.ui.callbacks import ForceGrabCB, GrabCB
    from bot.ui.keyboards import Keyboards

    kb = Keyboards.release_details(
        _rel("x"), ContentType.MOVIE, show_force_grab=True, list_token="tok123", idx=3,
    )
    datas = [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]
    assert GrabCB(tok="tok123", idx=3).pack() in datas
    assert ForceGrabCB(tok="tok123", idx=3).pack() in datas


@pytest.mark.asyncio
async def test_season_scope_for_another_title_is_stale():
    from bot.handlers import search
    from bot.handlers.search import results as results_mod
    from bot.ui.callbacks import SeasonScopeCB

    session = _session([], "", content_type=ContentType.SERIES,
                       selected_content=SeriesInfo(tvdb_id=1, title="New", sonarr_id=7))
    db = _db(session)
    cb = _cb()
    with patch.object(search, "process_search", AsyncMock()) as ps:
        await results_mod.handle_season_scope(cb, SeasonScopeCB(season=2, title_id="3"), _user(), db)

    ps.assert_not_awaited()
    assert _answered_stale(cb)


# ---------------------------------------------------------------- legacy data
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    ["rel:3", "confirm_grab", "grab_best", "force_grab", "sk:1", "tsa:0", "tsr:0",
     "fg:2", "fr:2", "alg:0:5"],
)
async def test_legacy_callback_data_gets_the_stale_answer(data):
    """Buttons from messages sent by the previous version: friendly alert, no action."""
    import bot.handlers as hp

    # Same order as setup_routers() (which can attach the routers only once).
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

    handler = None
    stack = list(roots)
    while stack and handler is None:
        router = stack.pop(0)
        for h in router.callback_query.handlers:
            ok, _kw = await h.check(query)
            if ok:
                handler = h
                break
        stack = list(router.sub_routers) + stack if handler is None else stack

    assert handler is not None, f"no handler for legacy {data!r}"
    cb = _cb()
    await handler.callback(cb)
    assert _answered_stale(cb)


# ---------------------------------------------------------------- free search
@pytest.mark.asyncio
async def test_free_search_old_grab_button_does_not_grab():
    from bot.handlers.search import free
    from bot.ui.callbacks import FindGrabCB

    session = SearchSession(user_id=42, query="q", content_type=ContentType.UNKNOWN,
                            results=[_rel("b0")], free_search=True, list_token="bbbbbb")
    db = _db(session)
    cb = _cb()
    add_service = MagicMock()
    add_service.grab_release = AsyncMock()
    with patch.object(free._search, "get_services", AsyncMock(return_value=(MagicMock(), add_service))):
        await free.handle_find_grab(cb, FindGrabCB(tok="aaaaaa", idx=0), _user(), db)

    add_service.grab_release.assert_not_awaited()
    assert _answered_stale(cb)


# ---------------------------------------------------------------- music
@pytest.mark.asyncio
async def test_old_soulseek_button_after_a_new_search_does_not_enqueue():
    from bot.handlers import music
    from bot.ui.callbacks import SlskdCB

    music._slskd_candidates[42] = [MagicMock()]
    new_tok = music._slskd_tokens.issue(42)
    slskd = MagicMock()
    slskd.enqueue = AsyncMock(return_value=True)
    cb = _cb()
    with patch.object(music, "get_slskd", AsyncMock(return_value=slskd)):
        await music.handle_slskd_selection(cb, SlskdCB(tok="old000", idx=0), _user(), _db())

    assert new_tok != "old000"
    slskd.enqueue.assert_not_awaited()
    assert _answered_stale(cb)
    music._slskd_candidates.pop(42, None)


@pytest.mark.asyncio
async def test_soulseek_double_tap_enqueues_once():
    import asyncio

    from bot.handlers import music
    from bot.ui.callbacks import SlskdCB

    cand = MagicMock(username="peer1", files=[], display_name="A", folder="f",
                     track_count=1, size_formatted="1 MB")
    music._slskd_candidates[42] = [cand]
    tok = music._slskd_tokens.issue(42)
    gate = asyncio.Event()

    async def slow_enqueue(*a, **k):
        await gate.wait()
        return True

    slskd = MagicMock()
    slskd.enqueue = AsyncMock(side_effect=slow_enqueue)

    async def open_gate():
        for _ in range(10):
            await asyncio.sleep(0)
        gate.set()

    with patch.object(music, "get_slskd", AsyncMock(return_value=slskd)):
        await asyncio.gather(
            music.handle_slskd_selection(_cb(), SlskdCB(tok=tok, idx=0), _user(), _db()),
            music.handle_slskd_selection(_cb(), SlskdCB(tok=tok, idx=0), _user(), _db()),
            open_gate(),
        )

    assert slskd.enqueue.await_count == 1
    music._slskd_candidates.pop(42, None)


@pytest.mark.asyncio
async def test_album_grab_from_a_non_music_session_is_stale():
    from bot.handlers import music
    from bot.ui.callbacks import AlbumGrabCB

    session = _session([_rel("m0")], "tok123")  # a movie session with the same token
    db = _db(session)
    cb = _cb()
    add_service = MagicMock()
    add_service.grab_release = AsyncMock()
    with patch.object(music, "_get_music_services", AsyncMock(return_value=(MagicMock(), add_service))):
        await music.handle_album_grab(cb, AlbumGrabCB(tok="tok123", idx=0, album_id=5), _user(), db)

    add_service.grab_release.assert_not_awaited()
    assert _answered_stale(cb)


@pytest.mark.asyncio
async def test_album_grab_with_an_old_token_is_stale():
    from bot.handlers import music
    from bot.ui.callbacks import AlbumGrabCB

    artist = ArtistInfo(mb_id="mb", name="A", lidarr_id=1)
    session = SearchSession(user_id=42, query="q", content_type=ContentType.MUSIC,
                            results=[_rel("m0")], selected_content=artist, list_token="newtok")
    db = _db(session)
    cb = _cb()
    add_service = MagicMock()
    add_service.grab_release = AsyncMock()
    with patch.object(music, "_get_music_services", AsyncMock(return_value=(MagicMock(), add_service))):
        await music.handle_album_grab(cb, AlbumGrabCB(tok="oldtok", idx=0, album_id=5), _user(), db)

    add_service.grab_release.assert_not_awaited()
    assert _answered_stale(cb)


@pytest.mark.asyncio
async def test_music_add_from_an_old_artist_card_is_stale():
    from bot.handlers import music
    from bot.ui.callbacks import MusicAddCB

    artist = ArtistInfo(mb_id="mb", name="B", lidarr_id=1)
    session = SearchSession(user_id=42, query="q", content_type=ContentType.MUSIC,
                            selected_content=artist, list_token="newtok")
    db = _db(session)
    cb = _cb()
    with patch.object(music, "_get_music_services", AsyncMock()) as svc:
        await music.handle_music_add(cb, MusicAddCB(tok="oldtok"), _user(), db)

    svc.assert_not_awaited()
    assert _answered_stale(cb)


def test_artist_card_uses_music_add_not_confirm_grab():
    from bot.ui.callbacks import MusicAddCB
    from bot.ui.keyboards import Keyboards

    kb = Keyboards.artist_details(ArtistInfo(mb_id="mb", name="A"), list_token="tok123")
    datas = [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]
    assert MusicAddCB(tok="tok123").pack() in datas
    assert "confirm_grab" not in datas


# ---------------------------------------------------------------- torrserver
@pytest.mark.asyncio
async def test_old_torrserver_add_button_does_not_add():
    from bot.handlers import torrserver as ts
    from bot.ui.callbacks import TsAddCB

    ts._results[42] = [MagicMock(link="magnet:?xt=urn:btih:" + "a" * 40, title="t")]
    ts._tokens.issue(42)
    service = MagicMock()
    service.add_and_publish = AsyncMock()
    cb = _cb()
    with patch.object(ts, "get_torrserver_service", AsyncMock(return_value=service)):
        await ts.handle_add(cb, TsAddCB(tok="old000", idx=0))

    service.add_and_publish.assert_not_awaited()
    assert _answered_stale(cb)
    ts._results.pop(42, None)
