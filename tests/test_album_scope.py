"""Выбор альбома для музыки — пикер, два источника, граб.

Спека: docs/superpowers/specs/2026-08-12-album-scope-design.md

До этой фичи тап по артисту делал `add_artist(monitor="all",
search_for_missing=True)` — вся дискография под мониторингом и в автопоиске.
Живой замер 2026-08-12 (Lidarr 3.1.2): у Enter Shikari 4 альбома из 8 с
файлами, у Free Flow Flava — 2 из 22. Резолв артиста и пикер живут в
tests/test_handlers_music_trending.py; здесь — то, что после пикера.
"""

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.models import (
    ActionLog,
    ActionType,
    AlbumInfo,
    ArtistInfo,
    ContentType,
    SearchResult,
    SearchSession,
    User,
    UserPreferences,
)
from bot.ui.callbacks import (
    AlbumGrabCB,
    AlbumPageCB,
    AlbumScopeCB,
    AlbumSourceCB,
)

USER_ID = 111


def _callback() -> MagicMock:
    cb = MagicMock()
    cb.answer = AsyncMock()
    cb.from_user = MagicMock(id=USER_ID)
    message = MagicMock()
    message.edit_text = AsyncMock()
    cb.message = message
    return cb


def _db(session=None) -> MagicMock:
    db = MagicMock()
    db.get_session = AsyncMock(return_value=session)
    db.save_session = AsyncMock()
    db.delete_session = AsyncMock()
    db.log_action = AsyncMock()

    @contextlib.asynccontextmanager
    async def _lock(_user_id):
        yield

    db.session_lock = _lock
    return db


def _user() -> User:
    return User(tg_id=USER_ID, preferences=UserPreferences())


def _artist(lidarr_id: int = 1, name: str = "Enter Shikari") -> ArtistInfo:
    return ArtistInfo(mb_id="mb-1", name=name, lidarr_id=lidarr_id)


def _session(artist: ArtistInfo, results: list | None = None) -> SearchSession:
    return SearchSession(
        user_id=USER_ID,
        query=artist.name,
        content_type=ContentType.MUSIC,
        selected_content=artist,
        results=results or [],
    )


def _album(album_id: int = 3, title: str = "The Mindsweep", **kw) -> AlbumInfo:
    return AlbumInfo(lidarr_id=album_id, title=title, **kw)


def _grab_action() -> ActionLog:
    return ActionLog(user_id=0, action_type=ActionType.GRAB, content_type=ContentType.MUSIC)


@pytest.fixture(autouse=True)
def _clear_album_cache():
    from bot.handlers import music

    music._album_candidates.clear()
    yield
    music._album_candidates.clear()


# ---------------------------------------------------------------------------
# Пикер: ноль значит «вся дискография»
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_zero_album_id_means_the_whole_discography():
    """«💿 Вся дискография» — это album_id=0: монитор на артиста + ArtistSearch.
    Ноль читается как «выбрано всё», а не как «ничего не выбрано» — тот самый
    урок сезонного цикла 2026-08-12."""
    from bot.handlers import music

    add_service = MagicMock()
    add_service.lidarr = AsyncMock()
    callback = _callback()
    db = _db(session=_session(_artist(lidarr_id=7)))

    with patch.object(music, "_get_music_services", AsyncMock(return_value=(MagicMock(), add_service))), \
            patch.object(music, "accessible_message", return_value=callback.message):
        await music.handle_album_scope(
            callback, AlbumScopeCB(album_id=0, artist_id=7), _user(), db,
        )

    add_service.lidarr.set_artist_monitored.assert_awaited_once_with(7, True)
    add_service.lidarr.search_artist.assert_awaited_once_with(7)


@pytest.mark.asyncio
async def test_picking_an_album_offers_both_sources():
    from bot.handlers import music

    music._album_candidates[USER_ID] = [_album()]
    callback = _callback()
    db = _db(session=_session(_artist()))

    with patch.object(music, "accessible_message", return_value=callback.message):
        await music.handle_album_scope(
            callback, AlbumScopeCB(album_id=3, artist_id=1), _user(), db,
        )

    kb = callback.message.edit_text.await_args.kwargs["reply_markup"]
    sources = [
        AlbumSourceCB.unpack(b.callback_data).source
        for row in kb.inline_keyboard for b in row
        if b.callback_data and b.callback_data.startswith("alsrc:")
    ]
    assert sources == ["tor", "sk"]


