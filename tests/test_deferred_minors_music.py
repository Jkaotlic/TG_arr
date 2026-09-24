"""Round 2 / D3 (music part): Soulseek completeness vs the median folder,
album-scope session save under the lock, discography confirm identity when
the artist has no Lidarr id."""

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.models import ArtistInfo, ContentType, SearchSession, SlskdFile, SlskdSearchResult, User, UserPreferences

USER_ID = 222


def _cand(n: int, *, fmt: str, tracks: int) -> SlskdSearchResult:
    return SlskdSearchResult(
        username=f"peer{n}", folder=f"Artist - Album {n}", has_free_slot=True,
        files=[SlskdFile(filename=f"{n}\\{t}.{fmt}", name=f"{t}.{fmt}", size=1000, extension=fmt)
               for t in range(tracks)],
    )


# ------------------------------------------------------------ slskd median
def test_discography_outlier_does_not_demote_normal_albums():
    """One 60-track discography folder used to set the bar (80% of 60 = 48),
    so every normal 12-track album counted as incomplete and the outlier won.
    Against the median (12) all three are complete → format decides."""
    from bot.clients.slskd import rank_candidates

    outlier = _cand(1, fmt="mp3", tracks=60)
    album_flac = _cand(2, fmt="flac", tracks=12)
    album_mp3 = _cand(3, fmt="mp3", tracks=12)
    ranked = rank_candidates([outlier, album_mp3, album_flac], prefer_complete=True)
    assert ranked[0] is album_flac


def test_single_tracks_do_not_drag_the_median_down():
    """GRAB-18 must survive: stray single-track folders don't make a lone
    FLAC track "complete" and let it beat a whole MP3 album."""
    from bot.clients.slskd import rank_candidates

    singles = [_cand(i, fmt="flac", tracks=1) for i in range(1, 4)]
    album_mp3 = _cand(9, fmt="mp3", tracks=11)
    ranked = rank_candidates([*singles, album_mp3], prefer_complete=True)
    assert ranked[0] is album_mp3


def test_partial_folder_below_median_is_still_incomplete():
    from bot.clients.slskd import rank_candidates

    partial_flac = _cand(1, fmt="flac", tracks=5)
    albums = [_cand(i, fmt="mp3", tracks=12) for i in range(2, 5)]
    ranked = rank_candidates([partial_flac, *albums], prefer_complete=True)
    assert ranked[-1] is partial_flac


# ------------------------------------------------------- shared fixtures
def _callback() -> MagicMock:
    cb = MagicMock()
    cb.answer = AsyncMock()
    cb.from_user = MagicMock(id=USER_ID)
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    return cb


def _user() -> User:
    return User(tg_id=USER_ID, preferences=UserPreferences())


def _session(artist: ArtistInfo, tok: str = "tok123", **extra) -> SearchSession:
    return SearchSession(
        user_id=USER_ID, query="q", content_type=ContentType.MUSIC,
        selected_content=artist, list_token=tok, **extra,
    )


def _services(add_service):
    from bot.handlers import music

    return patch.object(music, "_get_music_services", AsyncMock(return_value=(MagicMock(), add_service)))


# ------------------------------------------------- album scope under lock
@pytest.mark.asyncio
async def test_album_scope_rereads_session_inside_lock():
    """A write made by another handler between the unlocked read and the
    locked save must not be lost: the save uses the session re-read under
    the lock."""
    from bot.handlers import music
    from bot.ui.callbacks import AlbumScopeCB

    artist = ArtistInfo(mb_id="mb-1", name="Artist", lidarr_id=7)
    stale = _session(artist)
    fresh = _session(artist, current_page=3)  # written by a concurrent handler
    reads = iter([stale, fresh])

    db = MagicMock()
    db.get_session = AsyncMock(side_effect=lambda _uid: next(reads))
    db.save_session = AsyncMock()
    lock = asyncio.Lock()
    db.session_lock = MagicMock(return_value=lock)

    cb = _callback()
    with patch.object(music, "accessible_message", return_value=cb.message):
        await music.handle_album_scope(cb, AlbumScopeCB(album_id=0, artist_id=7), _user(), db)

    saved = db.save_session.await_args.args[1]
    assert saved.current_page == 3
    assert saved.list_token != "tok123"  # fresh token issued on the re-read copy


# ------------------------------------------ discography identity check
@pytest.mark.asyncio
async def test_discography_confirm_checks_artist_identity_without_lidarr_id():
    """With ``lidarr_id`` falsy the id comparison was ``0 == 0`` — a confirm
    button of artist A would run for artist B if the token matched."""
    from bot.handlers import music
    from bot.ui.callbacks import AlbumScopeCB, DiscographyCB

    artist_a = ArtistInfo(mb_id="mb-a", name="Artist A", lidarr_id=None)
    artist_b = ArtistInfo(mb_id="mb-b", name="Artist B", lidarr_id=None)

    # 1) confirm screen for A
    db = MagicMock()
    db.get_session = AsyncMock(return_value=_session(artist_a))
    db.save_session = AsyncMock()

    @contextlib.asynccontextmanager
    async def _lock(_uid):
        yield

    db.session_lock = _lock
    cb = _callback()
    with patch.object(music, "accessible_message", return_value=cb.message):
        await music.handle_album_scope(cb, AlbumScopeCB(album_id=0, artist_id=0), _user(), db)
    tok = db.save_session.await_args.args[1].list_token
    kb = cb.message.edit_text.await_args.kwargs["reply_markup"]
    confirm = DiscographyCB.unpack(kb.inline_keyboard[0][0].callback_data)
    assert len(kb.inline_keyboard[0][0].callback_data.encode()) <= 64

    add_service = MagicMock()
    add_service.lidarr = AsyncMock()

    # 2) session now holds B with the same token → stale
    db_b = MagicMock()
    db_b.get_session = AsyncMock(return_value=_session(artist_b, tok=tok))
    db_b.delete_session = AsyncMock()
    cb_b = _callback()
    with _services(add_service), patch.object(music, "accessible_message", return_value=cb_b.message):
        await music.handle_discography(cb_b, confirm, _user(), db_b)
    add_service.lidarr.search_artist.assert_not_awaited()
    assert cb_b.answer.await_args.kwargs.get("show_alert") is True

    # 3) still A → runs
    db_a = MagicMock()
    db_a.get_session = AsyncMock(return_value=_session(artist_a, tok=tok))
    db_a.delete_session = AsyncMock()
    cb_a = _callback()
    with _services(add_service), patch.object(music, "accessible_message", return_value=cb_a.message):
        await music.handle_discography(cb_a, confirm, _user(), db_a)
    add_service.lidarr.search_artist.assert_awaited_once()
