"""Feature #1: typed CallbackData factory for pagination (LOGIC-14 collision class)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.models import ContentType, SearchResult, SearchSession


def test_page_cb_roundtrip():
    from bot.ui.callbacks import PageCB

    packed = PageCB(scope="search", page=3).pack()
    got = PageCB.unpack(packed)
    assert got.scope == "search"
    assert got.page == 3


def test_search_results_nav_uses_typed_pagecb():
    from bot.ui.callbacks import PageCB
    from bot.ui.keyboards import Keyboards

    results = [SearchResult(guid=str(i), title=f"t{i}") for i in range(5)]
    kb = Keyboards.search_results(results, 0, 2, 5, False, 0)
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]
    page_cbs = [c for c in cbs if c.startswith("pg:")]
    assert page_cbs, f"no typed page callback in {cbs}"
    got = PageCB.unpack(page_cbs[0])
    assert got.scope == "search" and got.page == 1  # next page from page 0


@pytest.mark.asyncio
async def test_handle_pagination_reads_callback_data():
    from bot.handlers import search
    from bot.ui.callbacks import PageCB

    session = SearchSession(
        user_id=1, query="q", content_type=ContentType.MOVIE,
        results=[SearchResult(guid=str(i), title=f"t{i}") for i in range(10)],
    )
    db = AsyncMock()
    db.get_session = AsyncMock(return_value=session)
    db.save_session = AsyncMock()
    db.session_lock = MagicMock(return_value=asyncio.Lock())  # DB-02: real lock, not AsyncMock

    db_user = MagicMock()
    db_user.preferences = MagicMock(auto_grab_enabled=False)

    cb = MagicMock()
    cb.from_user = MagicMock(id=1)
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()

    await search.handle_pagination(cb, PageCB(scope="search", page=1), db_user, db)

    assert session.current_page == 1
    cb.message.edit_text.assert_awaited()


def test_torrent_page_cb_roundtrip():
    """LOGIC-01: TorrentPageCB field is named `flt` (not `filter`, which
    shadows aiogram's CallbackData.filter() classmethod and triggers a
    pydantic UserWarning)."""
    from bot.ui.callbacks import TorrentPageCB

    packed = TorrentPageCB(page=2, flt="downloading").pack()
    got = TorrentPageCB.unpack(packed)
    assert got.page == 2
    assert got.flt == "downloading"


def test_torrent_page_cb_does_not_shadow_parent_filter():
    """Regression guard: constructing TorrentPageCB must not warn."""
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        from bot.ui.callbacks import TorrentPageCB

        # Re-affirm the class is usable and the classmethod is intact.
        assert callable(TorrentPageCB.filter)
        TorrentPageCB(page=0, flt="all")


def test_torrent_list_pagination_buttons_use_typed_cb():
    """LOGIC-01/TEST-08a: pagination/refresh buttons carry the active filter
    via the typed TorrentPageCB, not the old plain t_page:N string."""
    from bot.models import TorrentFilter, TorrentInfo
    from bot.ui.callbacks import TorrentPageCB
    from bot.ui.keyboards import Keyboards

    torrents = [TorrentInfo(hash=f"{'a' * 39}{i}", name=f"T{i}", progress=0.1) for i in range(5)]
    kb = Keyboards.torrent_list(torrents, current_page=1, total_pages=3, current_filter=TorrentFilter.SEEDING)

    cbs = [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]
    typed_cbs = [c for c in cbs if c.startswith("tpg:")]
    assert typed_cbs, f"no typed TorrentPageCB button in {cbs}"
    for c in typed_cbs:
        unpacked = TorrentPageCB.unpack(c)
        assert unpacked.flt == TorrentFilter.SEEDING.value

    # Old plain string prefix must no longer be produced.
    assert not any(c.startswith("t_page:") for c in cbs)


# ---------------------------------------------------------------------------
# Выбор альбома для музыки (2026-08-12). Спека:
# docs/superpowers/specs/2026-08-12-album-scope-design.md
# ---------------------------------------------------------------------------


def _albums():
    from bot.models import AlbumInfo

    return [
        AlbumInfo(lidarr_id=3, title="The Mindsweep", release_date="2015-01-14", album_type="Album"),
        AlbumInfo(lidarr_id=4, title="The Spark", release_date="2017-09-22", has_files=True),
    ]


def test_album_scope_keyboard_offers_whole_discography_first():
    from bot.ui.callbacks import AlbumScopeCB
    from bot.ui.keyboards import Keyboards

    kb = Keyboards.album_scope(_albums(), artist_id=1)
    first = kb.inline_keyboard[0][0]

    assert "дискография" in first.text.lower()
    # Ноль — полноценный ответ «вся дискография», а не «не выбрано».
    assert AlbumScopeCB.unpack(first.callback_data).album_id == 0
    assert AlbumScopeCB.unpack(first.callback_data).artist_id == 1


def test_album_scope_labels_carry_year_and_library_mark():
    from bot.ui.keyboards import Keyboards

    labels = [b.text for row in Keyboards.album_scope(_albums(), artist_id=1).inline_keyboard for b in row]

    assert any("2015" in t and "The Mindsweep" in t for t in labels)
    assert any("The Spark" in t and "✅" in t for t in labels)


def test_album_scope_shows_newest_first():
    from bot.ui.keyboards import Keyboards

    labels = [b.text for row in Keyboards.album_scope(_albums(), artist_id=1).inline_keyboard for b in row]
    spark = next(i for i, t in enumerate(labels) if "The Spark" in t)
    mindsweep = next(i for i, t in enumerate(labels) if "The Mindsweep" in t)

    assert spark < mindsweep


def test_album_scope_paginates_a_long_discography():
    from bot.models import AlbumInfo
    from bot.ui.callbacks import AlbumPageCB
    from bot.ui.keyboards import Keyboards

    albums = [AlbumInfo(lidarr_id=i, title=f"Album {i}", release_date=f"20{i:02d}-01-01") for i in range(1, 23)]
    kb = Keyboards.album_scope(albums, artist_id=1, current_page=0, per_page=5)
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]

    assert any(c.startswith(f"{AlbumPageCB.__prefix__}:") for c in cbs)
    # 22 альбома при per_page=5 — пять страниц, а не одна стена кнопок.
    album_buttons = [c for c in cbs if c.startswith("al:")]
    assert len(album_buttons) == 6  # 5 альбомов + «вся дискография»


def test_album_sources_offers_torrents_then_soulseek():
    from bot.ui.callbacks import AlbumSourceCB
    from bot.ui.keyboards import Keyboards

    kb = Keyboards.album_sources(album_id=3)
    sources = [
        AlbumSourceCB.unpack(b.callback_data).source
        for row in kb.inline_keyboard for b in row
        if b.callback_data and b.callback_data.startswith(f"{AlbumSourceCB.__prefix__}:")
    ]

    assert sources == ["tor", "sk"]


def test_album_releases_keyboard_marks_a_discography_release():
    """Пометка нужна на КНОПКЕ, а не только в тексте: тапают по кнопке."""
    from bot.models import SearchResult
    from bot.ui.callbacks import AlbumGrabCB
    from bot.ui.keyboards import Keyboards

    releases = [
        SearchResult(guid="g1", title="The Mindsweep ALAC", size=330_097_421),
        SearchResult(guid="g2", title="Discography 2007-2023", size=20_000_000_000, is_season_pack=True),
    ]
    kb = Keyboards.album_releases(releases, album_id=3)
    labels = [b.text for row in kb.inline_keyboard for b in row]

    assert any("📚" in t for t in labels)
    first = kb.inline_keyboard[0][0]
    unpacked = AlbumGrabCB.unpack(first.callback_data)
    assert (unpacked.idx, unpacked.album_id) == (0, 3)


def test_album_grab_cb_rejects_a_negative_index():
    """Тот же довод, что у TsReleaseCB: отрицательный индекс обошёл бы проверку
    устаревшего кэша и разрешился бы в чужой элемент."""
    import pydantic
    import pytest as _pytest
    from bot.ui.callbacks import AlbumGrabCB

    with _pytest.raises(pydantic.ValidationError):
        AlbumGrabCB(idx=-1, album_id=3)