@pytest.mark.asyncio
async def test_stale_album_cache_reports_expiry_instead_of_crashing():
    """Кэш дискографии живёт в памяти процесса: после рестарта бота старая
    кнопка должна честно сказать «истёк», а не уронить хендлер."""
    from bot.handlers import music

    callback = _callback()
    db = _db(session=_session(_artist()))

    with patch.object(music, "accessible_message", return_value=callback.message):
        await music.handle_album_scope(
            callback, AlbumScopeCB(album_id=3, artist_id=1), _user(), db,
        )

    assert callback.answer.await_args.kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_album_pagination_keeps_the_same_discography():
    from bot.handlers import music

    music._album_candidates[USER_ID] = [
        _album(album_id=i, title=f"Album {i}", release_date=f"20{i:02d}-01-01")
        for i in range(1, 12)
    ]
    callback = _callback()
    db = _db(session=_session(_artist()))

    with patch.object(music, "accessible_message", return_value=callback.message):
        await music.handle_album_page(
            callback, AlbumPageCB(page=1, artist_id=1), _user(), db,
        )

    kb = callback.message.edit_text.await_args.kwargs["reply_markup"]
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert any("Album" in t for t in labels)


# ---------------------------------------------------------------------------
# Источники
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_torrent_source_lists_lidarr_releases():
    from bot.handlers import music

    music._album_candidates[USER_ID] = [_album()]
    add_service = MagicMock()
    add_service.lidarr = AsyncMock()
    add_service.lidarr.get_releases = AsyncMock(return_value=[
        SearchResult(guid="g", title="Enter Shikari - The Mindsweep ALAC", origin="arr", indexer_id=4),
    ])
    callback = _callback()
    db = _db(session=_session(_artist()))

    with patch.object(music, "_get_music_services", AsyncMock(return_value=(MagicMock(), add_service))), \
            patch.object(music, "accessible_message", return_value=callback.message):
        await music.handle_album_source(
            callback, AlbumSourceCB(album_id=3, source="tor"), _user(), db,
        )

    add_service.lidarr.get_releases.assert_awaited_once_with(3)
    saved = db.save_session.await_args.args[1]
    assert [r.guid for r in saved.results] == ["g"]


@pytest.mark.asyncio
async def test_torrent_source_with_no_hits_offers_soulseek_again():
    """Живой замер: по альбому нашлась ОДНА раздача, так что «пусто» — обычный
    случай, и он не должен быть тупиком."""
    from bot.handlers import music

    music._album_candidates[USER_ID] = [_album()]
    add_service = MagicMock()
    add_service.lidarr = AsyncMock()
    add_service.lidarr.get_releases = AsyncMock(return_value=[])
    callback = _callback()
    db = _db(session=_session(_artist()))

    with patch.object(music, "_get_music_services", AsyncMock(return_value=(MagicMock(), add_service))), \
            patch.object(music, "accessible_message", return_value=callback.message):
        await music.handle_album_source(
            callback, AlbumSourceCB(album_id=3, source="tor"), _user(), db,
        )

    kb = callback.message.edit_text.await_args.kwargs["reply_markup"]
    assert any("Soulseek" in b.text for row in kb.inline_keyboard for b in row)


@pytest.mark.asyncio
async def test_soulseek_source_searches_artist_and_album():
    """Soulseek индексирует файлы, а не тайтлы: «артист альбом» — тот же запрос,
    который уже умеет /album."""
    from bot.handlers import music

    music._album_candidates[USER_ID] = [_album()]
    callback = _callback()
    db = _db(session=_session(_artist()))

    with patch.object(music, "process_soulseek_search", AsyncMock()) as spied, \
            patch.object(music, "accessible_message", return_value=callback.message):
        await music.handle_album_source(
            callback, AlbumSourceCB(album_id=3, source="sk"), _user(), db,
        )

    assert spied.await_args.args[1] == "Enter Shikari The Mindsweep"


