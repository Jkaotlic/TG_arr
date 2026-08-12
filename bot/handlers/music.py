"""Music (Lidarr) handlers: search artists, view details, add to Lidarr."""

import asyncio
import html
from typing import Optional

import structlog
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot.clients.registry import (
    get_deezer,
    get_lidarr,
    get_navidrome,
    get_qbittorrent,
    get_radarr,
    get_slskd,
    get_sonarr,
)
from bot.config import get_settings
from bot.db import Database
from bot.handlers._cache import remember_lru
from bot.handlers.common import accessible_message, safe_edit, strip_command
from bot.models import (
    ActionLog,
    ActionType,
    AlbumInfo,
    ArtistInfo,
    ContentType,
    SearchSession,
    User,
)
from bot.services.add_service import AddService
from bot.services.search_service import SearchService
from bot.ui.callbacks import (
    AlbumGrabCB,
    AlbumPageCB,
    AlbumScopeCB,
    AlbumSourceCB,
    ArtistCB,
    SlskdCB,
    TrendingItemCB,
)
from bot.ui.formatters import Formatters
from bot.ui.keyboards import CallbackData, Keyboards
from bot.ui.menu import MENU_MUSIC

logger = structlog.get_logger()
router = Router()

# PERF-04: Reuse the same ScoringService instance across music requests.
# Import here rather than in search.py to avoid a circular import at module load.
from bot.handlers.search import _SCORING_SERVICE  # noqa: E402

# PERF-03: Hard cap for per-user in-memory caches — prevents unbounded memory growth.
_MAX_ARTIST_CANDIDATES = 100


def _remember(cache: dict, key, value) -> None:
    """Insert/update `key`, evicting the oldest entry (dict insertion order)
    when at capacity — a single overflow no longer kicks every other user
    mid-selection back to "Список истёк" (BUG-10/PERF-12).

    Thin wrapper around the shared LRU cache helper (LOGIC-21) — kept as a
    module-level function since existing tests call ``music._remember``
    directly.
    """
    remember_lru(cache, key, value, _MAX_ARTIST_CANDIDATES)

# Session key: store artist candidates list separately from SearchResult list
# We reuse SearchSession.results (as search releases) and selected_content (ArtistInfo)
# Per-user in-memory artist lookup cache (for select-by-index callbacks)
_artist_candidates: dict[int, list[ArtistInfo]] = {}
_trending_artists_cache: dict[int, list[dict]] = {}
# Дискография выбранного артиста. В сессию (и в SQLite) не кладётся: альбом —
# не «выбранный тайтл», а добавление варианта в union `ContentInfo` потребовало
# бы миграции сохранённых сессий. Цена — «список истёк» после рестарта бота,
# ровно как у артистов выше.
_album_candidates: dict[int, list[AlbumInfo]] = {}


def _artist_list_text(artists: list[ArtistInfo], page_artists: list[ArtistInfo], start_idx: int) -> str:
    """LOGIC-14a: render the numbered artist list body shared by the initial
    search results, pagination and the music-back handler."""
    lines = [f"🎵 <b>Найдено артистов: {len(artists)}</b>\n"]
    for i, a in enumerate(page_artists, start=start_idx):
        disamb = f" <i>[{html.escape(a.disambiguation)}]</i>" if a.disambiguation else ""
        in_lib = " ✅" if a.lidarr_id else ""
        lines.append(f"{i + 1}. <b>{html.escape(a.name)}</b>{disamb}{in_lib}")
    return "\n".join(lines)


async def _render_artist_list(message: Message, artists: list[ArtistInfo], page: int = 0) -> None:
    """LOGIC-14a: shared renderer for the artist-list keyboard — dedups the
    three previously-copied render blocks (initial search, art_page:, music_back).

    LOGIC-14b: ``per_page`` now follows ``settings.results_per_page`` (same
    knob the search-results pagination already respects) instead of a
    hardcoded 5, so users who tune that setting get consistent page sizes
    across search and artist lists.
    """
    per_page = get_settings().results_per_page
    start_idx = page * per_page
    page_artists = artists[start_idx:start_idx + per_page]
    text = _artist_list_text(artists, page_artists, start_idx)

    await safe_edit(
        message,
        text,
        reply_markup=Keyboards.artist_list(artists, current_page=page, per_page=per_page),
        parse_mode="HTML",
    )


