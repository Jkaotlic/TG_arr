"""Search and content management handlers."""

import asyncio
import html
import time

import structlog
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot.config import get_settings
from bot.db import Database
from bot.models import (
    ActionLog,
    ActionType,
    ContentType,
    MovieInfo,
    SearchSession,
    SeriesInfo,
    User,
)
from bot.clients.registry import get_emby, get_lidarr, get_prowlarr, get_qbittorrent, get_radarr, get_sonarr
from bot.services.add_service import AddService
from bot.services.scoring import ScoringService
from bot.services.search_service import SearchService
from bot.ui.callbacks import PageCB
from bot.ui.formatters import Formatters
from bot.ui.keyboards import CallbackData, Keyboards

logger = structlog.get_logger()
router = Router()

# PERF-04: Singleton ScoringService shared across all requests.
# ScoringService is stateless (only holds pre-compiled weights), so one instance is safe.
_SCORING_SERVICE = ScoringService()

# RACE-01: guard against double-grab. aiogram dispatches each callback as its own
# task, so a rapid double-tap (or Confirm→Force) would run the grab twice. A
# per-user in-progress claim makes the second concurrent grab a no-op.
_grab_in_progress: set[int] = set()
_grab_guard_lock = asyncio.Lock()


async def _claim_grab(user_id: int) -> bool:
    """Claim the grab slot for a user. Returns False if one is already in flight."""
    async with _grab_guard_lock:
        if user_id in _grab_in_progress:
            return False
        _grab_in_progress.add(user_id)
        return True


def _release_grab(user_id: int) -> None:
    """Release a user's grab slot (safe to call even if not held)."""
    _grab_in_progress.discard(user_id)

# Russian menu button texts
MENU_SEARCH = "🔍 Поиск"

# All menu button texts that should NOT trigger text search
# These are handled by their respective routers
MENU_BUTTONS = {
    MENU_SEARCH,
    "📥 Загрузки", "📊 qBit", "📺 Emby", "🔌 Статус", "⚙️ Настройки", "📋 История",
    "🔥 Топ", "📅 Календарь", "🎵 Музыка",
}


async def get_services() -> tuple[SearchService, AddService, ScoringService]:
    """Get service instances using singleton clients from registry."""
    prowlarr = await get_prowlarr()
    radarr = await get_radarr()
    sonarr = await get_sonarr()
    qbittorrent = await get_qbittorrent()  # Returns None if not configured
    lidarr = await get_lidarr()  # Returns None if not configured

    scoring = _SCORING_SERVICE
    search_service = SearchService(prowlarr, radarr, sonarr, scoring, lidarr=lidarr)
    add_service = AddService(prowlarr, radarr, sonarr, qbittorrent=qbittorrent, lidarr=lidarr)

    return search_service, add_service, scoring


def _strip_command(text: str, command: str) -> str:
    """Strip a leading /command (BUG-10/33: replace with maxsplit=1, prefix-only)."""
    text = text.strip()
    if text.startswith(command):
        rest = text[len(command):]
        # Strip @bot_username suffix on the command itself.
        if rest.startswith("@"):
            parts = rest.split(maxsplit=1)
            rest = parts[1] if len(parts) > 1 else ""
        return rest.strip()
    return text


@router.message(Command("search"))
async def cmd_search(message: Message, db_user: User, db: Database) -> None:
    """Handle /search <query> command - auto-detect content type."""
    if not message.text:
        await message.answer("Укажите запрос: <code>/search Дюна 2021</code>")
        return

    query = _strip_command(message.text, "/search")
    if not query:
        await message.answer("Укажите запрос: <code>/search Дюна 2021</code>")
        return

    await process_search(message, query, ContentType.UNKNOWN, db_user, db)


@router.message(Command("movie"))
async def cmd_movie(message: Message, db_user: User, db: Database) -> None:
    """Handle /movie <query> command."""
    if not message.text:
        await message.answer("Укажите название фильма: <code>/movie Дюна 2021</code>")
        return

    query = _strip_command(message.text, "/movie")
    if not query:
        await message.answer("Укажите название фильма: <code>/movie Дюна 2021</code>")
        return

    await process_search(message, query, ContentType.MOVIE, db_user, db)


