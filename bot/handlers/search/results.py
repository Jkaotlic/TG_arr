"""Result rendering, pagination, content-type selection and release picking."""

import asyncio
import html

import structlog
from aiogram import F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

from bot.config import get_settings
from bot.db import Database
from bot.models import ContentType, MovieInfo, SeriesInfo, User
from bot.ui.callbacks import PageCB, ReleaseCB, SeasonScopeCB, TitleCB
from bot.ui.formatters import Formatters
from bot.ui.keyboards import CallbackData, Keyboards

from bot.handlers import search as _search
from .services import router

logger = structlog.get_logger()


@router.callback_query(
    F.data.startswith(CallbackData.TYPE_MOVIE)
    | F.data.startswith(CallbackData.TYPE_SERIES)
    | F.data.startswith(CallbackData.TYPE_ANIME)
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

    # Music → hand off to the artist flow (different UX than release search)
    if callback.data == CallbackData.TYPE_MUSIC:
        await callback.answer()
        from bot.handlers.music import process_music_search

        await db.delete_session(user_id)  # music flow starts its own session
        await process_music_search(callback.message, session.query, db_user, db)
        return

    content_type = {
        CallbackData.TYPE_MOVIE: ContentType.MOVIE,
        CallbackData.TYPE_SERIES: ContentType.SERIES,
        CallbackData.TYPE_ANIME: ContentType.ANIME,
    }.get(callback.data, ContentType.SERIES)

    # Update session and continue search
    session.content_type = content_type
    await db.save_session(user_id, session)

    await callback.answer()

    # LOGIC-23: remove the type-selection buttons from the question message —
    # otherwise it stays clickable and a repeat tap re-launches a second
    # parallel search while the first one is still in flight.
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise

    # Use message.answer() to send results to same chat
    await _search.process_search(
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

    # DB-02: lock the read-modify-write cycle — a double-tap on pagination
    # races two concurrent get_session/save_session pairs otherwise.
    async with db.session_lock(user_id):
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

    # LOGIC-04/BUG-03: shared renderer — also swallows "message is not
    # modified" from a fast double-tap on the same page.
    await _search._render_results_page(
        callback.message,
        session.results,
        page,
        total_pages,
        session.query,
        session.content_type,
        per_page,
        db_user,
        settings,
    )

    await callback.answer()


@router.callback_query(ReleaseCB.filter())
async def handle_release_selection(
    callback: CallbackQuery, callback_data: ReleaseCB, db_user: User, db: Database
) -> None:
    """Handle release selection."""
    if not callback.message:
        return

    search_service, add_service = await _search.get_services()

    user_id = callback.from_user.id

    # DB-02: lock the read-modify-write cycle so a double-tap (two concurrent
    # release selections) can't have the second save_session clobber the
    # first's `selected_result`. Scoped to just the read+mutate+save below —
    # not the (potentially 30-95s) *arr lookup that follows, which uses its
    # own narrow lock around each update_session call.
    async with db.session_lock(user_id):
        session = await db.get_session(user_id)

        if not session or not session.results:
            await callback.answer("Сессия истекла. Начните новый поиск.", show_alert=True)
            return

        idx = callback_data.idx

        if idx < 0 or idx >= len(session.results):
            await callback.answer("Неверный выбор", show_alert=True)
            return

        # BUG-07: ack right after validation — the lookup below can take up to
        # HTTP_TIMEOUT × retries (30-95s when *arr is degraded), and Telegram
        # rejects answerCallbackQuery once the query is "too old" (~15-30s). The
        # user's tap-feedback (spinner) must not depend on a slow *arr response.
        await callback.answer()

        result = session.results[idx]
        session.selected_result = result
        await db.save_session(user_id, session)

    # Show release details.
    #
    # Migration 2026-07-28: the title is already resolved — process_search had
    # to create/find it in Scryer before releases could be searched at all, and
    # stored it on the session. So there is no second *arr lookup here (which
    # used to cost 30-95s when a service was degraded), only the optional Emby
    # "already in library" probe.
    text = Formatters.format_release_details(result)
    has_qbittorrent = add_service.qbittorrent is not None
    content = session.selected_content

    try:
        if content is None:
            await callback.message.edit_text(
                f"{text}\n\n⚠️ Информация о тайтле недоступна. Продолжить?",
                reply_markup=Keyboards.release_details(
                    result, session.content_type, show_force_grab=has_qbittorrent
                ),
                parse_mode="HTML",
            )
            return

        info_text = (
            Formatters.format_movie_info(content)
            if isinstance(content, MovieInfo)
            else Formatters.format_series_info(content)
        )
        emby_note = await _emby_library_note(content)
        await callback.message.edit_text(
            f"{text}\n\n---\n{info_text}{emby_note}",
            reply_markup=Keyboards.release_details(
                result, session.content_type, show_force_grab=has_qbittorrent, content=content
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning("Failed to render release card", error=str(e), exc_info=True)
        # SEC-20: escape exception text — error messages can contain '<' from URLs.
        await callback.message.edit_text(
            f"{text}\n\n⚠️ Ошибка загрузки информации: {html.escape(str(e))[:200]}",
            reply_markup=Keyboards.release_details(
                result, session.content_type, show_force_grab=has_qbittorrent
            ),
            parse_mode="HTML",
        )


async def _emby_library_note(content) -> str:
    """Feature #4: best-effort '<title> already in Emby' hint for the release card.

    Never raises and never blocks — on any error/timeout/absence returns "".
    """
    try:
        emby = await _search.get_emby()
        if emby is None:
            return ""
        item_type = "Series" if isinstance(content, SeriesInfo) else "Movie"
        name = getattr(content, "title", None) or ""
        year = getattr(content, "year", None)
        exists = await asyncio.wait_for(emby.item_exists(name, year, item_type), timeout=5.0)
        return "\n\n✅ <i>Уже в библиотеке Emby</i>" if exists else ""
    except Exception as e:
        # OBS-12a: this branch is intentionally best-effort/non-blocking, but a
        # silent swallow with zero trace made a systematically-timing-out Emby
        # invisible in the logs. DEBUG only — never fails the release card.
        logger.debug("emby_note_skipped", error=str(e))
        return ""


def _pick_by_year(items: list, release_year, query_year):
    """Pick the candidate whose `year` matches release.detected_year (preferred)
    or the query year, falling back to the first candidate.

    BUG-08 / LOGIC-07: metadata search returns candidates ordered by
    popularity, which is *not* necessarily what the user picked. The release
    the user clicked has its own detected year — prefer the candidate matching
    that.
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


@router.callback_query(F.data == CallbackData.BACK)
async def handle_back(callback: CallbackQuery, db_user: User, db: Database) -> None:
    """Handle back button."""
    if not callback.message:
        return

    settings = get_settings()
    user_id = callback.from_user.id

    # DB-02: lock the read-modify-write cycle around clearing the selection.
    async with db.session_lock(user_id):
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

    # LOGIC-04/BUG-03: shared renderer — also swallows "message is not
    # modified" from a repeat Back tap.
    await _search._render_results_page(
        callback.message,
        session.results,
        page,
        total_pages,
        session.query,
        session.content_type,
        per_page,
        db_user,
        settings,
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


@router.callback_query(F.data == "noop")
async def handle_noop(callback: CallbackQuery) -> None:
    """Handle no-op buttons (like page counter)."""
    await callback.answer()


@router.callback_query(F.data.startswith("page:"))
async def handle_legacy_page(callback: CallbackQuery) -> None:
    """BUG-02: orphaned legacy `page:N` buttons from messages sent before the
    typed-PageCB migration (a430ad3) have no matching handler anymore, so a
    tap on them used to spin forever (no answer() ever fired). Surface an
    explicit alert instead of a silent hang.
    """
    await callback.answer("Кнопка устарела — повторите поиск", show_alert=True)


@router.callback_query(F.data.startswith(CallbackData.RELEASE))
async def handle_legacy_release(callback: CallbackQuery) -> None:
    """r5: legacy ``rel:N`` string buttons from messages sent before the
    ReleaseCB migration — surface an explicit alert instead of falling
    through unhandled (see ``handle_legacy_page`` for the same pattern).
    """
    await callback.answer("Кнопка устарела — повторите поиск", show_alert=True)


@router.callback_query(TitleCB.filter())
async def handle_title_selection(
    callback: CallbackQuery, callback_data: TitleCB, db_user: User, db: Database
) -> None:
    """Continue the search with the title the user picked (2026-07-29).

    Reached only when `needs_title_confirmation` decided the metadata list was
    ambiguous — see the prod incident where "Холодное сердце" silently resolved
    to an unrelated German film.
    """
    if not callback.message:
        return

    user_id = callback.from_user.id
    session = await db.get_session(user_id)
    if not session or not session.lookup_candidates:
        await callback.answer("Сессия истекла. Начните новый поиск.", show_alert=True)
        return

    idx = callback_data.idx
    if idx < 0 or idx >= len(session.lookup_candidates):
        await callback.answer("Неверный выбор", show_alert=True)
        return

    chosen = session.lookup_candidates[idx]
    await callback.answer()

    # Drop the buttons so a second tap can't start a parallel search (LOGIC-23).
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise

    await _search.process_search(
        callback.message,
        session.query,
        session.content_type,
        db_user,
        db,
        chosen_title=chosen,
    )


@router.callback_query(SeasonScopeCB.filter())
async def handle_season_scope(
    callback: CallbackQuery, callback_data: SeasonScopeCB, db_user: User, db: Database
) -> None:
    """Continue the search scoped to the chosen season (0 = whole series)."""
    if not callback.message:
        return

    user_id = callback.from_user.id
    session = await db.get_session(user_id)
    if not session:
        await callback.answer("Сессия истекла. Начните новый поиск.", show_alert=True)
        return

    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise

    await _search.process_search(
        callback.message,
        session.query,
        session.content_type,
        db_user,
        db,
        chosen_title=session.selected_content,
        season_override=callback_data.season or None,
    )