def _find_album(user_id: int, album_id: int) -> Optional[AlbumInfo]:
    """Альбом из per-user кэша дискографии; None, если кэш истёк (рестарт бота)."""
    return next(
        (a for a in _album_candidates.get(user_id) or [] if a.lidarr_id == album_id),
        None,
    )


async def _show_album_scope(message: Message, artist: ArtistInfo, albums: list[AlbumInfo]) -> None:
    """Пикер «Что искать?» — прямой аналог `Keyboards.season_scope` у сериалов."""
    if albums:
        body = f"🎵 <b>{html.escape(artist.name)}</b>\n\nЧто искать?"
    else:
        # Метаданные могли ещё не подтянуться после добавления артиста — пустой
        # экран был бы тупиком, поэтому «вся дискография» остаётся доступной.
        body = (
            f"🎵 <b>{html.escape(artist.name)}</b>\n\n"
            "Дискография пока не известна Lidarr — попробуйте позже "
            "или возьмите артиста целиком."
        )
    await safe_edit(
        message,
        body,
        reply_markup=Keyboards.album_scope(
            albums,
            artist_id=artist.lidarr_id or 0,
            per_page=get_settings().results_per_page,
        ),
        parse_mode="HTML",
    )


async def _get_music_services() -> tuple[SearchService, AddService] | None:
    """Build SearchService and AddService wired to the music backends.

    Returns None only when *no* music backend is configured. Lidarr is the
    preferred one (it owns the library layout and hands work to slskd), but
    slskd alone is enough for search + direct download.

    Rollback 2026-08-10 (Task 13 finish-up): this used to build both services
    around the previous backend's single client. SearchService/AddService now take
    Radarr+Sonarr positionally (same as every other handler) — Radarr/Sonarr
    are always available from the registry (non-optional, see status.py's
    `_collect_statuses` docstring), so they're fetched here too even though
    music.py never acts on them directly; AddService also dropped the
    `slskd` kwarg it never used.
    """
    lidarr = await get_lidarr()
    slskd = await get_slskd()
    if lidarr is None and slskd is None:
        return None

    radarr = await get_radarr()
    sonarr = await get_sonarr()
    qbittorrent = await get_qbittorrent()

    search_service = SearchService(
        radarr, sonarr, lidarr=lidarr, scoring=_SCORING_SERVICE, slskd=slskd
    )
    add_service = AddService(radarr, sonarr, qbittorrent=qbittorrent, lidarr=lidarr)
    return search_service, add_service


# Per-user Soulseek candidates, same LRU discipline as _artist_candidates.
_slskd_candidates: dict[int, list] = {}


@router.message(Command("music"))
async def cmd_music(message: Message, db_user: User, db: Database) -> None:
    """Handle /music <artist> command."""
    if not message.text:
        await message.answer("Укажите артиста: <code>/music Metallica</code>")
        return

    query = strip_command(message.text, "/music")
    if not query:
        await message.answer("Укажите артиста: <code>/music Metallica</code>")
        return

    await process_music_search(message, query, db_user, db)


@router.message(Command("album"))
async def cmd_album(message: Message, db_user: User, db: Database) -> None:
    """Handle /album <artist - album> — direct Soulseek search.

    Lidarr's model is artist-centric: it can monitor an artist's discography
    but cannot express "just get me this one album". Soulseek indexes shared
    *files*, so slskd answers that question directly.
    """
    if not message.text:
        await message.answer("Укажите альбом: <code>/album Metallica Master of Puppets</code>")
        return

    query = strip_command(message.text, "/album")
    if not query:
        await message.answer("Укажите альбом: <code>/album Metallica Master of Puppets</code>")
        return

    await process_soulseek_search(message, query, db_user, db, kind="album")


