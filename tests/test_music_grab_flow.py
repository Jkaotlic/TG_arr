"""Audit 2026-09-23 B7: the music grab flow (GRAB-15, GRAB-16, GRAB-18,
UI-03, UI-10, UI-11).

- Album torrent releases keep Lidarr's order (accepted first, then rejected;
  no re-sort by seeders) and page 5 at a time with ◀️ N/M ▶️ and "◀️ Назад"
  to the source choice.
- A row tap opens a card; the card's button grabs. A ⛔ release asks
  "⚠️ Всё равно скачать" (B1's rejected-confirm pattern).
- Soulseek results page too, open a card before enqueue, and the enqueue is
  claimed and shows "⏳…" before the API call.
- Soulseek ranking: a complete album before format; m4a with lossless signs
  (bit depth / high bitrate) ranks as ALAC.
- "💿 Вся дискография" asks first, then claims, and reports a failure.
"""

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.models import (
    ActionLog,
    ActionType,
    AlbumInfo,
    ArtistInfo,
    ContentType,
    QualityInfo,
    SearchResult,
    SearchSession,
    SlskdFile,
    SlskdSearchResult,
    User,
    UserPreferences,
)

USER_ID = 111
TOK = "tok123"


# ------------------------------------------------------------------ fixtures
def _callback() -> MagicMock:
    cb = MagicMock()
    cb.answer = AsyncMock()
    cb.from_user = MagicMock(id=USER_ID)
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    return cb


def _db(session=None) -> MagicMock:
    db = MagicMock()
    db.get_session = AsyncMock(return_value=session)
    db.save_session = AsyncMock()
    db.update_session = AsyncMock(return_value=True)
    db.delete_session = AsyncMock()
    db.log_action = AsyncMock()

    @contextlib.asynccontextmanager
    async def _lock(_user_id):
        yield

    db.session_lock = _lock
    return db


def _user() -> User:
    return User(tg_id=USER_ID, preferences=UserPreferences())


def _artist() -> ArtistInfo:
    return ArtistInfo(mb_id="mb-1", name="Enter Shikari", lidarr_id=7)


def _rel(guid: str, *, rejected=False, seeders=1, codec="FLAC") -> SearchResult:
    return SearchResult(
        guid=guid, title=f"Enter Shikari - Album {guid}", origin="arr", indexer_id=4,
        size=300 * 1024 ** 2, seeders=seeders, rejected=rejected,
        rejections=["Album wasn't requested"] if rejected else [],
        quality=QualityInfo(codec=codec),
    )


def _session(results=None, tok=TOK) -> SearchSession:
    return SearchSession(
        user_id=USER_ID, query="Enter Shikari", content_type=ContentType.MUSIC,
        selected_content=_artist(), results=results or [], list_token=tok,
    )


def _grab_action(ok=True) -> ActionLog:
    return ActionLog(
        user_id=0, action_type=ActionType.GRAB, content_type=ContentType.MUSIC, success=ok,
    )


@pytest.fixture(autouse=True)
def _clean_state():
    from bot.handlers import music, search

    music._album_candidates.clear()
    music._slskd_candidates.clear()
    search._grab_in_progress.clear()
    music._album_candidates[USER_ID] = [AlbumInfo(lidarr_id=3, title="The Mindsweep")]
    yield
    music._album_candidates.clear()
    music._slskd_candidates.clear()
    music._slskd_tokens.drop(USER_ID)
    search._grab_in_progress.clear()


def _services(add_service):
    from bot.handlers import music

    return patch.object(music, "_get_music_services", AsyncMock(return_value=(MagicMock(), add_service)))


def _texts(kb) -> list[str]:
    return [b.text for row in kb.inline_keyboard for b in row]


def _datas(kb) -> list[str]:
    return [b.callback_data for row in kb.inline_keyboard for b in row]


