"""Result rendering, pagination, content-type selection and release picking."""

import asyncio
import html

import structlog
from aiogram import F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message

from bot.config import get_settings
from bot.db import Database
from bot.handlers.common import accessible_message
from bot.models import ContentType, SearchSession, SeriesInfo, User
from bot.ui.callbacks import BackCB, ContentTypeCB, PageCB, ReleaseCB, SeasonPageCB, SeasonScopeCB, TitleCB
from bot.ui.formatters import Formatters
from bot.ui.keyboards import CallbackData, Keyboards
from bot.ui.list_token import answer_stale, token_matches

from bot.handlers import search as _search
from .services import router

logger = structlog.get_logger()


@router.callback_query(ContentTypeCB.filter())
async def handle_type_selection(
    callback: CallbackQuery, callback_data: ContentTypeCB, db_user: User, db: Database,
) -> None:
    """Handle content type selection.

    BUG-02: кнопка привязана к своему запросу токеном, иначе «Фильм» под старым
    вопросом уводил как фильм запрос, набранный позже. BUG-12: двойной тап не
    запускает два поиска; токен проверяется ещё раз после захвата, потому что первый
    тап мог уже заменить сессию.
    """
    message = accessible_message(callback)
    if message is None:
        return

    user_id = callback.from_user.id
    if not await _question_is_current(db, user_id, callback_data.tok):
        await answer_stale(callback)
        return

    if not await _search._claim_grab(user_id):
        await callback.answer("⏳ Уже обрабатываю предыдущий запрос…")
        return
    try:
        session = await db.get_session(user_id)
        if session is None or not token_matches(session.list_token, callback_data.tok):
            await answer_stale(callback)
            return

        # Music → hand off to the artist flow (different UX than release search)
        if callback_data.kind == ContentType.MUSIC.value:
            await callback.answer()
            from bot.handlers.music import process_music_search

            await db.delete_session(user_id)  # music flow starts its own session
            # Audit UI-14: the question message becomes the artist list — its
            # type buttons must not stay alive under it.
            await process_music_search(message, session.query, db_user, db, status_msg=message)
            return

        try:
            content_type = ContentType(callback_data.kind)
        except ValueError:
            await answer_stale(callback)
            return

        # Update session and continue search
        session.content_type = content_type
        await db.save_session(user_id, session)

        await callback.answer()

        # LOGIC-23: remove the type-selection buttons from the question message —
        # otherwise it stays clickable.
        try:
            await message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise

        # Use message.answer() to send results to same chat
        await _search.process_search(
            message,
            session.query,
            content_type,
            db_user,
            db,
        )
    finally:
        _search._release_grab(user_id)


async def _question_is_current(db: Database, user_id: int, tok: str) -> bool:
    """Первая проверка до захвата слота: старой кнопке незачем его ждать."""
    session = await db.get_session(user_id)
    return session is not None and token_matches(session.list_token, tok)


@router.callback_query(F.data.startswith(CallbackData.TYPE_PREFIX))
async def handle_legacy_type_selection(callback: CallbackQuery) -> None:
    """BUG-02: кнопки ``type:*`` без токена не знают, о каком запросе спрашивали."""
    await answer_stale(callback)


@router.callback_query(PageCB.filter(F.scope == "search"))
async def handle_pagination(
    callback: CallbackQuery, callback_data: PageCB, db_user: User, db: Database
) -> None:
    """Handle pagination buttons (#1: typed PageCB, no string parsing)."""
    message = accessible_message(callback)
    if message is None:
        return

    settings = get_settings()
    user_id = callback.from_user.id

    # DB-02: lock the read-modify-write cycle — a double-tap on pagination
    # races two concurrent get_session/save_session pairs otherwise.
    async with db.session_lock(user_id):
        session = await db.get_session(user_id)

        # BUG-18: страница старого списка не перерисовывает новый.
        if not session or not session.results or not token_matches(session.list_token, callback_data.tok):
            await answer_stale(callback)
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
        message,
        session.results,
        page,
        total_pages,
        session.query,
        session.content_type,
        per_page,
        db_user,
        settings,
        list_token=session.list_token,
    )

    await callback.answer()