@router.message(Command("track"))
async def cmd_track(message: Message, db_user: User, db: Database) -> None:
    """Handle /track <artist - title> — direct Soulseek search for one track."""
    if not message.text:
        await message.answer("Укажите трек: <code>/track Metallica One</code>")
        return

    query = strip_command(message.text, "/track")
    if not query:
        await message.answer("Укажите трек: <code>/track Metallica One</code>")
        return

    await process_soulseek_search(message, query, db_user, db, kind="track")


async def _navidrome_note(artist: str, album: str = "") -> str:
    """Best-effort "already in the library" hint from Navidrome.

    Never raises and never blocks the flow — on any error/timeout/absence it
    returns "" and the user simply doesn't see the hint.
    """
    try:
        navidrome = await get_navidrome()
        if navidrome is None:
            return ""
        if album:
            exists = await asyncio.wait_for(navidrome.has_album(artist, album), timeout=5.0)
        else:
            exists = await asyncio.wait_for(navidrome.has_artist(artist), timeout=5.0)
        return "\n\n✅ <i>Уже есть в библиотеке (Navidrome)</i>" if exists else ""
    except Exception as e:
        logger.debug("navidrome_note_skipped", error=str(e))
        return ""


async def process_soulseek_search(
    message: Message,
    query: str,
    db_user: User,
    db: Database,
    *,
    kind: str = "album",
) -> None:
    """Search Soulseek via slskd and present the candidates."""
    MAX_QUERY_LENGTH = 200
    if len(query) > MAX_QUERY_LENGTH:
        await message.answer(f"❌ Запрос слишком длинный (макс. {MAX_QUERY_LENGTH} символов)")
        return
    if len(query) < 2:
        await message.answer("❌ Запрос слишком короткий (мин. 2 символа)")
        return

    slskd = await get_slskd()
    if slskd is None:
        await message.answer(
            "Soulseek не настроен. Добавьте SLSKD_URL и SLSKD_API_KEY в .env"
        )
        return

    user_id = db_user.tg_id
    log = logger.bind(user_id=user_id, query=query, kind=kind)
    status_msg = await message.answer("🔍 Ищу в Soulseek (это занимает ~25 секунд)...")

    try:
        results = await slskd.search(query, limit=25)
    except Exception as e:
        log.error("slskd_search_failed", error=str(e), exc_info=True)
        await status_msg.edit_text(Formatters.format_error("Soulseek недоступен"))
        return

    if not results:
        await status_msg.edit_text(
            Formatters.format_warning(f"В Soulseek ничего не найдено: <b>{html.escape(query)}</b>"),
            parse_mode="HTML",
        )
        return

    _remember(_slskd_candidates, user_id, results)

    await db.log_action(
        ActionLog(
            user_id=user_id,
            action_type=ActionType.SEARCH,
            content_type=ContentType.MUSIC,
            query=query,
        )
    )

    note = await _navidrome_note(results[0].guessed_artist, query if kind == "album" else "")
    await status_msg.edit_text(
        Formatters.format_slskd_results(results, query) + note,
        reply_markup=Keyboards.slskd_results(results),
        parse_mode="HTML",
    )


@router.callback_query(SlskdCB.filter())
async def handle_slskd_selection(
    callback: CallbackQuery, callback_data: SlskdCB, db_user: User, db: Database
) -> None:
    """Queue a Soulseek candidate for download via slskd."""
    message = accessible_message(callback)
    if message is None:
        return

    user_id = callback.from_user.id
    candidates = _slskd_candidates.get(user_id) or []
    idx = callback_data.idx
    if idx < 0 or idx >= len(candidates):
        await callback.answer("Список истёк. Повторите поиск.", show_alert=True)
        return

    await callback.answer("Ставлю в очередь...")
    candidate = candidates[idx]

    slskd = await get_slskd()
    if slskd is None:
        await message.edit_text(Formatters.format_error("Soulseek не настроен"))
        return

    action = ActionLog(
        user_id=user_id,
        action_type=ActionType.GRAB,
        content_type=ContentType.MUSIC,
        content_title=candidate.display_name,
        release_title=f"{candidate.username}: {candidate.folder}"[:200],
    )

    ok = await slskd.enqueue(candidate.username, candidate.files)
    action.success = ok
    if not ok:
        action.error_message = "slskd rejected the transfer"
    await db.log_action(action)

    if ok:
        await message.edit_text(
            Formatters.format_success(
                f"<b>{html.escape(candidate.display_name)}</b>\n\n"
                f"Поставлено в очередь Soulseek: {candidate.track_count} файл(ов), "
                f"{candidate.size_formatted}\n\n"
                f"Прогресс — в /downloads"
            ),
            parse_mode="HTML",
        )
    else:
        await message.edit_text(
            Formatters.format_error(
                "Не удалось поставить в очередь — возможно, пользователь ушёл в офлайн"
            )
        )