# --------------------------------------------------------- GRAB-15 order
@pytest.mark.asyncio
async def test_album_releases_keep_lidarr_order_rejected_last():
    from bot.handlers import music
    from bot.ui.callbacks import AlbumSourceCB

    add_service = MagicMock()
    add_service.lidarr.get_releases = AsyncMock(return_value=[
        _rel("bad", rejected=True, seeders=99), _rel("first", seeders=2), _rel("second", seeders=40),
    ])
    cb = _callback()
    db = _db(_session())
    with _services(add_service), patch.object(music, "accessible_message", return_value=cb.message):
        await music.handle_album_source(cb, AlbumSourceCB(album_id=3, source="tor"), _user(), db)

    saved = db.update_session.await_args.args[1]
    assert [r.guid for r in saved.results] == ["first", "second", "bad"]


# --------------------------------------------------------- UI-10 pagination
def _twelve():
    return [_rel(f"r{i}") for i in range(12)]


def test_album_release_keyboard_pages_with_back_to_sources():
    from bot.ui.callbacks import AlbumRelPageCB, AlbumReleaseCB, AlbumScopeCB
    from bot.ui.keyboards import Keyboards

    kb = Keyboards.album_releases(_twelve(), album_id=3, page=2, per_page=5, list_token=TOK, artist_id=7)
    rows = [AlbumReleaseCB.unpack(d) for d in _datas(kb) if d.startswith("alr:")]
    assert [r.idx for r in rows] == [10, 11]
    assert "3/3" in _texts(kb)
    assert AlbumRelPageCB(tok=TOK, page=1, album_id=3).pack() in _datas(kb)
    assert "◀️ Назад" in _texts(kb)
    assert AlbumScopeCB(album_id=3, artist_id=7).pack() in _datas(kb)


@pytest.mark.asyncio
async def test_album_release_page_handler_renders_the_page():
    from bot.handlers import music
    from bot.ui.callbacks import AlbumRelPageCB, AlbumReleaseCB

    cb = _callback()
    with patch.object(music, "accessible_message", return_value=cb.message):
        await music.handle_album_release_page(
            cb, AlbumRelPageCB(tok=TOK, page=1, album_id=3), _user(), _db(_session(_twelve())),
        )

    kb = cb.message.edit_text.await_args.kwargs["reply_markup"]
    idxs = [AlbumReleaseCB.unpack(d).idx for d in _datas(kb) if d.startswith("alr:")]
    assert idxs == [5, 6, 7, 8, 9]


@pytest.mark.asyncio
async def test_album_release_page_out_of_bounds_or_stale():
    from bot.handlers import music
    from bot.ui.callbacks import AlbumRelPageCB

    cb = _callback()
    with patch.object(music, "accessible_message", return_value=cb.message):
        await music.handle_album_release_page(
            cb, AlbumRelPageCB(tok=TOK, page=3, album_id=3), _user(), _db(_session(_twelve())),
        )
        await music.handle_album_release_page(
            cb, AlbumRelPageCB(tok="old000", page=0, album_id=3), _user(), _db(_session(_twelve())),
        )
    cb.message.edit_text.assert_not_awaited()
    assert cb.answer.await_count == 2


# --------------------------------------------------------- UI-03 card first
@pytest.mark.asyncio
async def test_row_tap_opens_a_card_instead_of_grabbing():
    from bot.handlers import music
    from bot.ui.callbacks import AlbumGrabCB, AlbumReleaseCB

    add_service = MagicMock()
    add_service.grab_release = AsyncMock()
    cb = _callback()
    with _services(add_service), patch.object(music, "accessible_message", return_value=cb.message):
        await music.handle_album_release_card(
            cb, AlbumReleaseCB(tok=TOK, idx=6, album_id=3), _user(), _db(_session(_twelve())),
        )

    add_service.grab_release.assert_not_awaited()
    text = cb.message.edit_text.await_args.args[0]
    kb = cb.message.edit_text.await_args.kwargs["reply_markup"]
    assert "FLAC" in text and "300.0 MB" in text
    assert "✅ Скачать" in _texts(kb)
    assert AlbumGrabCB(tok=TOK, idx=6, album_id=3, ok=0).pack() in _datas(kb)