@router.message(Command("series"))
async def cmd_series(message: Message, db_user: User, db: Database) -> None:
    """Handle /series <query> command."""
    if not message.text:
        await message.answer("Укажите название сериала: <code>/series Breaking Bad</code>")
        return

    query = _strip_command(message.text, "/series")
    if not query:
        await message.answer("Укажите название сериала: <code>/series Breaking Bad</code>")
        return

    await process_search(message, query, ContentType.SERIES, db_user, db)


@router.message(F.text == MENU_SEARCH)
async def handle_menu_search(message: Message) -> None:
    """Handle search menu button."""
    settings = get_settings()
    suffix = ", сериала или артиста" if settings.lidarr_enabled else " или сериала"
    await message.answer(f"🔍 Введите название фильма{suffix}:")


@router.message(F.text & ~F.text.startswith("/") & ~F.text.in_(MENU_BUTTONS))
async def handle_text_search(message: Message, db_user: User, db: Database) -> None:
    """Handle plain text as search query."""
    if not message.text:
        return

    await process_search(message, message.text.strip(), ContentType.UNKNOWN, db_user, db)


MAX_QUERY_LENGTH = 200


async def process_search(
    message: Message,
    query: str,
    content_type: ContentType,
    db_user: User,
    db: Database,
) -> None:
    """Process a search query."""
    if len(query) > MAX_QUERY_LENGTH:
        await message.answer(f"❌ Запрос слишком длинный (макс. {MAX_QUERY_LENGTH} символов)")
        return

    if len(query) < 2:
        await message.answer("❌ Запрос слишком короткий (мин. 2 символа)")
        return

    settings = get_settings()
    search_service, add_service, scoring = await get_services()

    # BUG-01: bind log BEFORE try so the except handler never sees a NameError
    # if message.answer() fails before log could be assigned inside try.
    user_id = db_user.tg_id
    log = logger.bind(user_id=user_id, query=query)
    t_start = time.monotonic()

    try:
        parsed = search_service.parse_query(query)
        clean_title = (parsed.get("title") or "").strip()
        log.info(
            "search_started",
            parsed=parsed,
            initial_content_type=content_type.value,
        )

        # Detect content type if unknown
        if content_type == ContentType.UNKNOWN:
            status_msg = await message.answer("🔍 Определяю тип контента...")

            # Strong signal: season-episode marker in query → SERIES.
            if parsed["season"] is not None:
                content_type = ContentType.SERIES
                detection = None
            else:
                t_detect = time.monotonic()
                detection = await search_service.detect_with_confidence(clean_title or query)
                log.info(
                    "stage_done",
                    stage="detect_content_type",
                    elapsed_ms=round((time.monotonic() - t_detect) * 1000, 1),
                    winner=detection.content_type.value,
                    confidence=round(detection.confidence, 3),
                    reason=detection.reason,
                )
                content_type = detection.content_type

            # Music auto-detected → hand off to Lidarr flow.
            if content_type == ContentType.MUSIC:
                await status_msg.delete()
                from bot.handlers.music import process_music_search

                await process_music_search(message, query, db_user, db)
                return

            # Unknown OR low/ambiguous confidence → ask the user (BUG-04, LOGIC-28).
            if content_type == ContentType.UNKNOWN:
                show_music = settings.lidarr_enabled
                question_suffix = (
                    "фильм, сериал или музыка?" if show_music else "фильм или сериал?"
                )
                hint = ""
                if detection and detection.candidates:
                    hint_lines = []
                    for kind, label in (("movie", "🎬"), ("series", "📺"), ("music", "🎵")):
                        items = detection.candidates.get(kind) or []
                        if items:
                            shown = ", ".join(html.escape(t) for t in items[:2])
                            hint_lines.append(f"{label} {shown}")
                    if hint_lines:
                        hint = "\n\n<i>Похоже на:</i>\n" + "\n".join(hint_lines)
                await status_msg.edit_text(
                    f"🤔 <b>{html.escape(query)}</b> — это {question_suffix}{hint}",
                    reply_markup=Keyboards.content_type_selection(show_music=show_music),
                    parse_mode="HTML",
                )
                session = SearchSession(
                    user_id=user_id,
                    query=query,
                    content_type=ContentType.UNKNOWN,
                )
                await db.save_session(user_id, session)
                log.info("search_branch", branch="question_user")
                return

            await status_msg.delete()

        status_msg = await message.answer("🔍 Ищу релизы...")

        # LOGIC-05: send the *raw* query (with year) to Prowlarr — many trackers
        # match "Title 2049" against "Title.2049." in release names. The clean
        # title is for Radarr/Sonarr lookup APIs, not for the indexer search.
        search_term = query if (clean_title and parsed.get("year")) else (clean_title or query)
        t_search = time.monotonic()
        results = await search_service.search_releases(search_term, content_type)
        log.info(
            "stage_done",
            stage="search_releases",
            elapsed_ms=round((time.monotonic() - t_search) * 1000, 1),
            result_count=len(results),
        )

        if not results:
            await status_msg.edit_text(
                Formatters.format_warning(f"Ничего не найдено для <b>{html.escape(query)}</b>"),
                parse_mode="HTML",
            )
            log.info("search_branch", branch="no_results")
            return

        await db.save_search(user_id, query, content_type, results)

        session = SearchSession(
            user_id=user_id,
            query=query,
            content_type=content_type,
            results=results,
            current_page=0,
        )
        await db.save_session(user_id, session)

        best_result = results[0] if results else None
        show_grab_best = (
            best_result
            and best_result.calculated_score >= settings.auto_grab_score_threshold
            and db_user.preferences.auto_grab_enabled
        )

        per_page = settings.results_per_page
        total_pages = (len(results) + per_page - 1) // per_page
        page_results = results[:per_page]

        text = Formatters.format_search_results_page(
            page_results,
            0,
            total_pages,
            query,
            content_type,
            per_page=per_page,
        )

        await status_msg.edit_text(
            text,
            reply_markup=Keyboards.search_results(
                page_results,
                0,
                total_pages,
                per_page,
                show_grab_best,
                best_result.calculated_score if best_result else 0,
            ),
            parse_mode="HTML",
        )

        action = ActionLog(
            user_id=user_id,
            action_type=ActionType.SEARCH,
            content_type=content_type,
            query=query,
        )
        await db.log_action(action)
        log.info(
            "search_branch",
            branch="results_shown",
            total_elapsed_ms=round((time.monotonic() - t_start) * 1000, 1),
            content_type=content_type.value,
        )

    except Exception as e:
        log.error("Search failed", error=str(e), exc_info=True)
        await message.answer(Formatters.format_error("Поиск временно недоступен"))