@router.message(F.text == MENU_MUSIC)
async def handle_menu_music(message: Message) -> None:
    """Handle 🎵 Музыка menu button — prompt user for query."""
    settings = get_settings()
    if not settings.music_enabled:
        await message.answer(
            "Музыкальный контур не настроен. Добавьте LIDARR_URL/LIDARR_API_KEY "
            "или SLSKD_URL/SLSKD_API_KEY в .env"
        )
        return
    await message.answer("🎵 Введите имя артиста (<code>/music &lt;artist&gt;</code> или просто текст):")


async def process_music_search(
    message: Message,
    query: str,
    db_user: User,
    db: Database,
) -> None:
    """Look up artists in Lidarr and present selection list."""
    MAX_QUERY_LENGTH = 200
    if len(query) > MAX_QUERY_LENGTH:
        await message.answer(f"❌ Запрос слишком длинный (макс. {MAX_QUERY_LENGTH} символов)")
        return
    if len(query) < 2:
        await message.answer("❌ Запрос слишком короткий (мин. 2 символа)")
        return

    services = await _get_music_services()
    if services is None:
        await message.answer("Lidarr не настроен. Добавьте LIDARR_URL и LIDARR_API_KEY в .env")
        return
    search_service, _add_service = services

    user_id = db_user.tg_id
    log = logger.bind(user_id=user_id, query=query)

    status_msg = await message.answer("🔍 Ищу артистов в Lidarr...")

    try:
        artists = await search_service.lookup_artist(query)
    except Exception as e:
        log.error("Artist lookup failed", error=str(e), exc_info=True)
        await status_msg.edit_text(Formatters.format_error("Не удалось найти артистов (Lidarr недоступен?)"))
        return

    if not artists:
        # BUG-32: drop any stale session before returning so unrelated callbacks
        # don't reactivate it later.
        await db.delete_session(user_id)
        await status_msg.edit_text(
            Formatters.format_warning(f"Артист не найден: <b>{html.escape(query)}</b>"),
            parse_mode="HTML",
        )
        return

    # Cache for callback lookup by index (BUG-10/PERF-12: LRU eviction, see _remember)
    _remember(_artist_candidates, user_id, artists[:25])

    # Persist a minimal session so /back/cancel consistently works
    session = SearchSession(
        user_id=user_id,
        query=query,
        content_type=ContentType.MUSIC,
    )
    await db.save_session(user_id, session)

    # Log action
    action = ActionLog(
        user_id=user_id,
        action_type=ActionType.SEARCH,
        content_type=ContentType.MUSIC,
        query=query,
    )
    await db.log_action(action)

    await _render_artist_list(status_msg, _artist_candidates[user_id], page=0)