@pytest.mark.asyncio
async def test_rejected_release_card_asks_to_take_it_anyway():
    from bot.handlers import music
    from bot.ui.callbacks import AlbumGrabCB, AlbumReleaseCB

    cb = _callback()
    with patch.object(music, "accessible_message", return_value=cb.message):
        await music.handle_album_release_card(
            cb, AlbumReleaseCB(tok=TOK, idx=0, album_id=3), _user(), _db(_session([_rel("x", rejected=True)])),
        )

    text = cb.message.edit_text.await_args.args[0]
    kb = cb.message.edit_text.await_args.kwargs["reply_markup"]
    assert "Album wasn&#x27;t requested" in text  # html-escaped
    assert "⚠️ Всё равно скачать" in _texts(kb)
    assert "✅ Скачать" not in _texts(kb)
    assert AlbumGrabCB(tok=TOK, idx=0, album_id=3, ok=1).pack() in _datas(kb)


@pytest.mark.asyncio
async def test_rejected_release_is_not_grabbed_without_ok():
    from bot.handlers import music
    from bot.ui.callbacks import AlbumGrabCB

    add_service = MagicMock()
    add_service.lidarr = AsyncMock()
    add_service.grab_release = AsyncMock(return_value=(True, _grab_action()))
    session = _session([_rel("x", rejected=True)])
    cb = _callback()
    with _services(add_service), patch.object(music, "accessible_message", return_value=cb.message):
        await music.handle_album_grab(cb, AlbumGrabCB(tok=TOK, idx=0, album_id=3), _user(), _db(session))
    add_service.grab_release.assert_not_awaited()

    cb = _callback()
    with _services(add_service), patch.object(music, "accessible_message", return_value=cb.message):
        await music.handle_album_grab(cb, AlbumGrabCB(tok=TOK, idx=0, album_id=3, ok=1), _user(), _db(session))
    add_service.grab_release.assert_awaited_once()


# --------------------------------------------------------- GRAB-16 double tap
@pytest.mark.asyncio
async def test_album_grab_double_tap_grabs_once_and_shows_progress_first():
    from bot.handlers import music
    from bot.ui.callbacks import AlbumGrabCB

    events: list[str] = []

    async def slow_grab(*_a, **_kw):
        events.append("grab")
        await asyncio.sleep(0.05)
        return True, _grab_action()

    add_service = MagicMock()
    add_service.lidarr = AsyncMock()
    add_service.grab_release = AsyncMock(side_effect=slow_grab)
    session = _session([_rel("a")])

    def make_cb():
        cb = _callback()

        async def edit(text, **_kw):
            events.append(text)
        cb.message.edit_text = AsyncMock(side_effect=edit)
        return cb

    cb1, cb2 = make_cb(), make_cb()
    with _services(add_service), patch.object(music, "accessible_message", side_effect=lambda c: c.message):
        await asyncio.gather(
            music.handle_album_grab(cb1, AlbumGrabCB(tok=TOK, idx=0, album_id=3), _user(), _db(session)),
            music.handle_album_grab(cb2, AlbumGrabCB(tok=TOK, idx=0, album_id=3), _user(), _db(session)),
        )

    assert add_service.grab_release.await_count == 1
    first_progress = next(i for i, e in enumerate(events) if e.startswith("⏳"))
    assert first_progress < events.index("grab")


@pytest.mark.asyncio
async def test_album_grab_exception_is_reported_to_the_user():
    from bot.handlers import music
    from bot.ui.callbacks import AlbumGrabCB

    add_service = MagicMock()
    add_service.lidarr = AsyncMock()
    add_service.grab_release = AsyncMock(side_effect=RuntimeError("boom"))
    cb = _callback()
    with _services(add_service), patch.object(music, "accessible_message", return_value=cb.message):
        await music.handle_album_grab(cb, AlbumGrabCB(tok=TOK, idx=0, album_id=3), _user(), _db(_session([_rel("a")])))

    assert "❌" in cb.message.edit_text.await_args.args[0]
    add_service.lidarr.set_album_monitored.assert_not_awaited()


