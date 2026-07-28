"""Entry points for the search flow: /search, /movie, /series, plain text, and
the top-level query-processing pipeline that kicks off content-type detection
and shows the first results page."""

import html
import time
from typing import Optional

import structlog
from aiogram import F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import Message

from bot.config import get_settings
from bot.db import Database
from bot.handlers.common import strip_command
from bot.models import ActionLog, ActionType, ContentType, SearchSession, User
from bot.ui.formatters import Formatters
from bot.ui.keyboards import Keyboards
from bot.ui.menu import MENU_BUTTONS, MENU_SEARCH

from bot.handlers import search as _search
from .services import MAX_QUERY_LENGTH, router

logger = structlog.get_logger()


@router.message(Command("search"))
async def cmd_search(message: Message, db_user: User, db: Database) -> None:
    """Handle /search <query> command - auto-detect content type."""
    if not message.text:
        await message.answer("Укажите запрос: <code>/search Дюна 2021</code>")
        return

    query = strip_command(message.text, "/search")
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

    query = strip_command(message.text, "/movie")
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

    query = strip_command(message.text, "/series")
    if not query:
        await message.answer("Укажите название сериала: <code>/series Breaking Bad</code>")
        return

    await process_search(message, query, ContentType.SERIES, db_user, db)


@router.message(Command("anime"))
async def cmd_anime(message: Message, db_user: User, db: Database) -> None:
    """Handle /anime <query> — anime is its own Scryer facet (own library and
    `1080p` quality profile), not a flavour of series."""
    if not message.text:
        await message.answer("Укажите название аниме: <code>/anime Frieren</code>")
        return

    query = strip_command(message.text, "/anime")
    if not query:
        await message.answer("Укажите название аниме: <code>/anime Frieren</code>")
        return

    await process_search(message, query, ContentType.ANIME, db_user, db)


@router.message(F.text == MENU_SEARCH)
async def handle_menu_search(message: Message) -> None:
    """Handle search menu button."""
    settings = get_settings()
    suffix = ", сериала, аниме или артиста" if settings.music_enabled else ", сериала или аниме"
    await message.answer(f"🔍 Введите название фильма{suffix}:")


@router.message(F.text & ~F.text.startswith("/") & ~F.text.in_(MENU_BUTTONS))
async def handle_text_search(message: Message, db_user: User, db: Database) -> None:
    """Handle plain text as search query."""
    if not message.text:
        return

    await process_search(message, message.text.strip(), ContentType.UNKNOWN, db_user, db)


def _pick_metadata_candidate(candidates: list, query_year: Optional[int]):
    """Choose the metadata candidate the user most likely meant.

    Prefers a year match (±1) when the query carried a year — "Dune 2021" must
    not resolve to the 1984 film just because it ranks higher by popularity.
    """
    if not candidates:
        return None
    if query_year:
        for candidate in candidates:
            if candidate.year and abs(int(candidate.year) - int(query_year)) <= 1:
                return candidate
    return candidates[0]


