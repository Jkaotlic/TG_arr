"""Music (Lidarr) handlers: search artists, view details, add to Lidarr."""

import asyncio
import hashlib
import html
from collections.abc import Callable
from typing import Optional

import structlog
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, ForceReply, InlineKeyboardMarkup, Message

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
    AlbumRefreshCB,
    AlbumRelPageCB,
    AlbumReleaseCB,
    AlbumScopeCB,
    AlbumSourceCB,
    ArtistCB,
    DiscographyCB,
    MusicAddCB,
    MusicRetryCB,
    SlskdCardCB,
    SlskdCB,
    SlskdPageCB,
    TrendingArtistCB,
    TrendingItemCB,
)
from bot.ui.formatters import Formatters
from bot.ui.keyboards import CallbackData, Keyboards
from bot.ui.keyboards._constants import MSG_OLD_BUTTON
from bot.ui.list_token import ListTokenRegistry, answer_stale, new_list_token, token_matches
from bot.ui.menu import MENU_BUTTONS, MENU_MUSIC, MUSIC_PROMPT

logger = structlog.get_logger()
router = Router()
#: Replies to the "🎵 Музыка" prompt. Its own router so setup_routers can put
#: it before search_router, whose text handler claims every plain text.
prompt_router = Router()

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
# BUG-18: кнопки этих двух списков несут индекс, поэтому и токен: иначе старая кнопка
# открывает элемент с тем же номером из более нового списка.
_artist_tokens = ListTokenRegistry()
_trending_artist_tokens = ListTokenRegistry()


def _artist_list_text(artists: list[ArtistInfo], page_artists: list[ArtistInfo], start_idx: int) -> str:
    """LOGIC-14a: render the numbered artist list body shared by the initial
    search results, pagination and the music-back handler."""
    lines = [f"🎵 <b>Найдено артистов: {len(artists)}</b>\n"]
    for i, a in enumerate(page_artists, start=start_idx):
        disamb = f" <i>[{html.escape(a.disambiguation)}]</i>" if a.disambiguation else ""
        in_lib = " ✅" if a.lidarr_id else ""
        lines.append(f"{i + 1}. <b>{html.escape(a.name)}</b>{disamb}{in_lib}")
    return "\n".join(lines)


async def _render_artist_list(
    message: Message, artists: list[ArtistInfo], list_token: str, page: int = 0,
) -> None:
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
        reply_markup=Keyboards.artist_list(artists, current_page=page, per_page=per_page, list_token=list_token),
        parse_mode="HTML",
    )


# Дискография у только что добавленного артиста приходит не сразу: Lidarr тянет
# её фоновой командой RefreshArtist. Живой пробник 2026-08-12 — сразу после
# add_artist ноль альбомов, спустя минуты они есть. Ждать минуты в колбэке
# нельзя (окно Telegram ~15 с), поэтому несколько коротких попыток, а дальше —
# кнопка «Обновить дискографию».
_ALBUM_RETRIES = 3
_ALBUM_RETRY_DELAY = 1.5


async def _albums_with_retry(lidarr, artist_id: int) -> list[AlbumInfo]:
    """Дискография артиста; повторяет запрос, пока Lidarr отвечает пустотой."""
    albums: list[AlbumInfo] = []
    for attempt in range(_ALBUM_RETRIES):
        albums = await lidarr.get_albums(artist_id)
        if albums:
            return albums
        if attempt < _ALBUM_RETRIES - 1:
            await asyncio.sleep(_ALBUM_RETRY_DELAY)
    return albums


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


def _same_music_flow(fresh: SearchSession | None, expected: SearchSession) -> bool:
    """Сессия в базе всё ещё тот самый поток: музыка, тот же артист, тот же токен.

    BUG-05: после долгого ожидания (поиск раздач, добавление артиста) пользователь
    мог начать другой запрос или нажать «Отмена».
    """
    return (
        fresh is not None
        and fresh.content_type is ContentType.MUSIC
        and isinstance(fresh.selected_content, ArtistInfo)
        and isinstance(expected.selected_content, ArtistInfo)
        and fresh.list_token == expected.list_token
        and _artist_fingerprint(fresh.selected_content) == _artist_fingerprint(expected.selected_content)
    )


async def _update_same_flow(
    db: Database, user_id: int, expected: SearchSession, mutate: Callable[[SearchSession], None],
) -> SearchSession | None:
    """Внести правку в сессию, только если поток тот же; иначе ``None``.

    Читает заново под замком и пишет через ``update_session``: старая копия,
    прочитанная до долгого await, не затирает новое состояние, а отменённая
    сессия не воскресает (``update_session`` не создаёт строку).
    """
    async with db.session_lock(user_id):
        fresh = await db.get_session(user_id)
        if fresh is None or not _same_music_flow(fresh, expected):
            return None
        mutate(fresh)
        if not await db.update_session(user_id, fresh):
            return None
        return fresh