# --------------------------------------------------------- Soulseek
def _cand(n: int, *, fmt="flac", tracks=10, user="peer") -> SlskdSearchResult:
    return SlskdSearchResult(
        username=f"{user}{n}", folder=f"Artist - Album {n}", has_free_slot=True,
        files=[SlskdFile(filename=f"{n}\\{t}.{fmt}", name=f"{t}.{fmt}", size=1000, extension=fmt)
               for t in range(tracks)],
    )


def _seed_slskd(cands):
    from bot.handlers import music

    music._slskd_candidates[USER_ID] = cands
    music._slskd_queries[USER_ID] = "Artist Album"
    return music._slskd_tokens.issue(USER_ID)


def test_slskd_keyboard_pages_and_opens_cards():
    from bot.ui.callbacks import SlskdCardCB, SlskdPageCB
    from bot.ui.keyboards import Keyboards

    kb = Keyboards.slskd_results([_cand(i) for i in range(12)], page=1, per_page=5, list_token=TOK)
    idxs = [SlskdCardCB.unpack(d).idx for d in _datas(kb) if d.startswith("skc:")]
    assert idxs == [5, 6, 7, 8, 9]
    assert "2/3" in _texts(kb)
    assert SlskdPageCB(tok=TOK, page=0).pack() in _datas(kb)
    assert SlskdPageCB(tok=TOK, page=2).pack() in _datas(kb)


@pytest.mark.asyncio
async def test_slskd_page_handler_bounds_and_token():
    from bot.handlers import music
    from bot.ui.callbacks import SlskdPageCB

    tok = _seed_slskd([_cand(i) for i in range(12)])
    cb = _callback()
    with patch.object(music, "accessible_message", return_value=cb.message):
        await music.handle_slskd_page(cb, SlskdPageCB(tok=tok, page=3), _user(), _db())
        await music.handle_slskd_page(cb, SlskdPageCB(tok="old000", page=1), _user(), _db())
    cb.message.edit_text.assert_not_awaited()

    with patch.object(music, "accessible_message", return_value=cb.message):
        await music.handle_slskd_page(cb, SlskdPageCB(tok=tok, page=2), _user(), _db())
    text = cb.message.edit_text.await_args.args[0]
    assert "11." in text and "12." in text and "1." not in text.split("\n", 2)[-1][:3]


@pytest.mark.asyncio
async def test_slskd_row_opens_a_card_without_enqueueing():
    from bot.handlers import music
    from bot.ui.callbacks import SlskdCardCB, SlskdCB

    tok = _seed_slskd([_cand(i) for i in range(3)])
    slskd = MagicMock()
    slskd.enqueue = AsyncMock(return_value=True)
    cb = _callback()
    with patch.object(music, "get_slskd", AsyncMock(return_value=slskd)), \
            patch.object(music, "accessible_message", return_value=cb.message):
        await music.handle_slskd_card(cb, SlskdCardCB(tok=tok, idx=2), _user(), _db())

    slskd.enqueue.assert_not_awaited()
    text = cb.message.edit_text.await_args.args[0]
    kb = cb.message.edit_text.await_args.kwargs["reply_markup"]
    assert "Artist - Album 2" in text and "FLAC" in text
    assert SlskdCB(tok=tok, idx=2).pack() in _datas(kb)