@router.callback_query(F.data.startswith(CallbackData.ARTIST_PAGE))
async def handle_artist_pagination(callback: CallbackQuery, db_user: User, db: Database) -> None:
    """Paginate the artist-list keyboard (LOGIC-14: independent prefix from search)."""
    message = accessible_message(callback)
    if message is None or not callback.data:
        return
    user_id = callback.from_user.id

    try:
        page = int(callback.data.removeprefix(CallbackData.ARTIST_PAGE))
    except ValueError:
        await callback.answer("Неверная страница", show_alert=True)
        return

    artists = _artist_candidates.get(user_id) or []
    if not artists:
        await callback.answer("Список истёк. Начните новый поиск.", show_alert=True)
        return

    per_page = get_settings().results_per_page
    total_pages = max(1, (len(artists) + per_page - 1) // per_page)
    if page < 0 or page >= total_pages:
        await callback.answer("Неверная страница", show_alert=True)
        return

    await _render_artist_list(message, artists, page=page)
    await callback.answer()


@router.callback_query(F.data == CallbackData.MUSIC_BACK)
async def handle_music_back(callback: CallbackQuery, db_user: User, db: Database) -> None:
    """LOGIC-24: back from artist_details → return to artist_list."""
    message = accessible_message(callback)
    if message is None:
        return
    user_id = callback.from_user.id
    artists = _artist_candidates.get(user_id) or []
    if not artists:
        await callback.answer("Список истёк. Начните новый поиск.", show_alert=True)
        return

    await _render_artist_list(message, artists, page=0)
    await callback.answer()


@router.callback_query(ArtistCB.filter())
async def handle_artist_selection(
    callback: CallbackQuery, callback_data: ArtistCB, db_user: User, db: Database
) -> None:
    """Handle artist selection from lookup results."""
    message = accessible_message(callback)
    if message is None:
        return

    user_id = callback.from_user.id
    idx = callback_data.idx

    artists = _artist_candidates.get(user_id) or []
    if idx < 0 or idx >= len(artists):
        await callback.answer("Выбор истёк. Начните новый поиск.", show_alert=True)
        return

    artist = artists[idx]

    # DB-02: lock the read-modify-write cycle around storing the selected artist.
    async with db.session_lock(user_id):
        session = await db.get_session(user_id)
        if session is None:
            session = SearchSession(user_id=user_id, query=artist.name, content_type=ContentType.MUSIC)
        session.selected_content = artist
        await db.save_session(user_id, session)

    await callback.answer()
    await message.edit_text(
        Formatters.format_artist_info(artist),
        reply_markup=Keyboards.artist_details(artist, already_in_library=bool(artist.lidarr_id)),
        parse_mode="HTML",
    )


@router.callback_query(AlbumScopeCB.filter())
async def handle_album_scope(
    callback: CallbackQuery, callback_data: AlbumScopeCB, db_user: User, db: Database
) -> None:
    """Continue after the discography picker.

    `album_id == 0` — «вся дискография»: полноценный ответ, а не «не выбрано».
    Сравнение именно с нулём, а не проверка на ложность: живой дефект
    2026-08-12 у сезонов замкнулся в цикл ровно на этом
    (analysis/2026-08-12-season-loop-and-packs.md).
    """
    message = accessible_message(callback)
    if message is None:
        return

    user_id = callback.from_user.id
    session = await db.get_session(user_id)
    if not session or not isinstance(session.selected_content, ArtistInfo):
        await callback.answer("Сессия истекла. Начните новый поиск.", show_alert=True)
        return
    artist: ArtistInfo = session.selected_content

    if callback_data.album_id == 0:
        services = await _get_music_services()
        if services is None:
            await callback.answer("Lidarr не настроен", show_alert=True)
            return
        _search_service, add_service = services

        await callback.answer("Беру всю дискографию...")
        artist_id = artist.lidarr_id or callback_data.artist_id
        # Монитор и поиск делегируются Lidarr: он сам решает, что «все альбомы»
        # значит для этого артиста по его metadata-профилю.
        await add_service.lidarr.set_artist_monitored(artist_id, True)
        await add_service.lidarr.search_artist(artist_id)
        await message.edit_text(
            Formatters.format_success(
                f"<b>{html.escape(artist.name)}</b>\n\n"
                "Вся дискография под мониторингом, запущен автопоиск."
            ),
            parse_mode="HTML",
        )
        await db.delete_session(user_id)
        _album_candidates.pop(user_id, None)
        return

    album = _find_album(user_id, callback_data.album_id)
    if album is None:
        await callback.answer("Список альбомов истёк. Начните новый поиск.", show_alert=True)
        return

    await callback.answer()
    year = f" · {album.year}" if album.year else ""
    await message.edit_text(
        f"💿 <b>{html.escape(album.title)}</b>{year}\n\nГде искать?",
        reply_markup=Keyboards.album_sources(album.lidarr_id),
        parse_mode="HTML",
    )


@router.callback_query(AlbumPageCB.filter())
async def handle_album_page(
    callback: CallbackQuery, callback_data: AlbumPageCB, db_user: User, db: Database
) -> None:
    """Page through a long discography (22 альбома живьём — это 5 страниц)."""
    message = accessible_message(callback)
    if message is None:
        return

    user_id = callback.from_user.id
    session = await db.get_session(user_id)
    albums = _album_candidates.get(user_id) or []
    if not albums or not session or not isinstance(session.selected_content, ArtistInfo):
        await callback.answer("Список альбомов истёк. Начните новый поиск.", show_alert=True)
        return

    await callback.answer()
    await safe_edit(
        message,
        f"🎵 <b>{html.escape(session.selected_content.name)}</b>\n\nЧто искать?",
        reply_markup=Keyboards.album_scope(
            albums,
            artist_id=callback_data.artist_id,
            current_page=callback_data.page,
            per_page=get_settings().results_per_page,
        ),
        parse_mode="HTML",
    )


@router.callback_query(AlbumSourceCB.filter())
async def handle_album_source(
    callback: CallbackQuery, callback_data: AlbumSourceCB, db_user: User, db: Database
) -> None:
    """Look for the chosen album: torrents via Lidarr, or Soulseek via slskd."""
    message = accessible_message(callback)
    if message is None:
        return

    user_id = callback.from_user.id
    session = await db.get_session(user_id)
    album = _find_album(user_id, callback_data.album_id)
    if album is None or not session or not isinstance(session.selected_content, ArtistInfo):
        await callback.answer("Список альбомов истёк. Начните новый поиск.", show_alert=True)
        return
    artist: ArtistInfo = session.selected_content

    if callback_data.source == "sk":
        # Soulseek индексирует ФАЙЛЫ, а не тайтлы: «артист альбом» — ровно тот
        # запрос, который уже умеет /album.
        await callback.answer()
        await process_soulseek_search(
            message, f"{artist.name} {album.title}", db_user, db, kind="album",
        )
        return

    services = await _get_music_services()
    if services is None:
        await callback.answer("Lidarr не настроен", show_alert=True)
        return
    _search_service, add_service = services

    await callback.answer("Ищу раздачи...")
    await message.edit_text(
        f"🔍 Ищу раздачи: <b>{html.escape(album.title)}</b>...", parse_mode="HTML",
    )
    try:
        releases = await add_service.lidarr.get_releases(album.lidarr_id)
    except Exception as e:
        logger.error("album_release_search_failed", error=str(e), exc_info=True)
        await message.edit_text(
            Formatters.format_error("Индексеры не ответили"),
            reply_markup=Keyboards.album_sources(album.lidarr_id),
        )
        return

    if not releases:
        # Живой замер: по одному альбому индексеры дали ОДНУ раздачу, так что
        # пусто — обычный случай. Soulseek остаётся кнопкой, а не тупиком.
        await message.edit_text(
            Formatters.format_warning(
                f"Торренты для <b>{html.escape(album.title)}</b> не найдены.\n\n"
                "Soulseek ищет по файлам и на таких запросах обычно удачливее."
            ),
            reply_markup=Keyboards.album_sources(album.lidarr_id),
            parse_mode="HTML",
        )
        return

    async with db.session_lock(user_id):
        session.results = releases
        session.current_page = 0
        await db.save_session(user_id, session)

    per_page = get_settings().results_per_page
    await message.edit_text(
        Formatters.format_search_results_page(
            releases[:per_page], 0,
            max(1, (len(releases) + per_page - 1) // per_page),
            f"{artist.name} — {album.title}", ContentType.MUSIC, per_page=per_page,
        ),
        reply_markup=Keyboards.album_releases(releases[:per_page], album.lidarr_id, per_page=per_page),
        parse_mode="HTML",
    )


@router.callback_query(AlbumGrabCB.filter())
async def handle_album_grab(
    callback: CallbackQuery, callback_data: AlbumGrabCB, db_user: User, db: Database
) -> None:
    """Take one torrent release for the chosen album.

    Монитор включается ЗДЕСЬ, а не при добавлении артиста: паритет с
    `_execute_grab` у сериалов. Порядок — альбом, затем артист.
    """
    message = accessible_message(callback)
    if message is None:
        return

    user_id = callback.from_user.id
    session = await db.get_session(user_id)
    if not session or callback_data.idx >= len(session.results):
        await callback.answer("Список раздач истёк. Начните новый поиск.", show_alert=True)
        return

    services = await _get_music_services()
    if services is None:
        await callback.answer("Lidarr не настроен", show_alert=True)
        return
    _search_service, add_service = services

    release = session.results[callback_data.idx]
    await callback.answer("Беру...")
    ok, action = await add_service.grab_release(
        release, ContentType.MUSIC, arr_id=callback_data.album_id,
    )
    action.user_id = user_id
    await db.log_action(action)

    if not ok:
        await message.edit_text(
            Formatters.format_error(action.error_message or "Не удалось взять раздачу"),
        )
        return

    # Монитор — следствие успешного взятия, а не попытки.
    await add_service.lidarr.set_album_monitored(callback_data.album_id, True)
    artist = session.selected_content
    if isinstance(artist, ArtistInfo) and artist.lidarr_id:
        await add_service.lidarr.set_artist_monitored(artist.lidarr_id, True)

    await message.edit_text(
        Formatters.format_success(
            f"<b>{html.escape(release.title)}</b>\n\nВзято, альбом под мониторингом."
        ),
        parse_mode="HTML",
    )
    await db.delete_session(user_id)
    _album_candidates.pop(user_id, None)


@router.callback_query(F.data.startswith(CallbackData.ARTIST))
async def handle_legacy_artist(callback: CallbackQuery) -> None:
    """r5: legacy ``artist:N`` string buttons from messages sent before the
    ArtistCB migration — surface an explicit alert instead of falling
    through unhandled.
    """
    await callback.answer("Кнопка устарела — повторите поиск", show_alert=True)


async def handle_confirm_music_add(callback: CallbackQuery, db_user: User, db: Database) -> None:
    """Confirm add artist to Lidarr."""
    # RACE-01: share the per-user grab guard with the movie/series flow so a
    # double-tap can't add the artist twice. Lazy import avoids a module cycle.
    from bot.handlers.search import _claim_grab, _release_grab

    user_id = db_user.tg_id
    session = await db.get_session(user_id)
    if not session or not isinstance(session.selected_content, ArtistInfo):
        return  # Not a music flow — fall through to other CONFIRM_GRAB handlers

    if not await _claim_grab(user_id):
        await callback.answer("⏳ Уже обрабатываю предыдущий запрос…")
        return

    try:
        services = await _get_music_services()
        if services is None:
            await callback.answer("Lidarr не настроен", show_alert=True)
            return
        _search_service, add_service = services

        artist: ArtistInfo = session.selected_content
        prefs = db_user.preferences

        await callback.answer()
        message = accessible_message(callback)

        try:
            lidarr_id = artist.lidarr_id
            if lidarr_id is None:
                if message is not None:
                    await message.edit_text(
                        f"⏳ Смотрю дискографию <b>{html.escape(artist.name)}</b>...",
                        parse_mode="HTML",
                    )
                # PERF-07a: 3 independent RTTs → 1 wall-clock RTT.
                profiles, metadata_profiles, folders = await asyncio.gather(
                    add_service.get_lidarr_profiles(),
                    add_service.get_lidarr_metadata_profiles(),
                    add_service.get_lidarr_root_folders(),
                )

                if not profiles or not folders or not metadata_profiles:
                    if message is not None:
                        await message.edit_text(
                            Formatters.format_error(
                                "Нет профилей качества / папок / metadata-профилей в Lidarr",
                            ),
                        )
                    return

                added, action = await add_service.add_artist(
                    artist=artist,
                    quality_profile_id=AddService.resolve_profile(
                        profiles, prefs.lidarr_quality_profile_id).id,
                    metadata_profile_id=AddService.resolve_profile(
                        metadata_profiles, prefs.lidarr_metadata_profile_id).id,
                    root_folder_path=AddService.resolve_root_folder(
                        folders, prefs.lidarr_root_folder_id),
                    # Артист добавляется НЕМОНИТОРИМЫМ — ровно как сериал в
                    # `_resolve_arr_entry`: id нужен, чтобы спросить дискографию
                    # и релизы, но простой просмотр не должен втягивать все
                    # альбомы в RSS-петлю Lidarr. Живой замер 2026-08-12: при
                    # старом `monitor="all"` у одного артиста промониторено 22
                    # альбома, скачано 2. Монитор включается в момент взятия
                    # раздачи — см. `grab_album_release`.
                    monitored=False,
                    monitor="none",
                    search_for_missing=False,
                )
                action.user_id = user_id
                await db.log_action(action)
                if added is None or not added.lidarr_id:
                    if message is not None:
                        await message.edit_text(Formatters.format_error(
                            action.error_message or "Не удалось добавить артиста",
                        ))
                    return
                artist = added
                lidarr_id = added.lidarr_id
                # DB-02: тот же lock, что при выборе артиста.
                async with db.session_lock(user_id):
                    session.selected_content = artist
                    await db.save_session(user_id, session)

            albums = await add_service.lidarr.get_albums(lidarr_id)
            _remember(_album_candidates, user_id, albums)
            if message is not None:
                await _show_album_scope(message, artist, albums)
        except Exception as e:
            logger.error("Album scope failed", error=str(e), exc_info=True)
            if message is not None:
                await message.edit_text(Formatters.format_error("Операция временно недоступна"))
    finally:
        _release_grab(user_id)


# BUG-27: CONFIRM_GRAB is now dispatched from handlers/search.py by session type.
# music.py exposes handle_confirm_music_add; it is NOT registered as a separate
# callback handler (aiogram does not cascade — would silently swallow movie/series).


# =========================================================================
# Trending music (Deezer)
# =========================================================================


@router.callback_query(F.data == CallbackData.TRENDING_MUSIC)
async def handle_trending_music(callback: CallbackQuery) -> None:
    """Show trending artists from Deezer."""
    message = accessible_message(callback)
    if message is None:
        return

    await callback.answer("🔍 Загружаю...")

    deezer = await get_deezer()
    if deezer is None:
        await message.edit_text(Formatters.format_error("Deezer отключён"))
        return

    try:
        artists = await deezer.get_trending_artists(limit=10)
    except Exception as e:
        logger.error("Deezer trending failed", error=str(e), exc_info=True)
        await message.edit_text(Formatters.format_error("Не удалось загрузить трендовых артистов"))
        return

    if not artists:
        await message.edit_text(Formatters.format_warning("Нет данных от Deezer"))
        return

    user_id = callback.from_user.id
    _remember(_trending_artists_cache, user_id, artists)

    await message.edit_text(
        Formatters.format_trending_artists(artists),
        reply_markup=Keyboards.trending_artists(artists),
        parse_mode="HTML",
    )


@router.callback_query(TrendingItemCB.filter(F.kind == "artist"))
async def handle_trending_artist_click(
    callback: CallbackQuery, callback_data: TrendingItemCB, db_user: User, db: Database,
) -> None:
    """Click a trending artist → lookup in Lidarr and show details."""
    message = accessible_message(callback)
    if message is None:
        return

    try:
        idx = int(callback_data.item_id)
    except ValueError:
        await callback.answer("Неверный выбор", show_alert=True)
        return

    user_id = callback.from_user.id
    artists = _trending_artists_cache.get(user_id) or []
    if idx < 0 or idx >= len(artists):
        await callback.answer("Выбор истёк", show_alert=True)
        return

    name = artists[idx].get("name", "")
    await callback.answer()
    if (message := accessible_message(callback)) is not None:
        await message.edit_text(f"🔍 Ищу <b>{html.escape(name)}</b> в Lidarr...", parse_mode="HTML")
    await process_music_search(message, name, db_user, db)


@router.callback_query(F.data.startswith(CallbackData.TRENDING_ARTIST))
async def handle_legacy_trending_artist(callback: CallbackQuery) -> None:
    """r5: legacy ``trend_a:N`` string buttons from messages sent before the
    TrendingItemCB migration — surface an explicit alert instead of falling
    through unhandled.
    """
    await callback.answer("Кнопка устарела — обновите список", show_alert=True)