@router.callback_query(
    F.data.startswith(CallbackData.TYPE_MOVIE)
    | F.data.startswith(CallbackData.TYPE_SERIES)
    | F.data.startswith(CallbackData.TYPE_MUSIC)
)
async def handle_type_selection(callback: CallbackQuery, db_user: User, db: Database) -> None:
    """Handle content type selection."""
    if not callback.data or not callback.message:
        return

    user_id = callback.from_user.id
    session = await db.get_session(user_id)

    if not session:
        await callback.answer("Сессия истекла. Начните новый поиск.", show_alert=True)
        return

    # Music → hand off to Lidarr artist flow (different UX than torrent search)
    if callback.data == CallbackData.TYPE_MUSIC:
        await callback.answer()
        from bot.handlers.music import process_music_search

        await db.delete_session(user_id)  # music flow starts its own session
        await process_music_search(callback.message, session.query, db_user, db)
        return

    content_type = ContentType.MOVIE if callback.data == CallbackData.TYPE_MOVIE else ContentType.SERIES

    # Update session and continue search
    session.content_type = content_type
    await db.save_session(user_id, session)

    await callback.answer()

    # Use message.answer() to send results to same chat
    await process_search(
        callback.message,
        session.query,
        content_type,
        db_user,
        db,
    )