@pytest.mark.asyncio
async def test_slskd_enqueue_shows_progress_before_the_api_and_reports_errors():
    from bot.handlers import music
    from bot.ui.callbacks import SlskdCB

    tok = _seed_slskd([_cand(0)])
    events: list[str] = []

    async def enqueue(*_a):
        events.append("enqueue")
        raise RuntimeError("peer went away")

    slskd = MagicMock()
    slskd.enqueue = AsyncMock(side_effect=enqueue)
    cb = _callback()

    async def edit(text, **_kw):
        events.append(text)
    cb.message.edit_text = AsyncMock(side_effect=edit)

    with patch.object(music, "get_slskd", AsyncMock(return_value=slskd)), \
            patch.object(music, "accessible_message", return_value=cb.message):
        await music.handle_slskd_selection(cb, SlskdCB(tok=tok, idx=0), _user(), _db())

    assert events[0].startswith("⏳")
    assert events.index("enqueue") > 0
    assert "❌" in events[-1]


# --------------------------------------------------------- GRAB-18 ranking
def test_complete_mp3_album_ranks_above_a_single_flac_track():
    from bot.clients.slskd import rank_candidates

    single_flac = _cand(1, fmt="flac", tracks=1)
    full_mp3 = _cand(2, fmt="mp3", tracks=11)
    ranked = rank_candidates([single_flac, full_mp3], prefer_complete=True)
    assert ranked[0] is full_mp3


def test_track_search_ranks_by_format_not_completeness():
    from bot.clients.slskd import rank_candidates

    single_flac = _cand(1, fmt="flac", tracks=1)
    full_mp3 = _cand(2, fmt="mp3", tracks=11)
    assert rank_candidates([full_mp3, single_flac], prefer_complete=False)[0] is single_flac


def test_m4a_with_lossless_signs_ranks_as_alac():
    from bot.clients.slskd import rank_candidates

    alac = _cand(1, fmt="m4a", tracks=10)
    for f in alac.files:
        f.bit_depth = 16
    aac = _cand(2, fmt="m4a", tracks=10)
    for f in aac.files:
        f.bitrate = 256
    mp3 = _cand(3, fmt="mp3", tracks=10)
    ranked = rank_candidates([mp3, aac, alac], prefer_complete=True)
    assert ranked[0] is alac
    assert ranked.index(aac) < ranked.index(mp3), "unknown m4a is still ≥ mp3"
    assert alac.effective_format == "alac"
    assert aac.effective_format == "m4a"


def test_client_flatten_uses_the_album_ranking():
    from bot.clients.slskd import SlskdClient

    client = SlskdClient("http://slskd.example.com", "key", search_timeout=0.1)
    responses = [
        {"username": "one", "hasFreeUploadSlot": True, "files": [
            {"filename": "A\\Artist - Album\\01.flac", "size": 10},
        ]},
        {"username": "full", "hasFreeUploadSlot": True, "files": [
            {"filename": f"B\\Artist - Album\\{i:02}.mp3", "size": 10} for i in range(10)
        ]},
    ]
    assert client._flatten_responses(responses)[0].username == "full"


# --------------------------------------------------------- UI-11 discography
@pytest.mark.asyncio
async def test_whole_discography_asks_first():
    from bot.handlers import music
    from bot.ui.callbacks import AlbumScopeCB, DiscographyCB

    add_service = MagicMock()
    add_service.lidarr = AsyncMock()
    cb = _callback()
    db = _db(_session())
    with _services(add_service), patch.object(music, "accessible_message", return_value=cb.message):
        await music.handle_album_scope(cb, AlbumScopeCB(album_id=0, artist_id=7), _user(), db)

    add_service.lidarr.set_artist_monitored.assert_not_awaited()
    add_service.lidarr.search_artist.assert_not_awaited()
    saved = db.save_session.await_args.args[1]
    kb = cb.message.edit_text.await_args.kwargs["reply_markup"]
    fp = music._artist_fingerprint(_artist())  # round 2 / D3
    assert DiscographyCB(tok=saved.list_token, artist_id=7, fp=fp).pack() in _datas(kb)
    assert "◀️ Назад" in _texts(kb)