def _album_sources_keyboard(user_id: int, artist: ArtistInfo, album: AlbumInfo, artist_id: int = 0):
    """«Где искать?» для альбома: «Назад» ведёт на страницу списка, где альбом стоит.

    BUG-04: после пустого поиска торрентов клавиатура собиралась без артиста и
    страницы, «Назад» давал ``alpg:0:0``, а «Вся дискография» отвечала «Список
    устарел». Теперь оба экрана строятся одним местом.
    """
    ordered = Keyboards.album_order(_album_candidates.get(user_id) or [])
    position = next((i for i, a in enumerate(ordered) if a.lidarr_id == album.lidarr_id), 0)
    return Keyboards.album_sources(
        album.lidarr_id,
        artist_id=artist.lidarr_id or artist_id,
        page=position // get_settings().results_per_page,
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
# Audit 2026-09-23 GRAB-03: token of each user's current Soulseek list — a
# button from an older search must not enqueue the same index of a newer one.
_slskd_tokens = ListTokenRegistry(max_users=_MAX_ARTIST_CANDIDATES)
# The query behind each user's Soulseek list — the page header (UI-10).
_slskd_queries: dict[int, str] = {}


def _per_page() -> int:
    return get_settings().results_per_page


def _total_pages(count: int, per_page: int) -> int:
    return max(1, (count + per_page - 1) // per_page)


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
        # GRAB-18: an album search ranks complete folders first; a track
        # search ranks by format.
        results = await slskd.search(query, limit=25, prefer_complete=kind != "track")
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
    _remember(_slskd_queries, user_id, query)
    list_token = _slskd_tokens.issue(user_id)

    await db.log_action(
        ActionLog(
            user_id=user_id,
            action_type=ActionType.SEARCH,
            content_type=ContentType.MUSIC,
            query=query,
        )
    )

    note = await _navidrome_note(results[0].guessed_artist, query if kind == "album" else "")
    per_page = _per_page()
    await status_msg.edit_text(
        Formatters.format_slskd_results(results, query, page=0, per_page=per_page) + note,
        reply_markup=Keyboards.slskd_results(results, page=0, per_page=per_page, list_token=list_token),
        parse_mode="HTML",
    )


def _slskd_list(user_id: int, tok: str) -> list | None:
    """The user's Soulseek candidates if ``tok`` names that list, else None."""
    candidates = _slskd_candidates.get(user_id) or []
    if not candidates or not _slskd_tokens.check(user_id, tok):
        return None
    return candidates


@router.callback_query(SlskdPageCB.filter())
async def handle_slskd_page(
    callback: CallbackQuery, callback_data: SlskdPageCB, db_user: User, db: Database,
) -> None:
    """Audit UI-10: page through Soulseek candidates (same list, same token)."""
    message = accessible_message(callback)
    if message is None:
        return
    user_id = callback.from_user.id
    candidates = _slskd_list(user_id, callback_data.tok)
    if candidates is None:
        await answer_stale(callback)
        return
    per_page = _per_page()
    if callback_data.page >= _total_pages(len(candidates), per_page):
        await callback.answer("Неверная страница", show_alert=True)
        return
    await callback.answer()
    await safe_edit(
        message,
        Formatters.format_slskd_results(
            candidates, _slskd_queries.get(user_id, ""), page=callback_data.page, per_page=per_page,
        ),
        reply_markup=Keyboards.slskd_results(
            candidates, page=callback_data.page, per_page=per_page, list_token=callback_data.tok,
        ),
        parse_mode="HTML",
    )


@router.callback_query(SlskdCardCB.filter())
async def handle_slskd_card(
    callback: CallbackQuery, callback_data: SlskdCardCB, db_user: User, db: Database,
) -> None:
    """Audit UI-03: a row opens the candidate's card; "✅ Скачать" there
    enqueues (``SlskdCB``)."""
    message = accessible_message(callback)
    if message is None:
        return
    user_id = callback.from_user.id
    candidates = _slskd_list(user_id, callback_data.tok)
    if candidates is None or callback_data.idx >= len(candidates):
        await answer_stale(callback)
        return
    await callback.answer()
    await safe_edit(
        message,
        Formatters.format_slskd_candidate(candidates[callback_data.idx]),
        reply_markup=Keyboards.slskd_card(callback_data.idx, callback_data.tok, per_page=_per_page()),
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
    if not _slskd_tokens.check(user_id, callback_data.tok) or idx >= len(candidates):
        await answer_stale(callback)
        return

    # Double tap (audit GRAB-16 part): the same per-user guard as every grab.
    from bot.handlers.search import _claim_grab, _release_grab

    if not await _claim_grab(user_id):
        await callback.answer("⏳ Уже обрабатываю предыдущий запрос…")
        return
    try:
        # Re-check after the claim: a finished first tap drops the token.
        candidates = _slskd_list(user_id, callback_data.tok) or []
        if idx >= len(candidates):
            await answer_stale(callback)
            return
        await _enqueue_slskd_candidate(
            callback, message, db, user_id, candidates[idx],
            retry_markup=Keyboards.slskd_card(idx, callback_data.tok, per_page=_per_page()),
        )
    finally:
        _release_grab(user_id)


async def _enqueue_slskd_candidate(
    callback: CallbackQuery, message: Message, db: Database, user_id: int, candidate,
    retry_markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    """Queue one Soulseek candidate (the guarded body of the selection).

    ``retry_markup`` — the candidate card's keyboard (✅ Скачать / ◀️ Назад),
    put back under a failure message so the user can retry or go back
    instead of hitting a dead end (final review 2026-09-24). The list token
    is only dropped on success, so the retry button stays valid.
    """
    await callback.answer("Ставлю в очередь...")

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

    # Audit GRAB-16: say so before the API call — a second tap meanwhile
    # sees "⏳" and the claim, not a live button.
    await message.edit_text("⏳ Ставлю в очередь Soulseek…")
    try:
        ok = await slskd.enqueue(candidate.username, candidate.files)
    except Exception as e:
        logger.error("slskd_enqueue_failed", error=str(e), exc_info=True)
        action.success = False
        action.error_message = str(e)[:200]
        await db.log_action(action)
        await message.edit_text(
            Formatters.format_error("Soulseek не ответил — очередь не изменилась, попробуйте ещё раз"),
            reply_markup=retry_markup,
        )
        return
    action.success = ok
    if not ok:
        action.error_message = "slskd rejected the transfer"
    else:
        # Taken: the list has done its job, its buttons are stale from now on.
        _slskd_tokens.drop(user_id)
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
            ),
            reply_markup=retry_markup,
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
    # Audit UI-13: the answer used to go to content-type auto-detection like
    # any text; a ForceReply ties it to the artist search (same pattern as
    # TORRSERVER_PROMPT).
    await message.answer(
        MUSIC_PROMPT,
        reply_markup=ForceReply(input_field_placeholder="Имя артиста"),
    )


# A command typed as the reply ("/downloads") is a command, not an artist
# name — let it fall through to its own router (final review 2026-09-24).
@prompt_router.message(
    F.reply_to_message.text == MUSIC_PROMPT,
    ~F.text.startswith("/"),
    ~F.text.in_(MENU_BUTTONS),  # BUG-14: нажатая кнопка меню — не имя артиста
)
async def handle_music_prompt_reply(message: Message, db_user: User, db: Database) -> None:
    """The artist name typed in reply to the "🎵 Музыка" prompt."""
    await process_music_search(message, (message.text or "").strip(), db_user, db)


async def process_music_search(
    message: Message,
    query: str,
    db_user: User,
    db: Database,
    status_msg: Optional[Message] = None,
) -> None:
    """Look up artists in Lidarr and present selection list.

    ``status_msg`` — a bot message to reuse for progress and results (the
    type-choice question, the trending list): audit UI-14, the flow used to
    leave it hanging with live buttons and add a new "Ищу…" message.
    """
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

    if status_msg is None:
        status_msg = await message.answer("🔍 Ищу артистов в Lidarr...")
    else:
        await status_msg.edit_text("🔍 Ищу артистов в Lidarr...")

    try:
        artists = await search_service.lookup_artist(query)
    except Exception as e:
        log.error("Artist lookup failed", error=str(e), exc_info=True)
        # BUG-32: как и при «Артист не найден», чужая сессия не должна оживать от
        # кнопок, которые остались в чате.
        await db.delete_session(user_id)
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

    await _render_artist_list(
        status_msg, _artist_candidates[user_id], page=0, list_token=_artist_tokens.issue(user_id),
    )


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

    await _render_artist_list(message, artists, page=page, list_token=_artist_tokens.current(user_id))
    await callback.answer()


@router.callback_query(F.data == CallbackData.MUSIC_BACK)
async def handle_music_back(callback: CallbackQuery, db_user: User, db: Database) -> None:
    """LOGIC-24: back from artist_details → return to artist_list — on the
    page the artist was picked from (audit UI-08/UI-09)."""
    message = accessible_message(callback)
    if message is None:
        return
    user_id = callback.from_user.id
    artists = _artist_candidates.get(user_id) or []
    if not artists:
        await callback.answer("Список истёк. Начните новый поиск.", show_alert=True)
        return

    session = await db.get_session(user_id)
    chosen = session.selected_content if session else None
    position = next(
        (i for i, a in enumerate(artists)
         if isinstance(chosen, ArtistInfo) and a.mb_id == chosen.mb_id),
        0,
    )
    await _render_artist_list(
        message, artists,
        page=position // get_settings().results_per_page,
        list_token=_artist_tokens.current(user_id),
    )
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
    if not _artist_tokens.check(user_id, callback_data.tok) or idx >= len(artists):
        await answer_stale(callback)
        return

    artist = artists[idx]
    # BUG-15: дискография прежнего артиста не должна пережить выбор нового.
    _album_candidates.pop(user_id, None)

    # DB-02: lock the read-modify-write cycle around storing the selected artist.
    async with db.session_lock(user_id):
        session = await db.get_session(user_id)
        if session is None or session.content_type is not ContentType.MUSIC:
            session = SearchSession(user_id=user_id, query=artist.name, content_type=ContentType.MUSIC)
        session.selected_content = artist
        # Audit GRAB-03: the artist card is its own "list" — its button must
        # not add a different artist picked later in another message.
        session.list_token = new_list_token()
        await db.save_session(user_id, session)

    await callback.answer()
    await message.edit_text(
        Formatters.format_artist_info(artist),
        reply_markup=Keyboards.artist_details(
            artist, already_in_library=bool(artist.lidarr_id), list_token=session.list_token,
        ),
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
        # Audit GRAB-03: the picker belongs to one artist.
        if artist.lidarr_id and callback_data.artist_id != artist.lidarr_id:
            await answer_stale(callback)
            return
        # Audit UI-11: the whole discography (tens of albums, tens of GB) is
        # confirmed first; the confirm button carries a fresh token.
        artist_id = artist.lidarr_id or callback_data.artist_id
        # Round 2 / D3: re-read under the lock — saving the copy read above
        # would overwrite whatever another handler wrote in between.
        async with db.session_lock(user_id):
            locked = await db.get_session(user_id)
            if (
                locked is None
                or not isinstance(locked.selected_content, ArtistInfo)
                or _artist_fingerprint(locked.selected_content) != _artist_fingerprint(artist)
            ):
                await answer_stale(callback)
                return
            locked.list_token = new_list_token()
            await db.save_session(user_id, locked)
            list_token = locked.list_token
        await callback.answer()
        await message.edit_text(
            f"💿 <b>{html.escape(artist.name)}</b> — вся дискография?\n\n"
            "Lidarr возьмёт под мониторинг все альбомы артиста и начнёт их искать. "
            "Это может быть много гигабайт.",
            reply_markup=Keyboards.discography_confirm(
                artist_id, list_token, artist_fp=_artist_fingerprint(artist),
            ),
            parse_mode="HTML",
        )
        return

    # BUG-15: кнопка альбома принадлежит артисту, под которым нарисован список.
    # Без сверки кнопка артиста A при сессии артиста B открывала альбом A, а
    # торрент-граб потом мониторил артиста B.
    if artist.lidarr_id and callback_data.artist_id != artist.lidarr_id:
        await answer_stale(callback)
        return
    album = _find_album(user_id, callback_data.album_id)
    if album is None:
        await callback.answer("Список альбомов истёк. Начните новый поиск.", show_alert=True)
        return

    await callback.answer()
    year = f" · {album.year}" if album.year else ""
    # Audit UI-08: "Назад" goes to the album page this album sits on.
    await message.edit_text(
        f"💿 <b>{html.escape(album.title)}</b>{year}\n\nГде искать?",
        reply_markup=_album_sources_keyboard(user_id, artist, album, callback_data.artist_id),
        parse_mode="HTML",
    )


def _artist_fingerprint(artist: ArtistInfo) -> str:
    """Short stable id of an artist for callback data (round 2 / D3): the
    MusicBrainz id, else the name — for artists without a Lidarr id."""
    key = (artist.mb_id or artist.name or "").strip().casefold()
    return hashlib.sha1(key.encode()).hexdigest()[:8]


def _discography_session_ok(session: SearchSession | None, callback_data: DiscographyCB) -> bool:
    """The confirm button still belongs to the artist and screen the session holds.

    Round 2 / D3: without a Lidarr id the id check degenerates to ``0 == 0``,
    so the artist fingerprint carried by the button must match instead.
    """
    if session is None or not isinstance(session.selected_content, ArtistInfo):
        return False
    artist = session.selected_content
    if not token_matches(session.list_token, callback_data.tok):
        return False
    if artist.lidarr_id:
        return artist.lidarr_id == callback_data.artist_id
    return bool(callback_data.fp) and callback_data.fp == _artist_fingerprint(artist)


@router.callback_query(DiscographyCB.filter())
async def handle_discography(
    callback: CallbackQuery, callback_data: DiscographyCB, db_user: User, db: Database,
) -> None:
    """"✅ Да, вся дискография" (audit UI-11/GRAB-16): claimed against a
    double tap, "⏳" before the API, and a Lidarr failure is said out loud."""
    message = accessible_message(callback)
    if message is None:
        return
    user_id = callback.from_user.id
    session = await db.get_session(user_id)
    if not _discography_session_ok(session, callback_data):
        await answer_stale(callback)
        return

    services = await _get_music_services()
    if services is None:
        await callback.answer("Lidarr не настроен", show_alert=True)
        return
    _search_service, add_service = services

    from bot.handlers.search import _claim_grab, _release_grab

    if not await _claim_grab(user_id):
        await callback.answer("⏳ Уже обрабатываю предыдущий запрос…")
        return
    try:
        # Re-read after the claim: a finished first tap deleted the session.
        session = await db.get_session(user_id)
        if not _discography_session_ok(session, callback_data):
            await answer_stale(callback)
            return
        assert session is not None and isinstance(session.selected_content, ArtistInfo)
        artist = session.selected_content
        artist_id = callback_data.artist_id
        await callback.answer("Беру всю дискографию...")
        await message.edit_text("⏳ Включаю мониторинг и поиск дискографии…")
        try:
            # Монитор и поиск делегируются Lidarr: он сам решает, что «все
            # альбомы» значит для этого артиста по его metadata-профилю.
            await add_service.lidarr.set_artist_monitored(artist_id, True)
            await add_service.lidarr.search_artist(artist_id)
        except Exception as e:
            logger.error("discography_grab_failed", artist_id=artist_id, error=str(e), exc_info=True)
            await message.edit_text(Formatters.format_error(
                "Lidarr не ответил — дискография не поставлена в поиск. Попробуйте позже."
            ))
            return
        await message.edit_text(
            Formatters.format_success(
                f"<b>{html.escape(artist.name)}</b>\n\n"
                "Вся дискография под мониторингом, запущен автопоиск."
            ),
            parse_mode="HTML",
        )
        await db.delete_session(user_id)
        _album_candidates.pop(user_id, None)
    finally:
        _release_grab(user_id)


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
            reply_markup=_album_sources_keyboard(user_id, artist, album),
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
            reply_markup=_album_sources_keyboard(user_id, artist, album),
            parse_mode="HTML",
        )
        return

    # Живой замер 2026-08-12: 127 раздач на альбом, 24 из них Lidarr отверг
    # («Album wasn't requested», «Wrong album») — отвергнутые уходят вниз.
    # Audit 2026-09-23 GRAB-15: внутри групп — порядок самого Lidarr (он уже
    # отсортировал по профилю качества), а не по сидам: пересортировка по сидам
    # ставила MP3 выше FLAC, который профиль хотел. Сортировка стабильна.
    releases.sort(key=lambda r: r.rejected)

    def _store_releases(fresh: SearchSession) -> None:
        fresh.results = releases
        fresh.current_page = 0
        fresh.list_token = new_list_token()  # audit GRAB-03: new list, new token

    # BUG-05: Lidarr отвечал долго; пока ждали, пользователь мог начать другой
    # поиск или отменить этот. Старую копию сессии поверх не пишем и чужое
    # сообщение не правим.
    stored = await _update_same_flow(db, user_id, session, _store_releases)
    if stored is None:
        logger.info("album_releases_dropped_flow_changed", user_id=user_id)
        return

    await _render_album_releases(message, stored, album.lidarr_id, page=0)


def _album_label(user_id: int, session: SearchSession, album_id: int) -> str:
    artist = session.selected_content
    album = _find_album(user_id, album_id)
    name = artist.name if isinstance(artist, ArtistInfo) else ""
    return f"{name} — {album.title}" if album else name


async def _render_album_releases(
    message: Message, session: SearchSession, album_id: int, page: int,
) -> None:
    """One page of the album's torrent releases (audit UI-10)."""
    per_page = _per_page()
    releases = session.results
    artist = session.selected_content
    await safe_edit(
        message,
        Formatters.format_search_results_page(
            releases[page * per_page:(page + 1) * per_page], page,
            _total_pages(len(releases), per_page),
            _album_label(session.user_id, session, album_id), ContentType.MUSIC, per_page=per_page,
        ),
        reply_markup=Keyboards.album_releases(
            releases, album_id, page=page, per_page=per_page, list_token=session.list_token,
            artist_id=(artist.lidarr_id or 0) if isinstance(artist, ArtistInfo) else 0,
        ),
        parse_mode="HTML",
    )


def _album_list_ok(session: SearchSession | None, tok: str) -> bool:
    """Audit GRAB-03: the button names the album-release list this music
    session holds now (a movie/series session shares the index space)."""
    return bool(
        session
        and session.content_type is ContentType.MUSIC
        and isinstance(session.selected_content, ArtistInfo)
        and session.results
        and token_matches(session.list_token, tok)
    )


@router.callback_query(AlbumRelPageCB.filter())
async def handle_album_release_page(
    callback: CallbackQuery, callback_data: AlbumRelPageCB, db_user: User, db: Database,
) -> None:
    """Audit UI-10: ◀️ N/M ▶️ through an album's releases (same list)."""
    message = accessible_message(callback)
    if message is None:
        return
    session = await db.get_session(callback.from_user.id)
    if session is None or not _album_list_ok(session, callback_data.tok):
        await answer_stale(callback)
        return
    if callback_data.page >= _total_pages(len(session.results), _per_page()):
        await callback.answer("Неверная страница", show_alert=True)
        return
    await callback.answer()
    await _render_album_releases(message, session, callback_data.album_id, callback_data.page)


@router.callback_query(AlbumReleaseCB.filter())
async def handle_album_release_card(
    callback: CallbackQuery, callback_data: AlbumReleaseCB, db_user: User, db: Database,
) -> None:
    """Audit UI-03: a row tap opens the release card — format, size, seeders,
    Lidarr's verdict — instead of grabbing."""
    message = accessible_message(callback)
    if message is None:
        return
    session = await db.get_session(callback.from_user.id)
    if (
        session is None
        or not _album_list_ok(session, callback_data.tok)
        or callback_data.idx >= len(session.results)
    ):
        await answer_stale(callback)
        return
    await callback.answer()
    await _show_album_release_card(message, session, callback_data.idx, callback_data.album_id)


async def _show_album_release_card(
    message: Message, session: SearchSession, idx: int, album_id: int,
) -> None:
    await safe_edit(
        message,
        Formatters.format_album_release(session.results[idx]),
        reply_markup=Keyboards.album_release_card(
            session.results[idx], idx, album_id, session.list_token, per_page=_per_page(),
        ),
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
    # Audit GRAB-03: the button names the album-release list it was drawn for;
    # a movie/series session (same index space!) or a newer list is stale.
    if (
        session is None
        or not _album_list_ok(session, callback_data.tok)
        or callback_data.idx >= len(session.results)
    ):
        await answer_stale(callback)
        return

    # GRAB-02 (B1 pattern): a release Lidarr rejects needs the explicit
    # "⚠️ Всё равно скачать" (ok=1) from its card.
    if session.results[callback_data.idx].rejected and not callback_data.ok:
        await callback.answer()
        await _show_album_release_card(message, session, callback_data.idx, callback_data.album_id)
        return

    services = await _get_music_services()
    if services is None:
        await callback.answer("Lidarr не настроен", show_alert=True)
        return
    _search_service, add_service = services

    from bot.handlers.search import _claim_grab, _release_grab

    if not await _claim_grab(user_id):
        await callback.answer("⏳ Уже обрабатываю предыдущий запрос…")
        return
    try:
        # Re-read after the claim: a finished first tap deleted the session.
        session = await db.get_session(user_id)
        if (
            session is None
            or not _album_list_ok(session, callback_data.tok)
            or callback_data.idx >= len(session.results)
        ):
            await answer_stale(callback)
            return
        await _grab_album_release(callback, callback_data, message, session, add_service, db, user_id)
    finally:
        _release_grab(user_id)


async def _grab_album_release(
    callback: CallbackQuery,
    callback_data: AlbumGrabCB,
    message: Message,
    session: SearchSession,
    add_service: AddService,
    db: Database,
    user_id: int,
) -> None:
    """The guarded body of `handle_album_grab`."""
    release = session.results[callback_data.idx]
    await callback.answer("Беру...")
    # Audit GRAB-16: "⏳" before the API call, so the card's button is gone
    # while Lidarr works.
    await message.edit_text(f"⏳ Беру раздачу: <i>{html.escape(release.title)}</i>…", parse_mode="HTML")
    try:
        ok, action = await add_service.grab_release(
            release, ContentType.MUSIC, arr_id=callback_data.album_id,
        )
    except Exception as e:
        logger.error("album_grab_failed", error=str(e), exc_info=True)
        await message.edit_text(
            Formatters.format_error("Lidarr не ответил — раздача не взята, попробуйте ещё раз"),
        )
        return
    action.user_id = user_id
    await db.log_action(action)

    if not ok:
        await message.edit_text(
            Formatters.format_error(action.error_message or "Не удалось взять раздачу"),
        )
        return

    # Монитор — следствие успешного взятия, а не попытки. Сбой здесь не
    # отменяет взятую раздачу — логируется, как в `_execute_grab`.
    try:
        await add_service.lidarr.set_album_monitored(callback_data.album_id, True)
        artist = session.selected_content
        if isinstance(artist, ArtistInfo) and artist.lidarr_id:
            await add_service.lidarr.set_artist_monitored(artist.lidarr_id, True)
    except Exception as e:
        logger.warning("album_monitor_enable_failed", album_id=callback_data.album_id, error=str(e))

    await message.edit_text(
        Formatters.format_success(
            f"<b>{html.escape(release.title)}</b>\n\nВзято, альбом под мониторингом."
        ),
        parse_mode="HTML",
    )
    await db.delete_session(user_id)
    _album_candidates.pop(user_id, None)


@router.callback_query(AlbumRefreshCB.filter())
async def handle_album_refresh(
    callback: CallbackQuery, callback_data: AlbumRefreshCB, db_user: User, db: Database
) -> None:
    """Re-ask Lidarr for the discography after RefreshArtist has had time."""
    message = accessible_message(callback)
    if message is None:
        return

    user_id = callback.from_user.id
    session = await db.get_session(user_id)
    if not session or not isinstance(session.selected_content, ArtistInfo):
        await callback.answer("Сессия истекла. Начните новый поиск.", show_alert=True)
        return

    # Аудит 2026-08-13: артиста задаёт сессия, а не callback_data. Иначе
    # подменённый колбэк показывал дискографию чужого артиста под заголовком
    # выбранного — заголовок и содержимое расходились.
    artist = session.selected_content
    if callback_data.artist_id != artist.lidarr_id:
        await callback.answer("Кнопка от другого артиста. Начните новый поиск.", show_alert=True)
        return

    services = await _get_music_services()
    if services is None:
        await callback.answer("Lidarr не настроен", show_alert=True)
        return
    _search_service, add_service = services

    await callback.answer("Спрашиваю Lidarr...")
    albums = await add_service.lidarr.get_albums(artist.lidarr_id)
    _remember(_album_candidates, user_id, albums)
    await _show_album_scope(message, artist, albums)


@router.callback_query(F.data.startswith(CallbackData.ARTIST))
async def handle_legacy_artist(callback: CallbackQuery) -> None:
    """r5: legacy ``artist:N`` string buttons from messages sent before the
    ArtistCB migration — surface an explicit alert instead of falling
    through unhandled.
    """
    await answer_stale(callback)


@router.callback_query(MusicAddCB.filter())
async def handle_music_add(
    callback: CallbackQuery, callback_data: MusicAddCB, db_user: User, db: Database,
) -> None:
    """"🔍 Искать" on an artist card (audit 2026-09-23: was the shared
    ``confirm_grab``, which a movie session routed to a movie grab).

    The token ties the button to the artist card it sits on — a card left in
    an old message must not add the artist picked later.
    """
    session = await db.get_session(callback.from_user.id)
    if (
        not session
        or not isinstance(session.selected_content, ArtistInfo)
        or not token_matches(session.list_token, callback_data.tok)
    ):
        await answer_stale(callback)
        return
    await handle_confirm_music_add(callback, db_user, db)


@router.callback_query(F.data.startswith("sk:") | F.data.startswith("alg:"))
async def handle_legacy_music_pick(callback: CallbackQuery) -> None:
    """Audit 2026-09-23: ``sk:N`` / ``alg:N:ALBUM`` without a list token —
    buttons from messages sent by the previous version."""
    await answer_stale(callback)


async def handle_confirm_music_add(callback: CallbackQuery, db_user: User, db: Database) -> None:
    """Confirm add artist to Lidarr."""
    # RACE-01: share the per-user grab guard with the movie/series flow so a
    # double-tap can't add the artist twice. Lazy import avoids a module cycle.
    from bot.handlers.search import _claim_grab, _release_grab

    user_id = db_user.tg_id
    session = await db.get_session(user_id)
    if not session or not isinstance(session.selected_content, ArtistInfo):
        return  # Not a music flow (handle_music_add checks this before calling)

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
                # DB-02/BUG-05: тот же lock, что при выборе артиста; добавление
                # шло долго, и поток мог смениться или быть отменён.
                def _store_artist(fresh: SearchSession) -> None:
                    fresh.selected_content = added

                if await _update_same_flow(db, user_id, session, _store_artist) is None:
                    logger.info("music_add_dropped_flow_changed", user_id=user_id)
                    return

            albums = await _albums_with_retry(add_service.lidarr, lidarr_id)
            if not _same_music_flow(await db.get_session(user_id), session):
                logger.info("music_albums_dropped_flow_changed", user_id=user_id)
                return
            _remember(_album_candidates, user_id, albums)
            if message is not None:
                await _show_album_scope(message, artist, albums)
        except Exception as e:
            logger.error("Album scope failed", error=str(e), exc_info=True)
            if message is not None:
                await message.edit_text(Formatters.format_error("Операция временно недоступна"))
    finally:
        _release_grab(user_id)


# BUG-27 / audit 2026-09-23: the artist card's button is `MusicAddCB`
# (`handle_music_add` above), its own prefix — no longer the `confirm_grab`
# string it once shared with the movie/series release card.


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
        reply_markup=Keyboards.trending_artists(artists, list_token=_trending_artist_tokens.issue(user_id)),
        parse_mode="HTML",
    )


@router.callback_query(TrendingArtistCB.filter())
async def handle_trending_artist_click(
    callback: CallbackQuery, callback_data: TrendingArtistCB, db_user: User, db: Database,
) -> None:
    """Click a trending artist → lookup in Lidarr and show details."""
    message = accessible_message(callback)
    if message is None:
        return

    idx = callback_data.idx
    user_id = callback.from_user.id
    artists = _trending_artists_cache.get(user_id) or []
    if not _trending_artist_tokens.check(user_id, callback_data.tok) or idx >= len(artists):
        await answer_stale(callback)
        return

    name = artists[idx].get("name", "")
    await callback.answer()
    # Audit UI-14: the results replace the trending list in this message.
    await process_music_search(message, name, db_user, db, status_msg=message)


@router.callback_query(TrendingItemCB.filter(F.kind == "artist"))
async def handle_legacy_indexed_trending_artist(callback: CallbackQuery) -> None:
    """BUG-18: ``tri:artist:N`` без токена не знает, из какого чарта этот номер."""
    await answer_stale(callback)


@router.callback_query(F.data.startswith(CallbackData.TRENDING_ARTIST))
async def handle_legacy_trending_artist(callback: CallbackQuery) -> None:
    """r5: legacy ``trend_a:N`` string buttons from messages sent before the
    TrendingItemCB migration — surface an explicit alert instead of falling
    through unhandled.
    """
    await callback.answer(MSG_OLD_BUTTON, show_alert=True)


#: M1: answers to a «🔁 Докачать» tap, by `MusicRetryService.retry` outcome.
_RETRY_ANSWERS = {
    "started": "🔁 Lidarr ищет альбом заново",
    "already": "🔁 Lidarr уже ищет альбом заново",
    "unavailable": "Lidarr не настроен",
    "failed": "❌ Lidarr не ответил — попробуйте позже",
}


@router.callback_query(MusicRetryCB.filter())
async def handle_music_retry(callback: CallbackQuery, callback_data: MusicRetryCB, db: Database) -> None:
    """M1 (improvements round 2): «🔁 Докачать» under a music summary sends
    Lidarr's AlbumSearch for the album behind the token. Double taps (and
    other recipients of the same summary) within the cooldown don't search
    twice; after a started search the pressed button leaves this message."""
    from bot.services.music_retry import MusicRetryService

    result = await MusicRetryService(db, get_lidarr).retry(callback_data.tok)
    if result == "stale":
        await callback.answer(MSG_OLD_BUTTON, show_alert=True)
        return
    await callback.answer(_RETRY_ANSWERS[result], show_alert=result in ("unavailable", "failed"))
    if result != "started":
        return
    message = accessible_message(callback)
    markup = message.reply_markup if message is not None else None
    if message is None or markup is None:
        return
    pressed = callback_data.pack()
    rows = [
        [b for b in row if b.callback_data != pressed] for row in markup.inline_keyboard
    ]
    rows = [row for row in rows if row]
    try:
        await message.edit_reply_markup(
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None,
        )
    except Exception as e:  # "message is not modified", too old, …: cosmetic only
        logger.debug("music_retry_markup_edit_failed", error=str(e))