# ---------------------------------------------------------------------------
# Граб: монитор — следствие успешного взятия
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_music_grab_monitors_only_the_taken_album():
    """Монитор включается ЗДЕСЬ, а не при добавлении артиста — паритет с
    `_execute_grab` у сериалов, где `set_series_monitored` вызывается в момент
    взятия раздачи."""
    from bot.handlers import music

    add_service = MagicMock()
    add_service.lidarr = AsyncMock()
    add_service.grab_release = AsyncMock(return_value=(True, _grab_action()))
    music._album_candidates[USER_ID] = [_album()]
    session = _session(_artist(), results=[
        SearchResult(guid="g", title="ALAC", origin="arr", indexer_id=4),
    ])
    callback = _callback()

    with patch.object(music, "_get_music_services", AsyncMock(return_value=(MagicMock(), add_service))), \
            patch.object(music, "accessible_message", return_value=callback.message):
        await music.handle_album_grab(
            callback, AlbumGrabCB(idx=0, album_id=3), _user(), _db(session=session),
        )

    assert add_service.grab_release.await_args.kwargs["arr_id"] == 3
    assert add_service.grab_release.await_args.args[1] is ContentType.MUSIC
    add_service.lidarr.set_album_monitored.assert_awaited_once_with(3, True)
    add_service.lidarr.set_artist_monitored.assert_awaited_once_with(1, True)


@pytest.mark.asyncio
async def test_failed_music_grab_does_not_monitor_anything():
    """Монитор — следствие успешного взятия, а не попытки."""
    from bot.handlers import music

    action = _grab_action()
    action.success = False
    action.error_message = "Отклонено профилем"
    add_service = MagicMock()
    add_service.lidarr = AsyncMock()
    add_service.grab_release = AsyncMock(return_value=(False, action))
    music._album_candidates[USER_ID] = [_album()]
    session = _session(_artist(), results=[
        SearchResult(guid="g", title="ALAC", origin="arr", indexer_id=4),
    ])
    callback = _callback()

    with patch.object(music, "_get_music_services", AsyncMock(return_value=(MagicMock(), add_service))), \
            patch.object(music, "accessible_message", return_value=callback.message):
        await music.handle_album_grab(
            callback, AlbumGrabCB(idx=0, album_id=3), _user(), _db(session=session),
        )

    add_service.lidarr.set_album_monitored.assert_not_awaited()
    assert "Отклонено профилем" in callback.message.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_grab_with_a_stale_release_list_says_so():
    from bot.handlers import music

    callback = _callback()

    with patch.object(music, "accessible_message", return_value=callback.message):
        await music.handle_album_grab(
            callback, AlbumGrabCB(idx=5, album_id=3), _user(), _db(session=_session(_artist())),
        )

    assert callback.answer.await_args.kwargs.get("show_alert") is True


# ---------------------------------------------------------------------------
# Пометки в списке релизов
# ---------------------------------------------------------------------------


def test_music_results_page_uses_a_music_emoji():
    """До правки музыкальная страница показывала 📺 — эмодзи сериала."""
    from bot.ui.formatters import Formatters

    text = Formatters.format_search_results_page(
        [SearchResult(guid="g", title="Album ALAC")], 0, 1, "q", ContentType.MUSIC,
    )

    assert "🎵" in text


def test_discography_release_is_marked_in_the_list():
    """Иначе пользователь, выбравший один альбом, тапнет по 20 ГБ и не поймёт."""
    from bot.ui.formatters import Formatters

    text = Formatters.format_search_results_page(
        [SearchResult(guid="g", title="Enter Shikari - Discography", is_season_pack=True)],
        0, 1, "q", ContentType.MUSIC,
    )

    assert "📚" in text


def test_season_pack_with_a_known_season_is_not_labelled_a_discography():
    """У сериального пака сезон печатается отдельной строкой — 📚 там не к месту."""
    from bot.ui.formatters import Formatters

    text = Formatters.format_search_results_page(
        [SearchResult(guid="g", title="Show S02E01-08", is_season_pack=True, detected_season=2)],
        0, 1, "q", ContentType.SERIES,
    )

    assert "📚" not in text


# ---------------------------------------------------------------------------
# Проводка. Тесты выше зовут хендлеры напрямую и потому НЕ увидят ни забытого
# декоратора, ни коллизии префиксов — а на живом боте это выглядит как «кнопка
# не работает». В памяти проекта такие грабли уже были (порядок роутеров в
# разделе TorrServer).
# ---------------------------------------------------------------------------