@pytest.mark.asyncio
async def test_confirmed_discography_runs_once_on_double_tap():
    from bot.handlers import music
    from bot.ui.callbacks import DiscographyCB

    async def slow(*_a):
        await asyncio.sleep(0.05)

    add_service = MagicMock()
    add_service.lidarr = MagicMock()
    add_service.lidarr.set_artist_monitored = AsyncMock(side_effect=slow)
    add_service.lidarr.search_artist = AsyncMock()
    session = _session()
    with _services(add_service), patch.object(music, "accessible_message", side_effect=lambda c: c.message):
        await asyncio.gather(
            music.handle_discography(_callback(), DiscographyCB(tok=TOK, artist_id=7), _user(), _db(session)),
            music.handle_discography(_callback(), DiscographyCB(tok=TOK, artist_id=7), _user(), _db(session)),
        )
    add_service.lidarr.search_artist.assert_awaited_once_with(7)


@pytest.mark.asyncio
async def test_discography_stale_token_or_other_artist_does_nothing():
    from bot.handlers import music
    from bot.ui.callbacks import DiscographyCB

    add_service = MagicMock()
    add_service.lidarr = AsyncMock()
    for cbd in (DiscographyCB(tok="old000", artist_id=7), DiscographyCB(tok=TOK, artist_id=8)):
        cb = _callback()
        with _services(add_service), patch.object(music, "accessible_message", return_value=cb.message):
            await music.handle_discography(cb, cbd, _user(), _db(_session()))
        assert cb.answer.await_args.kwargs.get("show_alert") is True
    add_service.lidarr.search_artist.assert_not_awaited()


@pytest.mark.asyncio
async def test_discography_failure_is_shown_to_the_user():
    from bot.handlers import music
    from bot.ui.callbacks import DiscographyCB

    add_service = MagicMock()
    add_service.lidarr = MagicMock()
    add_service.lidarr.set_artist_monitored = AsyncMock(side_effect=RuntimeError("lidarr down"))
    add_service.lidarr.search_artist = AsyncMock()
    cb = _callback()
    with _services(add_service), patch.object(music, "accessible_message", return_value=cb.message):
        await music.handle_discography(cb, DiscographyCB(tok=TOK, artist_id=7), _user(), _db(_session()))

    assert "❌" in cb.message.edit_text.await_args.args[0]
    assert cb.message.edit_text.await_args_list[0].args[0].startswith("⏳")


# --------------------------------------------------------- callback budget
def test_new_music_callbacks_fit_64_bytes():
    from bot.ui.callbacks import (
        AlbumGrabCB,
        AlbumRelPageCB,
        AlbumReleaseCB,
        DiscographyCB,
        SlskdCardCB,
        SlskdPageCB,
    )

    tok, big, aid = "zzzzzz", 499, 2 ** 31 - 1
    for packed in (
        AlbumGrabCB(tok=tok, idx=big, album_id=aid, ok=1).pack(),
        AlbumReleaseCB(tok=tok, idx=big, album_id=aid).pack(),
        AlbumRelPageCB(tok=tok, page=big, album_id=aid).pack(),
        SlskdCardCB(tok=tok, idx=big).pack(),
        SlskdPageCB(tok=tok, page=big).pack(),
        DiscographyCB(tok=tok, artist_id=aid).pack(),
    ):
        assert len(packed.encode()) <= 64, packed


@pytest.mark.asyncio
async def test_old_album_grab_button_without_ok_field_is_stale():
    """A B1-era ``alg:tok:idx:album`` button must not grab from the card-less
    row it sat on."""
    from bot.handlers import music
    from bot.ui.callbacks import AlbumGrabCB

    with pytest.raises((TypeError, ValueError)):
        AlbumGrabCB.unpack(f"alg:{TOK}:0:3")
    cb = _callback()
    cb.data = f"alg:{TOK}:0:3"
    await music.handle_legacy_music_pick(cb)
    assert cb.answer.await_args.kwargs.get("show_alert") is True


def test_music_handlers_are_registered():
    from bot.handlers.music import router

    names = {h.callback.__name__ for h in router.callback_query.handlers}
    assert {
        "handle_album_release_page", "handle_album_release_card", "handle_slskd_page",
        "handle_slskd_card", "handle_discography",
    } <= names