@router.callback_query(ReleaseCB.filter())
async def handle_release_selection(
    callback: CallbackQuery, callback_data: ReleaseCB, db_user: User, db: Database
) -> None:
    """Handle release selection."""
    message = accessible_message(callback)
    if message is None:
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

        # Audit GRAB-03: a button from another list (older search, or a
        # session that no longer has one) must not resolve `idx` here.
        if (
            not session
            or not session.results
            or session.free_search
            or not token_matches(session.list_token, callback_data.tok)
            or callback_data.idx >= len(session.results)
        ):
            await answer_stale(callback)
            return

        idx = callback_data.idx

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
    # Rollback 2026-08-10: the title is already resolved — process_search had
    # to create/find it in Radarr/Sonarr before releases could be searched at
    # all, and stored it on the session. So there is no second *arr lookup
    # here (which used to cost 30-95s when a service was degraded), only the
    # optional Emby "already in library" probe.
    text = Formatters.format_release_details(result)
    has_qbittorrent = add_service.qbittorrent is not None
    content = session.selected_content

    try:
        if content is None:
            await message.edit_text(
                f"{text}\n\n⚠️ Информация о тайтле недоступна. Продолжить?",
                reply_markup=Keyboards.release_details(
                    result, session.content_type, show_force_grab=has_qbittorrent,
                    list_token=session.list_token, idx=idx,
                    season_menu=session.title_created,
                ),
                parse_mode="HTML",
            )
            return

        # Shared with the season picker's way back (audit UI-15). ArtistInfo
        # never reaches this card — music has its own.
        emby_note = await _emby_library_note(content)
        await message.edit_text(
            f"{Formatters.format_release_card(result, content)}{emby_note}",
            reply_markup=Keyboards.release_details(
                result, session.content_type, show_force_grab=has_qbittorrent, content=content,
                list_token=session.list_token, idx=idx,
                season_menu=session.title_created,
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning("Failed to render release card", error=str(e), exc_info=True)
        # SEC-20: escape exception text — error messages can contain '<' from URLs.
        await message.edit_text(
            f"{text}\n\n⚠️ Ошибка загрузки информации: {html.escape(str(e)[:200])}",
            reply_markup=Keyboards.release_details(
                result, session.content_type, show_force_grab=has_qbittorrent,
                list_token=session.list_token, idx=idx,
                season_menu=session.title_created,
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


@router.callback_query(BackCB.filter())
async def handle_back(callback: CallbackQuery, callback_data: BackCB, db_user: User, db: Database) -> None:
    """Handle back button."""
    message = accessible_message(callback)
    if message is None:
        return

    settings = get_settings()
    user_id = callback.from_user.id

    # DB-02: lock the read-modify-write cycle around clearing the selection.
    async with db.session_lock(user_id):
        session = await db.get_session(user_id)

        # BUG-18: «Назад» со старой карточки не сбрасывает выбор в новом списке.
        if not session or not session.results or not token_matches(session.list_token, callback_data.tok):
            await answer_stale(callback)
            return

        # Clear the release selection and go back to results.
        # Review fix round 1 (B1): keep `selected_content` — it is the TITLE the
        # whole list belongs to (Radarr/Sonarr id), not a per-card selection.
        # Clearing it made every grab button on the redrawn list read as
        # "Список устарел" and the next card "Информация о тайтле недоступна".
        # PERF-06: only re-serialize the session when there is actually a selection
        # to clear — pressing Back with nothing selected is a no-op write.
        if session.selected_result is not None:
            session.selected_result = None
            await db.save_session(user_id, session)

    # Show results page
    per_page = settings.results_per_page
    total_pages = (len(session.results) + per_page - 1) // per_page
    page = min(session.current_page, max(0, total_pages - 1))

    # LOGIC-04/BUG-03: shared renderer — also swallows "message is not
    # modified" from a repeat Back tap.
    await _search._render_results_page(
        message,
        session.results,
        page,
        total_pages,
        session.query,
        session.content_type,
        per_page,
        db_user,
        settings,
        list_token=session.list_token,
    )

    await callback.answer()


@router.callback_query(F.data == CallbackData.BACK)
async def handle_legacy_back(callback: CallbackQuery) -> None:
    """BUG-18: кнопка ``back`` без токена не знает, к какому списку ведёт."""
    await answer_stale(callback)


@router.callback_query(F.data == CallbackData.CANCEL)
async def handle_cancel(callback: CallbackQuery, db: Database) -> None:
    """Handle cancel button."""
    message = accessible_message(callback)
    if message is None:
        return

    user_id = callback.from_user.id
    await db.delete_session(user_id)

    await message.edit_text("Операция отменена. Отправьте новый запрос для поиска.")
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
    await answer_stale(callback)


@router.callback_query(F.data.startswith(CallbackData.RELEASE))
async def handle_legacy_release(callback: CallbackQuery) -> None:
    """r5: legacy ``rel:N`` string buttons from messages sent before the
    ReleaseCB migration — surface an explicit alert instead of falling
    through unhandled (see ``handle_legacy_page`` for the same pattern).
    Audit 2026-09-23: also catches ``rel:N`` without a list token.
    """
    await answer_stale(callback)


@router.callback_query(TitleCB.filter())
async def handle_title_selection(
    callback: CallbackQuery, callback_data: TitleCB, db_user: User, db: Database
) -> None:
    """Continue the search with the title the user picked (2026-07-29).

    Reached only when `resolve_title_candidate` decided the metadata list was
    ambiguous — see the prod incident where "Холодное сердце" silently resolved
    to an unrelated German film.

    BUG-01: токен привязывает кнопку к её выбору. BUG-12: двойной тап не запускает
    два добавления; после захвата сессия читается заново, первый тап мог её заменить.
    """
    message = accessible_message(callback)
    if message is None:
        return

    user_id = callback.from_user.id
    if await _load_title_choice(db, user_id, callback_data) is None:
        await answer_stale(callback)
        return

    if not await _search._claim_grab(user_id):
        await callback.answer("⏳ Уже обрабатываю предыдущий запрос…")
        return
    try:
        session = await _load_title_choice(db, user_id, callback_data)
        if session is None or session.lookup_candidates is None:
            await answer_stale(callback)
            return

        chosen = session.lookup_candidates[callback_data.idx]
        await callback.answer()

        # Drop the buttons so a later tap can't start another search (LOGIC-23).
        try:
            await message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise

        await _search.process_search(
            message,
            session.query,
            session.content_type,
            db_user,
            db,
            chosen_title=chosen,
        )
    finally:
        _search._release_grab(user_id)


async def _load_title_choice(db: Database, user_id: int, callback_data: TitleCB) -> SearchSession | None:
    """Сессия, если кнопка всё ещё называет кандидата текущего выбора."""
    session = await db.get_session(user_id)
    if (
        session is None
        or not session.lookup_candidates
        or not token_matches(session.list_token, callback_data.tok)
        or callback_data.idx >= len(session.lookup_candidates)
    ):
        return None
    return session


@router.callback_query(SeasonPageCB.filter())
async def handle_season_page(
    callback: CallbackQuery, callback_data: SeasonPageCB, db: Database
) -> None:
    """Flip the season picker to another page (audit UI-17)."""
    from bot.handlers.search.commands import _known_seasons

    message = accessible_message(callback)
    if message is None:
        return

    session = await db.get_session(callback.from_user.id)
    title = session.selected_content if session else None
    title_id = title.sonarr_id if isinstance(title, SeriesInfo) else None
    if session is None or title_id is None or str(title_id) != callback_data.title_id:
        await answer_stale(callback)
        return

    seasons = _known_seasons(title, session.content_type)
    await callback.answer()
    try:
        await message.edit_reply_markup(
            reply_markup=Keyboards.season_scope(seasons, callback_data.title_id, page=callback_data.page),
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


@router.callback_query(SeasonScopeCB.filter())
async def handle_season_scope(
    callback: CallbackQuery, callback_data: SeasonScopeCB, db_user: User, db: Database
) -> None:
    """Continue the search scoped to the chosen season (0 = whole series)."""
    message = accessible_message(callback)
    if message is None:
        return

    user_id = callback.from_user.id
    session = await db.get_session(user_id)
    # Audit GRAB-03/UI-02: the season picker belongs to one title; a picker
    # left in an old message must not search seasons of whatever title the
    # session holds now.
    title = session.selected_content if session else None
    title_id = title.sonarr_id if isinstance(title, SeriesInfo) else None
    if session is None or title_id is None or str(title_id) != callback_data.title_id:
        await answer_stale(callback)
        return

    # Round 2 / D3: "📺 Весь сериал" sends Sonarr's SeriesSearch — a double
    # tap must not send it twice. Same per-user slot as every grab.
    whole_series = callback_data.season == 0
    if whole_series and not await _search._claim_grab(user_id):
        await callback.answer("⏳ Уже обрабатываю предыдущий запрос…")
        return
    try:
        if whole_series:
            # Re-read after the claim: a finished first tap deleted the session.
            session = await db.get_session(user_id)
            title = session.selected_content if session else None
            if session is None or not isinstance(title, SeriesInfo) or str(title.sonarr_id) != callback_data.title_id:
                await answer_stale(callback)
                return
        await _continue_season_scope(callback, message, session, callback_data, db_user, db)
    finally:
        if whole_series:
            _search._release_grab(user_id)


async def _continue_season_scope(
    callback: CallbackQuery,
    message: Message,
    session: SearchSession,
    callback_data: SeasonScopeCB,
    db_user: User,
    db: Database,
) -> None:
    """The rest of ``handle_season_scope`` (split out so the whole-series
    claim can wrap it in one try/finally)."""
    await callback.answer()
    try:
        await message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise

    await _search.process_search(
        message,
        session.query,
        session.content_type,
        db_user,
        db,
        chosen_title=session.selected_content,
        # Ноль («📺 Весь сериал») передаётся как есть: `or None` терял ответ, а
        # None в `process_search` значит «пользователя ещё не спрашивали» — и
        # тот снова показывал этот же выбор сезона (живой дефект 2026-08-12).
        season_override=callback_data.season,
        title_created=session.title_created,
    )