def test_album_handlers_are_registered_in_the_music_router():
    from bot.handlers.music import router

    names = {h.callback.__name__ for h in router.callback_query.handlers}

    assert {
        "handle_album_scope",
        "handle_album_page",
        "handle_album_source",
        "handle_album_grab",
    } <= names


def test_callback_prefixes_are_unique_across_all_families():
    """`search_router` регистрируется раньше музыкального, поэтому одинаковый
    префикс молча увёл бы музыкальный колбэк в чужой обработчик."""
    import inspect

    from aiogram.filters.callback_data import CallbackData as _CD

    from bot.ui import callbacks as cb_module

    prefixes = {}
    for _name, obj in inspect.getmembers(cb_module, inspect.isclass):
        if issubclass(obj, _CD) and obj is not _CD:
            prefix = obj.__prefix__
            assert prefix not in prefixes, (
                f"префикс {prefix!r} уже занят {prefixes[prefix]}"
            )
            prefixes[prefix] = obj.__name__

    # Новые семейства на месте и различимы.
    assert {"al", "alsrc", "alpg", "alg"} <= set(prefixes)


# ---------------------------------------------------------------------------
# Живой прогон 2026-08-12 внутри контейнера (127 раздач альбома «Reload»)
# показал две дырки, которых на моках не видно:
#   1. `parse_quality_name` не знает аудио-форматов — codec/source/resolution
#      выходили пустыми, и подпись кнопки была «1. 767.9 MB» без FLAC/MP3.
#      Реальные имена от Lidarr: FLAC 24bit, FLAC, MP3-320, AAC-VBR, MP3-256,
#      MP3-192, MP3-160, MP3-128, WMA, WavPack, Unknown.
#   2. 24 раздачи из 127 Lidarr отверг («Album wasn't requested», «Wrong
#      album»), но на кнопке они выглядели как остальные.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,codec", [
    ("FLAC 24bit", "FLAC 24bit"),
    ("FLAC", "FLAC"),
    ("MP3-320", "MP3-320"),
    ("AAC-VBR", "AAC-VBR"),
    ("WavPack", "WavPack"),
    ("WMA", "WMA"),
])
def test_audio_quality_names_survive_parsing(name, codec):
    """Для музыки формат — главный различитель раздач, а resolution/source —
    видео-поля и для FLAC пусты. Имя кладётся в codec: FLAC/MP3/AAC и есть
    кодеки."""
    from bot.services.release_parser import parse_quality_name

    assert parse_quality_name(name).codec == codec


def test_unknown_quality_name_stays_empty():
    from bot.services.release_parser import parse_quality_name

    assert parse_quality_name("Unknown").codec is None


def test_video_quality_names_are_unaffected():
    """Страховка: видео-имена по-прежнему разбираются в resolution/source."""
    from bot.services.release_parser import parse_quality_name

    q = parse_quality_name("Bluray-2160p")
    assert (q.resolution, q.source) == ("2160p", "BluRay")
    assert q.codec is None


def test_album_release_buttons_show_format_and_rejection():
    from bot.models import QualityInfo
    from bot.ui.keyboards import Keyboards

    releases = [
        SearchResult(
            guid="g1", title="Metallica - Reload FLAC", size=784_000_000,
            quality=QualityInfo(codec="FLAC"), seeders=12,
        ),
        SearchResult(
            guid="g2", title="Metallica - Reload WAV", size=767_900_000,
            quality=QualityInfo(codec="WAV"), rejected=True,
            rejections=["Album wasn't requested"],
        ),
    ]
    labels = [b.text for row in Keyboards.album_releases(releases, album_id=3).inline_keyboard for b in row]

    assert any("FLAC" in t for t in labels)
    assert any("⛔" in t for t in labels), "отвергнутую Lidarr раздачу надо помечать на кнопке"