@router.callback_query(PageCB.filter(F.scope == "search"))
async def handle_pagination(
    callback: CallbackQuery, callback_data: PageCB, db_user: User, db: Database
) -> None:
    """Handle pagination buttons (#1: typed PageCB, no string parsing)."""
    if not callback.message:
        return

    settings = get_settings()
    user_id = callback.from_user.id
    session = await db.get_session(user_id)

    if not session or not session.results:
        await callback.answer("Сессия истекла. Начните новый поиск.", show_alert=True)
        return

    page = callback_data.page

    per_page = settings.results_per_page
    total_pages = (len(session.results) + per_page - 1) // per_page

    if page < 0 or page >= total_pages:
        await callback.answer("Неверная страница", show_alert=True)
        return

    # PERF-06: persist current_page only when it actually changed. Re-tapping the
    # current page (or a no-op nav) must not re-serialize the whole session to SQLite.
    if page != session.current_page:
        session.current_page = page
        await db.save_session(user_id, session)

    # Get page results
    start_idx = page * per_page
    page_results = session.results[start_idx:start_idx + per_page]

    # Check grab best
    best_result = session.results[0] if session.results else None
    show_grab_best = (
        best_result
        and best_result.calculated_score >= settings.auto_grab_score_threshold
        and db_user.preferences.auto_grab_enabled
    )

    text = Formatters.format_search_results_page(
        page_results,
        page,
        total_pages,
        session.query,
        session.content_type,
        per_page=per_page,
    )

    await callback.message.edit_text(
        text,
        reply_markup=Keyboards.search_results(
            page_results,
            page,
            total_pages,
            per_page,
            show_grab_best,
            best_result.calculated_score if best_result else 0,
        ),
        parse_mode="HTML",
    )

    await callback.answer()


@router.callback_query(F.data.startswith(CallbackData.RELEASE))
async def handle_release_selection(callback: CallbackQuery, db_user: User, db: Database) -> None:
    """Handle release selection."""
    if not callback.data or not callback.message:
        return

    search_service, add_service, _ = await get_services()

    user_id = callback.from_user.id
    session = await db.get_session(user_id)

    if not session or not session.results:
        await callback.answer("Сессия истекла. Начните новый поиск.", show_alert=True)
        return

    # Parse release index
    try:
        idx = int(callback.data.removeprefix(CallbackData.RELEASE))
    except ValueError:
        await callback.answer("Неверный выбор", show_alert=True)
        return

    if idx < 0 or idx >= len(session.results):
        await callback.answer("Неверный выбор", show_alert=True)
        return

    result = session.results[idx]
    session.selected_result = result
    await db.save_session(user_id, session)

    # Show release details
    text = Formatters.format_release_details(result)

    # Now need to look up the actual content in Radarr/Sonarr
    await callback.message.edit_text(
        text + "\n\n🔍 Загружаю информацию...",
        parse_mode="HTML",
    )

    # Check if force grab is available
    has_qbittorrent = add_service.qbittorrent is not None

    # Look up content — match by detected_year when available (BUG-08, LOGIC-07)
    # so a "Dune" query doesn't pick up the 1984 movie when the chosen release
    # is the 2021 one (or vice-versa).
    parsed = search_service.parse_query(session.query)
    lookup_term = (parsed.get("title") or "").strip() or session.query
    try:
        if session.content_type == ContentType.MOVIE:
            movies = await search_service.lookup_movie(lookup_term)
            movie = _pick_by_year(movies, result.detected_year, parsed.get("year"))
            if movie:
                session.selected_content = movie
                # RACE-04: UPDATE-only — if Cancel/grab deleted the session during
                # the (slow) lookup, don't resurrect it; abort instead.
                if not await db.update_session(user_id, session):
                    await callback.answer("Сессия истекла. Начните новый поиск.", show_alert=True)
                    return

                movie_text = Formatters.format_movie_info(movie)
                emby_note = await _emby_library_note(movie)
                await callback.message.edit_text(
                    f"{text}\n\n---\n{movie_text}{emby_note}",
                    reply_markup=Keyboards.release_details(result, session.content_type, show_force_grab=has_qbittorrent, content=movie),
                    parse_mode="HTML",
                )
            else:
                await callback.message.edit_text(
                    f"{text}\n\n⚠️ Не удалось найти информацию о фильме. Продолжить?",
                    reply_markup=Keyboards.release_details(result, session.content_type, show_force_grab=has_qbittorrent),
                    parse_mode="HTML",
                )
        else:
            series_list = await search_service.lookup_series(lookup_term)
            series = _pick_by_year(series_list, result.detected_year, parsed.get("year"))
            if series:
                session.selected_content = series
                # RACE-04: UPDATE-only — don't resurrect a session deleted mid-lookup.
                if not await db.update_session(user_id, session):
                    await callback.answer("Сессия истекла. Начните новый поиск.", show_alert=True)
                    return

                series_text = Formatters.format_series_info(series)
                emby_note = await _emby_library_note(series)
                await callback.message.edit_text(
                    f"{text}\n\n---\n{series_text}{emby_note}",
                    reply_markup=Keyboards.release_details(result, session.content_type, show_force_grab=has_qbittorrent, content=series),
                    parse_mode="HTML",
                )
            else:
                await callback.message.edit_text(
                    f"{text}\n\n⚠️ Не удалось найти информацию о сериале. Продолжить?",
                    reply_markup=Keyboards.release_details(result, session.content_type, show_force_grab=has_qbittorrent),
                    parse_mode="HTML",
                )
    except Exception as e:
        logger.warning("Failed to lookup content", error=str(e), exc_info=True)
        # SEC-20: escape exception text — error messages can contain '<' from URLs.
        await callback.message.edit_text(
            f"{text}\n\n⚠️ Ошибка загрузки информации: {html.escape(str(e))[:200]}",
            reply_markup=Keyboards.release_details(result, session.content_type, show_force_grab=has_qbittorrent),
            parse_mode="HTML",
        )

    await callback.answer()