async def _resolve_title(
    search_service,
    add_service,
    detection,
    content_type: ContentType,
    lookup_term: str,
    query_year: Optional[int],
):
    """Resolve a query to a Scryer title id, adding the title if it's new.

    Scryer can only list releases for a title in its catalog, so this is the
    step that replaced "just send the query to Prowlarr". The title is added
    **unmonitored** — browsing releases must not enrol anything into automatic
    acquisition; `AddService.grab_release` turns monitoring on once the user
    actually downloads something.
    """
    candidates = (
        detection.lookup_results
        if detection and detection.content_type == content_type and detection.lookup_results
        else await search_service.search_metadata(lookup_term, content_type)
    )
    chosen = _pick_metadata_candidate(candidates, query_year)
    if chosen is None:
        return None

    title, created = await add_service.ensure_title(chosen, content_type)
    if not getattr(title, "scryer_id", None):
        logger.warning("scryer_title_without_id", title=getattr(title, "title", None))
        return None
    logger.info(
        "title_resolved",
        title=title.title,
        title_id=title.scryer_id,
        content_type=content_type.value,
        created=created,
    )
    return title


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
    search_service, add_service = await _search.get_services()

    # BUG-01: bind log BEFORE try so the except handler never sees a NameError
    # if message.answer() fails before log could be assigned inside try.
    user_id = db_user.tg_id
    log = logger.bind(user_id=user_id, query=query)
    t_start = time.monotonic()
    # LOGIC-23: tracked so the except-handler can edit the in-flight status
    # message ("Ищу релизы...") instead of leaving it hanging and sending a
    # brand-new error message underneath it.
    status_msg: Optional[Message] = None
    # LOGIC-06: only set when content_type was UNKNOWN and detection ran;
    # carries lookup_results forward into the session below so a later grab
    # doesn't repeat the same metadata lookup.
    detection = None

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

            # Note: a season marker no longer short-circuits to SERIES here.
            # It narrows detection to series-vs-anime *inside*
            # detect_with_confidence, because those are separate Scryer
            # libraries with separate quality profiles — see its docstring.
            t_detect = time.monotonic()
            detection = await search_service.detect_with_confidence(query)
            log.info(
                "stage_done",
                stage="detect_content_type",
                elapsed_ms=round((time.monotonic() - t_detect) * 1000, 1),
                winner=detection.content_type.value,
                confidence=round(detection.confidence, 3),
                reason=detection.reason,
            )
            content_type = detection.content_type

            # Music auto-detected → hand off to the music flow.
            if content_type == ContentType.MUSIC:
                await status_msg.delete()
                from bot.handlers.music import process_music_search

                await process_music_search(message, query, db_user, db)
                return

            # Unknown OR low/ambiguous confidence → ask the user (BUG-04, LOGIC-28).
            if content_type == ContentType.UNKNOWN:
                show_music = settings.music_enabled
                question_suffix = (
                    "фильм, сериал, аниме или музыка?" if show_music else "фильм, сериал или аниме?"
                )
                hint = ""
                if detection and detection.candidates:
                    hint_lines = []
                    for kind, label in (
                        ("movie", "🎬"), ("series", "📺"), ("anime", "🎌"), ("music", "🎵"),
                    ):
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

        # Migration 2026-07-28: Scryer searches releases per *title*, not by free
        # text, so resolve the title first. Detection already fetched metadata
        # candidates for the winning facet — reuse them instead of searching again.
        title = await _resolve_title(
            search_service, add_service, detection, content_type,
            clean_title or query, parsed.get("year"),
        )
        if title is None:
            await status_msg.edit_text(
                Formatters.format_warning(f"Ничего не найдено для <b>{html.escape(query)}</b>"),
                parse_mode="HTML",
            )
            log.info("search_branch", branch="no_metadata")
            return

        t_search = time.monotonic()
        results = await search_service.search_releases(
            title.scryer_id,
            content_type,
            season=parsed.get("season"),
            episode=parsed.get("episode"),
            preferred_resolution=db_user.preferences.preferred_resolution,
            timeout=settings.scryer_search_timeout,
        )
        log.info(
            "stage_done",
            stage="search_releases",
            elapsed_ms=round((time.monotonic() - t_search) * 1000, 1),
            result_count=len(results),
            title_id=title.scryer_id,
        )

        if not results:
            await status_msg.edit_text(
                Formatters.format_warning(
                    f"Релизы для <b>{html.escape(title.title)}</b> не найдены.\n\n"
                    "Тайтл добавлен в каталог Scryer — можно включить мониторинг, "
                    "и он продолжит искать сам."
                ),
                parse_mode="HTML",
            )
            log.info("search_branch", branch="no_results")
            return

        # DB-03: search_results was a write-only table (JSON blob duplicated
        # into `sessions` right below) — never read by any handler. Dropped
        # from the hot path; `actions` (ActionType.SEARCH, logged below)
        # already covers search history.
        session = SearchSession(
            user_id=user_id,
            query=query,
            content_type=content_type,
            results=results,
            current_page=0,
            selected_content=title,
        )
        await db.save_session(user_id, session)

        per_page = settings.results_per_page
        total_pages = (len(results) + per_page - 1) // per_page

        # LOGIC-04: shared renderer (also swallows "message is not modified").
        await _search._render_results_page(
            status_msg, results, 0, total_pages, query, content_type, per_page, db_user, settings
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
        # LOGIC-23: edit the in-flight status message ("Ищу релизы...") rather
        # than leaving it hanging forever with a separate error message below it.
        error_text = Formatters.format_error("Поиск временно недоступен")
        if status_msg is not None:
            try:
                await status_msg.edit_text(error_text)
            except TelegramBadRequest:
                await message.answer(error_text)
        else:
            await message.answer(error_text)