@pytest.mark.asyncio
async def test_rejected_releases_sink_to_the_bottom():
    """24 из 127 живых раздач отвергнуты — они не должны занимать первые кнопки.
    Внутри групп порядок по сидам: у музыки размер и формат — вкус, а сиды это
    «скачается ли вообще»."""
    from bot.handlers import music

    music._album_candidates[USER_ID] = [_album()]
    add_service = MagicMock()
    add_service.lidarr = AsyncMock()
    add_service.lidarr.get_releases = AsyncMock(return_value=[
        SearchResult(guid="bad", title="rejected", rejected=True, seeders=99),
        SearchResult(guid="ok-low", title="ok few seeders", seeders=2),
        SearchResult(guid="ok-high", title="ok many seeders", seeders=40),
    ])
    callback = _callback()
    db = _db(session=_session(_artist()))

    with patch.object(music, "_get_music_services", AsyncMock(return_value=(MagicMock(), add_service))),             patch.object(music, "accessible_message", return_value=callback.message):
        await music.handle_album_source(
            callback, AlbumSourceCB(album_id=3, source="tor"), _user(), db,
        )

    saved = db.save_session.await_args.args[1]
    assert [r.guid for r in saved.results] == ["ok-high", "ok-low", "bad"]


# ---------------------------------------------------------------------------
# Живой пробник 2026-08-12 (добавление Gojira в Lidarr и возврат): сразу после
# add_artist `GET /album?artistId=` вернул НОЛЬ альбомов — Lidarr тянет
# дискографию фоновой командой RefreshArtist. Через несколько минут альбомы
# появились. То есть главный сценарий («добавь нового артиста и возьми альбом»)
# без ретрая упирается в пустой пикер.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_discography_is_retried_before_giving_up():
    """Первый ответ пустой (RefreshArtist ещё идёт) — второй уже с альбомами."""
    from bot.handlers import music

    lidarr = AsyncMock()
    lidarr.get_albums = AsyncMock(side_effect=[[], [], [_album()]])

    with patch.object(music.asyncio, "sleep", AsyncMock()):
        albums = await music._albums_with_retry(lidarr, artist_id=1)

    assert [a.lidarr_id for a in albums] == [3]
    assert lidarr.get_albums.await_count == 3


@pytest.mark.asyncio
async def test_retry_gives_up_and_returns_empty():
    """Ждать минуты нельзя — окно колбэка Telegram ~15 с. Сдаёмся и показываем
    пикер с кнопкой обновления."""
    from bot.handlers import music

    lidarr = AsyncMock()
    lidarr.get_albums = AsyncMock(return_value=[])

    with patch.object(music.asyncio, "sleep", AsyncMock()):
        albums = await music._albums_with_retry(lidarr, artist_id=1)

    assert albums == []
    assert lidarr.get_albums.await_count == music._ALBUM_RETRIES


@pytest.mark.asyncio
async def test_a_found_discography_is_not_retried():
    from bot.handlers import music

    lidarr = AsyncMock()
    lidarr.get_albums = AsyncMock(return_value=[_album()])

    albums = await music._albums_with_retry(lidarr, artist_id=1)

    assert lidarr.get_albums.await_count == 1
    assert len(albums) == 1


def test_empty_picker_offers_a_refresh_button():
    """Иначе пользователю остаётся только начать поиск заново."""
    from bot.ui.callbacks import AlbumRefreshCB
    from bot.ui.keyboards import Keyboards

    kb = Keyboards.album_scope([], artist_id=7)
    refresh = [
        b for row in kb.inline_keyboard for b in row
        if b.callback_data and b.callback_data.startswith("alrf:")
    ]

    assert refresh, "пустой пикер без кнопки обновления — тупик"
    assert AlbumRefreshCB.unpack(refresh[0].callback_data).artist_id == 7


def test_non_empty_picker_has_no_refresh_button():
    from bot.ui.keyboards import Keyboards

    kb = Keyboards.album_scope([_album()], artist_id=7)
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]

    assert not any(c.startswith("alrf:") for c in cbs)


@pytest.mark.asyncio
async def test_refresh_reasks_lidarr_for_the_discography():
    from bot.handlers import music
    from bot.ui.callbacks import AlbumRefreshCB

    add_service = MagicMock()
    add_service.lidarr = AsyncMock()
    add_service.lidarr.get_albums = AsyncMock(return_value=[_album()])
    callback = _callback()
    db = _db(session=_session(_artist(lidarr_id=7)))

    with patch.object(music, "_get_music_services", AsyncMock(return_value=(MagicMock(), add_service))),             patch.object(music, "accessible_message", return_value=callback.message):
        await music.handle_album_refresh(
            callback, AlbumRefreshCB(artist_id=7), _user(), db,
        )

    add_service.lidarr.get_albums.assert_awaited_with(7)
    assert music._album_candidates[USER_ID][0].lidarr_id == 3