async def _emby_library_note(content) -> str:
    """Feature #4: best-effort '<title> already in Emby' hint for the release card.

    Never raises and never blocks — on any error/timeout/absence returns "".
    """
    try:
        emby = await get_emby()
        if emby is None:
            return ""
        item_type = "Series" if isinstance(content, SeriesInfo) else "Movie"
        name = getattr(content, "title", None) or ""
        year = getattr(content, "year", None)
        exists = await asyncio.wait_for(emby.item_exists(name, year, item_type), timeout=5.0)
        return "\n\n✅ <i>Уже в библиотеке Emby</i>" if exists else ""
    except Exception:
        return ""


def _pick_by_year(items: list, release_year, query_year):
    """Pick the candidate whose `year` matches release.detected_year (preferred)
    or query year, falling back to the first candidate.

    BUG-08 / LOGIC-07: Radarr/Sonarr `lookup_*` returns candidates ordered by
    popularity, which is *not* what the user picked. The release the user
    clicked has its own detected year — prefer the candidate matching that.
    """
    if not items:
        return None
    target_year = release_year or query_year
    if target_year:
        for it in items:
            cand_year = getattr(it, "year", None)
            if cand_year and abs(int(cand_year) - int(target_year)) <= 1:
                return it
    return items[0]


@router.callback_query(F.data == CallbackData.GRAB_BEST)
async def handle_grab_best(callback: CallbackQuery, db_user: User, db: Database) -> None:
    """Handle 'Grab Best' button - grab the highest scored release."""
    if not callback.message:
        return

    user_id = callback.from_user.id
    if not await _claim_grab(user_id):
        await callback.answer("⏳ Уже обрабатываю предыдущий запрос…")
        return
    try:
        search_service, add_service, _ = await get_services()

        session = await db.get_session(user_id)

        if not session or not session.results:
            await callback.answer("Сессия истекла. Начните новый поиск.", show_alert=True)
            return

        result = session.results[0]  # Best result
        session.selected_result = result
        await db.save_session(user_id, session)

        await callback.answer("Скачиваю лучший релиз...")
        await callback.message.edit_text("⏳ Скачиваю лучший релиз...")

        # Lookup and grab
        await grab_release(callback.message, session, db_user, db, search_service, add_service)
    finally:
        _release_grab(user_id)


@router.callback_query(F.data == CallbackData.CONFIRM_GRAB)
async def handle_confirm_grab(callback: CallbackQuery, db_user: User, db: Database) -> None:
    """
    Handle grab confirmation — single dispatch point for movie/series/music.

    BUG-27: music handler previously attached its own `F.data == CONFIRM_GRAB`
    callback and was included before search_router, so it silently swallowed
    the event for movies/series (aiogram does not cascade handlers after a
    routed match). Now we dispatch by session.selected_content type here.
    """
    if not callback.message:
        return

    user_id = callback.from_user.id
    session = await db.get_session(user_id)

    if not session:
        await callback.answer("Сессия истекла. Начните новый поиск.", show_alert=True)
        return

    # Music flow — delegate to music handler's add-artist logic.
    from bot.models import ArtistInfo

    if isinstance(session.selected_content, ArtistInfo):
        from bot.handlers.music import handle_confirm_music_add

        await handle_confirm_music_add(callback, db_user, db)
        return

    # Movie / series flow — requires a selected release.
    if not session.selected_result:
        await callback.answer("Сессия истекла. Начните новый поиск.", show_alert=True)
        return

    # RACE-01: reject a concurrent second grab for this user.
    if not await _claim_grab(user_id):
        await callback.answer("⏳ Уже обрабатываю предыдущий запрос…")
        return
    try:
        search_service, add_service, _ = await get_services()
        await callback.answer("Обработка...")
        await callback.message.edit_text("⏳ Обрабатываю запрос...")

        await grab_release(callback.message, session, db_user, db, search_service, add_service)
    finally:
        _release_grab(user_id)


async def grab_release(
    message: Message,
    session: SearchSession,
    db_user: User,
    db: Database,
    search_service: SearchService,
    add_service: AddService,
) -> None:
    """Perform the actual grab operation."""
    await _execute_grab(message, session, db_user, db, search_service, add_service)


def _decide_monitor_type(result, force_download: bool, override: str | None = None) -> str:
    """Choose the Sonarr monitor scope for a grabbed series release.

    Feature #2: an explicit user preset (``override``) always wins.

    BUG-04: otherwise, a single targeted season (non-pack) must NOT be added with
    a type that monitors every season — Sonarr's "existing" returns True for all
    seasons of a brand-new series, so grabbing one season would silently monitor
    the whole show. Use "none" so only the explicitly grabbed release is pulled.
    """
    if override:
        return override
    if force_download:
        return "all"
    if result.is_season_pack:
        return "all"
    if result.detected_season is not None:
        return "none"
    return "all"


# Feature #2: season-monitoring presets exposed on the series release card.
_SEASON_PRESETS = {"all", "future", "latestSeason", "firstSeason", "none"}


def _resolve_folder(folders: list, preferred_id: int | None) -> str:
    """Resolve root folder path from user preference or first available."""
    if not folders:
        raise ValueError("Нет доступных папок для сохранения")
    if preferred_id:
        folder = next((f for f in folders if f.id == preferred_id), None)
        return folder.path if folder else folders[0].path
    return folders[0].path


async def _execute_grab(
    message: Message,
    session: SearchSession,
    db_user: User,
    db: Database,
    search_service: SearchService,
    add_service: AddService,
    *,
    force_download: bool = False,
) -> None:
    """Common grab logic for normal and force grab."""
    user_id = session.user_id
    result = session.selected_result
    prefs = db_user.preferences

    if not result:
        await message.edit_text(Formatters.format_error("Релиз не выбран"))
        return

    try:
        parsed = search_service.parse_query(session.query)
        lookup_term = (parsed.get("title") or "").strip() or session.query
        if session.content_type == ContentType.MOVIE:
            movie = session.selected_content
            if not isinstance(movie, MovieInfo):
                movies = await search_service.lookup_movie(lookup_term)
                if not movies:
                    await message.edit_text(Formatters.format_error("Не удалось найти фильм в Radarr"))
                    return
                movie = _pick_by_year(movies, result.detected_year, parsed.get("year"))

            profiles = await add_service.get_radarr_profiles()
            folders = await add_service.get_radarr_root_folders()

            if not profiles or not folders:
                await message.edit_text(Formatters.format_error("Нет профилей качества или папок в Radarr"))
                return

            profile_id = prefs.radarr_quality_profile_id or profiles[0].id
            folder_path = _resolve_folder(folders, prefs.radarr_root_folder_id)

            success, action, msg = await add_service.grab_movie_release(
                movie=movie,
                release=result,
                quality_profile_id=profile_id,
                root_folder_path=folder_path,
                force_download=force_download,
            )

            action.user_id = user_id
            await db.log_action(action)

            if success:
                await message.edit_text(
                    Formatters.format_success(f"<b>{html.escape(movie.title)}</b> ({movie.year})\n\n{msg}\n\nРелиз: <i>{html.escape(result.title)}</i>"),
                    parse_mode="HTML",
                )
            else:
                await message.edit_text(Formatters.format_error(msg))

        else:
            series = session.selected_content
            if not isinstance(series, SeriesInfo):
                series_list = await search_service.lookup_series(lookup_term)
                if not series_list:
                    await message.edit_text(Formatters.format_error("Не удалось найти сериал в Sonarr"))
                    return
                series = _pick_by_year(series_list, result.detected_year, parsed.get("year"))

            profiles = await add_service.get_sonarr_profiles()
            folders = await add_service.get_sonarr_root_folders()

            if not profiles or not folders:
                await message.edit_text(Formatters.format_error("Нет профилей качества или папок в Sonarr"))
                return

            profile_id = prefs.sonarr_quality_profile_id or profiles[0].id
            folder_path = _resolve_folder(folders, prefs.sonarr_root_folder_id)

            # Determine monitor type: user preset (#2) wins, else auto (BUG-04/BUG-32)
            monitor_type = _decide_monitor_type(result, force_download, override=session.monitor_type)

            success, action, msg = await add_service.grab_series_release(
                series=series,
                release=result,
                quality_profile_id=profile_id,
                root_folder_path=folder_path,
                monitor_type=monitor_type,
                force_download=force_download,
            )

            action.user_id = user_id
            await db.log_action(action)

            if success:
                year_str = f" ({series.year})" if series.year else ""
                await message.edit_text(
                    Formatters.format_success(f"<b>{html.escape(series.title)}</b>{year_str}\n\n{msg}\n\nРелиз: <i>{html.escape(result.title)}</i>"),
                    parse_mode="HTML",
                )
            else:
                await message.edit_text(Formatters.format_error(msg))

        await db.delete_session(user_id)

    except ValueError as ve:
        # LOGIC-16: surface "no folders" / similar config errors with their text.
        logger.warning("Grab config error", error=str(ve))
        await message.edit_text(Formatters.format_error(html.escape(str(ve))[:200]))
        await db.delete_session(user_id)
    except Exception as e:
        logger.error("Grab failed", error=str(e), exc_info=True)
        await message.edit_text(Formatters.format_error("Операция временно недоступна"))
        await db.delete_session(user_id)


@router.callback_query(F.data == CallbackData.BACK)
async def handle_back(callback: CallbackQuery, db_user: User, db: Database) -> None:
    """Handle back button."""
    if not callback.message:
        return

    settings = get_settings()
    user_id = callback.from_user.id
    session = await db.get_session(user_id)

    if not session or not session.results:
        await callback.answer("Сессия истекла", show_alert=True)
        return

    # Clear selection and go back to results.
    # PERF-06: only re-serialize the session when there is actually a selection
    # to clear — pressing Back with nothing selected is a no-op write.
    if session.selected_result is not None or session.selected_content is not None:
        session.selected_result = None
        session.selected_content = None
        await db.save_session(user_id, session)

    # Show results page
    per_page = settings.results_per_page
    total_pages = (len(session.results) + per_page - 1) // per_page
    page = min(session.current_page, max(0, total_pages - 1))

    start_idx = page * per_page
    page_results = session.results[start_idx:start_idx + per_page]

    best_result = session.results[0] if session.results else None
    show_grab_best = (
        best_result
        and best_result.calculated_score >= settings.auto_grab_score_threshold
        and db_user.preferences.auto_grab_enabled
    )

    text = Formatters.format_search_results_page(
        page_results,
        page,
        total_pages,
        session.query,
        session.content_type,
        per_page=per_page,
    )

    await callback.message.edit_text(
        text,
        reply_markup=Keyboards.search_results(
            page_results,
            page,
            total_pages,
            per_page,
            show_grab_best,
            best_result.calculated_score if best_result else 0,
        ),
        parse_mode="HTML",
    )

    await callback.answer()


@router.callback_query(F.data == CallbackData.CANCEL)
async def handle_cancel(callback: CallbackQuery, db: Database) -> None:
    """Handle cancel button."""
    if not callback.message:
        return

    user_id = callback.from_user.id
    await db.delete_session(user_id)

    await callback.message.edit_text("Операция отменена. Отправьте новый запрос для поиска.")
    await callback.answer()


@router.callback_query(F.data == CallbackData.FORCE_GRAB)
async def handle_force_grab(callback: CallbackQuery, db_user: User, db: Database) -> None:
    """Handle force grab button - downloads directly via qBittorrent."""
    if not callback.message:
        return

    user_id = db_user.tg_id
    # RACE-01: reject a concurrent second grab (e.g. Confirm then Force) for this user.
    if not await _claim_grab(user_id):
        await callback.answer("⏳ Уже обрабатываю предыдущий запрос…")
        return
    try:
        await callback.answer("Загружаю напрямую...")

        message = callback.message
        session = await db.get_session(user_id)
        if not session or not session.selected_result:
            await message.edit_text(Formatters.format_error("Сессия истекла. Повторите поиск."))
            return

        search_service, add_service, _ = await get_services()

        if not add_service.qbittorrent:
            await message.edit_text(Formatters.format_error("qBittorrent не настроен"))
            await db.delete_session(user_id)
            return

        await _execute_grab(message, session, db_user, db, search_service, add_service, force_download=True)
    finally:
        _release_grab(user_id)


@router.callback_query(F.data == CallbackData.SEASON_MENU)
async def handle_season_menu(callback: CallbackQuery, db_user: User, db: Database) -> None:
    """Feature #2: show the season-monitoring preset picker for a series."""
    if not callback.message:
        return
    user_id = callback.from_user.id
    session = await db.get_session(user_id)
    if not session or not session.selected_result:
        await callback.answer("Сессия истекла. Начните новый поиск.", show_alert=True)
        return
    await callback.answer()
    current = session.monitor_type or "auto"
    await callback.message.edit_text(
        f"📺 <b>Мониторинг сезонов</b>\n\nТекущий: <code>{current}</code>\n\nВыберите, какие сезоны отслеживать:",
        reply_markup=Keyboards.season_presets(),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith(CallbackData.SEASON_PRESET))
async def handle_season_preset(callback: CallbackQuery, db_user: User, db: Database) -> None:
    """Feature #2: store the chosen monitoring preset and return to the release card."""
    if not callback.data or not callback.message:
        return
    user_id = callback.from_user.id
    session = await db.get_session(user_id)
    if not session or not session.selected_result:
        await callback.answer("Сессия истекла. Начните новый поиск.", show_alert=True)
        return

    preset = callback.data.removeprefix(CallbackData.SEASON_PRESET)
    if preset not in _SEASON_PRESETS:
        await callback.answer("Неверный выбор", show_alert=True)
        return

    session.monitor_type = preset
    if not await db.update_session(user_id, session):
        await callback.answer("Сессия истекла. Начните новый поиск.", show_alert=True)
        return

    await callback.answer(f"Мониторинг: {preset}")
    _, add_service, _ = await get_services()
    has_qbittorrent = add_service.qbittorrent is not None
    result = session.selected_result
    text = Formatters.format_release_details(result)
    await callback.message.edit_text(
        f"{text}\n\n📺 Мониторинг: <b>{preset}</b>",
        reply_markup=Keyboards.release_details(
            result, session.content_type,
            show_force_grab=has_qbittorrent,
            content=session.selected_content,
        ),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "noop")
async def handle_noop(callback: CallbackQuery) -> None:
    """Handle no-op buttons (like page counter)."""
    await callback.answer()
