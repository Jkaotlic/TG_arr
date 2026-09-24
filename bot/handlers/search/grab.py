"""Grab confirmation, execution and season-monitoring preset handlers."""

import html

import structlog
from aiogram import F
from aiogram.types import CallbackQuery, Message

from bot.config import get_settings
from bot.db import Database
from bot.handlers.common import accessible_message
from bot.models import ContentType, MovieInfo, SearchResult, SearchSession, SeriesInfo, User
from bot.services.add_service import (
    GRAB_OUTCOME_REJECTED,
    GRAB_OUTCOME_UNREACHABLE,
    AddService,
    grab_outcome,
)
from bot.services.scoring import grab_best_candidate
from bot.services.search_service import SearchService
from bot.ui.callbacks import AutoSearchCB, ForceGrabCB, GrabBestCB, GrabCB, SeasonPresetCB
from bot.ui.formatters import Formatters, season_preset_name
from bot.ui.keyboards import CallbackData, Keyboards
from bot.ui.keyboards._constants import MSG_OLD_BUTTON
from bot.ui.list_token import answer_stale, token_matches

from bot.handlers import search as _search
from .services import router

logger = structlog.get_logger()


# Feature #2: season-monitoring presets exposed on the series release card.
# These strings match Sonarr's own `monitor` addOptions values verbatim (see
# SonarrClient.add_series) — rollback 2026-08-10 removed the previous
# backend's translation table this used to feed, since there is nothing left
# to translate to.
_SEASON_PRESETS = {"all", "future", "latestSeason", "firstSeason", "none"}

#: GRAB-05: a series that was in the library before this search keeps its
#: monitoring; the grab does not rewrite it.
_EXISTING_SERIES_NOTE = (
    "Сериал уже был в библиотеке — мониторинг не меняю. Управлять: /title"
)


def _catalogue_list(session: SearchSession | None, tok: str) -> bool:
    """Whether ``tok`` names the catalogue result list this session holds now.

    Audit 2026-09-23 GRAB-03: every grab button carries the token of the list
    it was drawn for; anything else (older search, free search, music, a
    session from an older bot version without a token) is "устарел".
    """
    return bool(
        session
        and session.results
        and not session.free_search
        and isinstance(session.selected_content, (MovieInfo, SeriesInfo))
        and token_matches(session.list_token, tok)
    )


def _selected_index(session: SearchSession) -> int:
    """Index of ``selected_result`` in ``results`` (by guid) for re-rendering
    the release card's buttons; 0 when it cannot be found."""
    selected = session.selected_result
    if selected is not None:
        for i, r in enumerate(session.results):
            if r.guid == selected.guid:
                return i
    return 0


async def _load_pick(db: Database, user_id: int, tok: str, idx: int) -> SearchSession | None:
    """The session, if ``tok``/``idx`` still name a release of its current
    catalogue list; None otherwise ("устарел").

    Review fix round 1: called again AFTER ``_claim_grab`` — between the
    first read and the claim a concurrent action may have replaced or
    deleted the session, and the grab must act on what is there now.
    """
    session = await db.get_session(user_id)
    if session is None or not _catalogue_list(session, tok) or idx >= len(session.results):
        return None
    return session


def _arr_name(content_type: ContentType) -> str:
    if content_type is ContentType.MOVIE:
        return "Radarr"
    if content_type is ContentType.MUSIC:
        return "Lidarr"
    return "Sonarr"


def _rejection_text(release: SearchResult, content_type: ContentType) -> str:
    """GRAB-02: why *arr refuses a release, before the user takes it anyway."""
    reasons = release.rejections or ["причина не указана"]
    lines = "\n".join(f"• {html.escape(r)}" for r in reasons)
    return (
        f"⚠️ <b>{_arr_name(content_type)} отклоняет этот релиз</b>\n\n"
        f"<i>{html.escape(release.title)}</i>\n\n"
        f"Причины:\n{lines}\n\n"
        "Скачать всё равно?"
    )


async def _show_rejection_card(message: Message, session: SearchSession, idx: int) -> None:
    await message.edit_text(
        _rejection_text(session.results[idx], session.content_type),
        reply_markup=Keyboards.rejected_confirm(session.list_token, idx),
        parse_mode="HTML",
    )


@router.callback_query(GrabBestCB.filter())
async def handle_grab_best(
    callback: CallbackQuery, callback_data: GrabBestCB, db_user: User, db: Database,
) -> None:
    """Handle 'Grab Best' button - grab the highest scored release."""
    message = accessible_message(callback)
    if message is None:
        return

    user_id = callback.from_user.id
    if not await _search._claim_grab(user_id):
        await callback.answer("⏳ Уже обрабатываю предыдущий запрос…")
        return
    try:
        search_service, add_service = await _search.get_services()

        session = await db.get_session(user_id)

        if session is None or not _catalogue_list(session, callback_data.tok):
            await answer_stale(callback)
            return

        # Audit 2026-09-23 GRAB-02: the keyboard may predate the list it
        # points at, so the verdict is re-checked here — never grab a
        # release *arr rejected (or scored negative) from the one-tap button.
        settings = get_settings()
        result = grab_best_candidate(
            session.results,
            auto_grab_enabled=db_user.preferences.auto_grab_enabled,
            threshold=settings.auto_grab_score_threshold,
        )
        if result is None:
            if session.results[0].rejected:
                # Ambiguity resolution 2026-09-24: never grab, show why instead.
                await callback.answer()
                await _show_rejection_card(message, session, 0)
                return
            await callback.answer(
                "Лучший релиз не проходит профиль качества — выберите вручную",
                show_alert=True,
            )
            return
        session.selected_result = result
        await db.save_session(user_id, session)

        await callback.answer("Скачиваю лучший релиз...")
        await message.edit_text("⏳ Скачиваю лучший релиз...")

        # Lookup and grab
        await _search.grab_release(message, session, db_user, db, search_service, add_service)
    finally:
        _search._release_grab(user_id)


@router.callback_query(GrabCB.filter())
async def handle_confirm_grab(
    callback: CallbackQuery, callback_data: GrabCB, db_user: User, db: Database,
) -> None:
    """Grab the release a card shows — movie/series only.

    BUG-27 history: this used to be the single ``confirm_grab`` dispatch point
    for movies, series AND the music artist card, choosing by
    ``session.selected_content``. Audit 2026-09-23 GRAB-03 split it: the
    artist card has its own ``MusicAddCB`` (bot/handlers/music.py), and this
    button carries the list token and the release index, so a card from an
    older list — or from another content type — can never grab.
    """
    message = accessible_message(callback)
    if message is None:
        return

    user_id = callback.from_user.id
    session = await _load_pick(db, user_id, callback_data.tok, callback_data.idx)
    if session is None:
        await answer_stale(callback)
        return

    # GRAB-02: a release *arr rejected needs a second, explicit tap.
    if session.results[callback_data.idx].rejected and not callback_data.ok:
        await callback.answer()
        await _show_rejection_card(message, session, callback_data.idx)
        return

    # RACE-01: reject a concurrent second grab for this user.
    if not await _search._claim_grab(user_id):
        await callback.answer("⏳ Уже обрабатываю предыдущий запрос…")
        return
    try:
        session = await _load_pick(db, user_id, callback_data.tok, callback_data.idx)
        if session is None:
            await answer_stale(callback)
            return
        session.selected_result = session.results[callback_data.idx]
        search_service, add_service = await _search.get_services()
        await callback.answer("Обработка...")
        await message.edit_text("⏳ Обрабатываю запрос...")

        await _search.grab_release(message, session, db_user, db, search_service, add_service)
    finally:
        _search._release_grab(user_id)


async def grab_release(
    message: Message,
    session: SearchSession,
    db_user: User,
    db: Database,
    search_service: SearchService,
    add_service: AddService,
) -> None:
    """Perform the actual grab operation."""
    await _search._execute_grab(message, session, db_user, db, search_service, add_service)


def _decide_monitor_type(result, force_download: bool, override: str | None = None) -> str:
    """Choose the monitor scope for a grabbed series/anime release.

    Feature #2: an explicit user preset (``override``) always wins.

    BUG-04: otherwise, a single targeted season (non-pack) must NOT be added
    with a type that monitors every season — that would silently pull the whole
    show. "future" keeps the back catalogue untouched while letting Sonarr pick
    up episodes that have not aired yet.
    """
    if override:
        return override
    if force_download:
        return "all"
    if result.is_season_pack:
        return "all"
    if result.detected_season is not None:
        # BUG-04 держится: старые сезоны не тянем. Но "none" оставляет сериал
        # неспособным подхватить и НОВЫЕ серии — а пользователь, взявший серию
        # идущего шоу, хочет продолжение. Живой замер 2026-08-12
        # (analysis/2026-08-12-seasonpass-probe.md): "future" промониторил
        # ровно 4 невышедшие серии из 34 и снял все вышедшие.
        return "future"
    return "all"


async def _apply_series_monitoring(
    add_service: AddService, arr_id: int, result: SearchResult, session: SearchSession,
) -> None:
    """Monitoring after a successful series grab (audit 2026-09-23 GRAB-05).

    - A season pack for season N monitors season N only — never "all". On a
      series this search added, the series flag goes up with it (the add
      left everything off); on one already in the library, only that
      season's flag changes.
    - Otherwise a preset (the user's, or ``_decide_monitor_type``'s) is
      applied only to a series THIS search added (``session.title_created``).
      A series that was already in the library keeps the monitoring its
      owner set — grabbing one episode used to overwrite it.
    """
    sonarr = add_service.sonarr
    created = session.title_created
    pack_season = result.detected_season if result.is_season_pack else None

    if pack_season is not None and not (created and session.monitor_type):
        await sonarr.monitor_season(arr_id, pack_season, monitor_series=created)
        logger.info("monitor_scope_applied", arr_id=arr_id, season=pack_season, created=created)
        return

    if not created:
        logger.info("monitor_scope_kept", arr_id=arr_id, reason="title_existed")
        return

    # Сериал добавлен с monitor="none", поэтому поднять один флаг сериала
    # мало: все сезоны остаются немониторимыми, и новые серии не
    # подхватываются. Область, которую пользователь выбрал в
    # `handle_season_preset`, лежит в `session.monitor_type`; если меню не
    # открывали, её решает `_decide_monitor_type`.
    monitor = _decide_monitor_type(result, False, override=session.monitor_type)
    await sonarr.set_series_monitored(arr_id, True)
    await sonarr.set_season_monitoring(arr_id, monitor)
    logger.info(
        "monitor_scope_applied",
        arr_id=arr_id,
        monitor=monitor,
        from_user=session.monitor_type is not None,
    )


def _resolve_folder(folders: list, preferred_id) -> str:
    """Resolve root folder path from user preference or first available.

    LOGIC-11: thin wrapper kept for backward compatibility (tests/callers
    patch/import this by name) — the real logic now lives in
    ``AddService.resolve_root_folder``, shared with trending.py/music.py.
    """
    return AddService.resolve_root_folder(folders, preferred_id)


async def _execute_grab(
    message: Message,
    session: SearchSession,
    db_user: User,
    db: Database,
    search_service: SearchService,
    add_service: AddService,
    *,
    force_download: bool = False,
    auto_search: bool = False,
) -> None:
    """Common grab logic for normal, force and *arr-auto-search grabs.

    Audit 2026-09-23: ``auto_search=True`` is the explicit "🔍 Пусть *arr ищет
    сам" choice (GRAB-08) — same monitoring follow-up as a grab, but the
    message says *arr may take a different release. A failed grab keeps the
    session when there is something to offer next (auto-search, or the way
    back to a card whose rejection reasons were just shown).

    Rollback 2026-08-10 (Task 12): the title was already added to Radarr/
    Sonarr (and its releases listed) before this point — see
    ``commands.py``'s module docstring for how a query becomes a library
    entry — so this still doesn't re-look-up the content or pick a quality
    profile/root folder (that happened at add time). Grabbing is
    ``AddService.grab_release`` for the ONE release the user selected —
    unlike the previous backend's ``grab_with_fallback``, *arr's own
    interactive search already excludes releases it cannot act on, so there is no
    multi-candidate retry loop to drive (no task in this rollback has
    specified one; see ``AddService.grab_with_fallback``'s docstring).
    """
    user_id = session.user_id
    result = session.selected_result

    if not result:
        await message.edit_text(Formatters.format_error("Релиз не выбран"))
        return

    title = session.selected_content
    # Music is dispatched to its own handler well before this point; narrowing
    # here keeps the movie/series attribute access below honest (and lets
    # mypy carry the narrowing through the rest of the function, unlike an
    # isinstance check split across separate if/elif branches).
    if not isinstance(title, (MovieInfo, SeriesInfo)):
        await message.edit_text(
            Formatters.format_error("Тайтл не найден в Radarr/Sonarr — повторите поиск")
        )
        await db.delete_session(user_id)
        return

    arr_id = title.radarr_id if isinstance(title, MovieInfo) else title.sonarr_id
    if arr_id is None:
        # The session predates this rollback, or the title vanished from the
        # library between search and grab.
        await message.edit_text(
            Formatters.format_error("Тайтл не найден в Radarr/Sonarr — повторите поиск")
        )
        await db.delete_session(user_id)
        return

    try:
        if auto_search:
            success, action = await add_service.auto_search(
                result, session.content_type, arr_id=arr_id,
            )
        else:
            success, action = await add_service.grab_release(
                result,
                session.content_type,
                arr_id=arr_id,
                force_download=force_download,
            )

        action.user_id = user_id
        await db.log_action(action)

        if success:
            # The title was added UNMONITORED so that merely browsing releases
            # could not enlist it in *arr's RSS loop (see `_resolve_arr_entry`).
            # Now that a release has actually been taken, the user does want it
            # tracked — future upgrades and missing episodes included. A failure
            # here must not undo a successful grab, so it is logged, not raised.
            #
            # Audit 2026-09-23 GRAB-07: a force (qBit) grab bypasses *arr
            # entirely, so it must not change *arr's monitoring either.
            if not force_download:
                try:
                    if isinstance(title, MovieInfo):
                        await add_service.radarr.set_movie_monitored(arr_id, True)
                    else:
                        await _apply_series_monitoring(
                            add_service, arr_id, result, session,
                        )
                except Exception as e:
                    logger.warning("monitor_enable_failed", arr_id=arr_id, error=str(e))

            year_str = f" ({title.year})" if title.year else ""
            if auto_search:
                body = (
                    f"<b>{html.escape(title.title)}</b>{year_str}\n\n"
                    f"🔍 {_arr_name(session.content_type)} ищет сам — релиз может быть другим"
                )
            else:
                body = (
                    f"<b>{html.escape(title.title)}</b>{year_str}\n\n"
                    f"Релиз: <i>{html.escape(result.title)}</i>"
                )
            await message.edit_text(Formatters.format_success(body), parse_mode="HTML")
            await db.delete_session(user_id)
            return

        # format_error escapes its argument itself.
        error_text = action.error_message or "Не удалось скачать релиз"
        outcome = grab_outcome(action) if not (force_download or auto_search) else None
        if outcome in (GRAB_OUTCOME_UNREACHABLE, GRAB_OUTCOME_REJECTED):
            arr_name = _arr_name(session.content_type)
            if outcome == GRAB_OUTCOME_REJECTED:
                text = f"{arr_name} отклонил релиз: {error_text}"
            else:
                text = (
                    f"{error_text}\n\n"
                    f"Можно попросить {arr_name} найти раздачу самому — "
                    "релиз может быть другим."
                )
            # The session stays: the buttons below act on it.
            await message.edit_text(
                Formatters.format_error(text),
                reply_markup=Keyboards.grab_failed(
                    session.list_token, _selected_index(session), arr_name,
                    offer_auto_search=outcome == GRAB_OUTCOME_UNREACHABLE,
                ),
                parse_mode="HTML",
            )
            return

        await message.edit_text(Formatters.format_error(error_text))
        await db.delete_session(user_id)

    except Exception as e:
        logger.error("Grab failed", error=str(e), exc_info=True)
        await message.edit_text(Formatters.format_error("Операция временно недоступна"))
        await db.delete_session(user_id)


@router.callback_query(ForceGrabCB.filter())
async def handle_force_grab(
    callback: CallbackQuery, callback_data: ForceGrabCB, db_user: User, db: Database,
) -> None:
    """Handle force grab button - downloads directly via qBittorrent."""
    message = accessible_message(callback)
    if message is None:
        return

    user_id = db_user.tg_id
    if await _load_pick(db, user_id, callback_data.tok, callback_data.idx) is None:
        await answer_stale(callback)
        return

    # RACE-01: reject a concurrent second grab (e.g. Confirm then Force) for this user.
    if not await _search._claim_grab(user_id):
        await callback.answer("⏳ Уже обрабатываю предыдущий запрос…")
        return
    try:
        session = await _load_pick(db, user_id, callback_data.tok, callback_data.idx)
        if session is None:
            await answer_stale(callback)
            return
        await callback.answer("Загружаю напрямую...")
        session.selected_result = session.results[callback_data.idx]

        search_service, add_service = await _search.get_services()

        if not add_service.qbittorrent:
            await message.edit_text(Formatters.format_error("qBittorrent не настроен"))
            await db.delete_session(user_id)
            return

        await _search._execute_grab(message, session, db_user, db, search_service, add_service, force_download=True)
    finally:
        _search._release_grab(user_id)


@router.callback_query(F.data == CallbackData.SEASON_MENU)
async def handle_season_menu(callback: CallbackQuery, db_user: User, db: Database) -> None:
    """Feature #2: show the season-monitoring preset picker for a series."""
    message = accessible_message(callback)
    if message is None:
        return
    user_id = callback.from_user.id
    session = await db.get_session(user_id)
    if not session or not session.selected_result:
        await callback.answer("Сессия истекла. Начните новый поиск.", show_alert=True)
        return
    if not session.title_created:
        # GRAB-05: the preset is applied only to a series this search added.
        await callback.answer(_EXISTING_SERIES_NOTE, show_alert=True)
        return
    await callback.answer()
    current = season_preset_name(session.monitor_type)
    await message.edit_text(
        f"📺 <b>Мониторинг сезонов</b>\n\nСейчас: <b>{current}</b>\n\nВыберите, какие сезоны отслеживать:",
        reply_markup=Keyboards.season_presets(),
        parse_mode="HTML",
    )


@router.callback_query(SeasonPresetCB.filter())
async def handle_season_preset(
    callback: CallbackQuery, callback_data: SeasonPresetCB, db_user: User, db: Database
) -> None:
    """Feature #2: store the chosen monitoring preset and return to the release card."""
    message = accessible_message(callback)
    if message is None:
        return
    user_id = callback.from_user.id

    # DB-02: lock the read-modify-write cycle around the season preset choice.
    async with db.session_lock(user_id):
        session = await db.get_session(user_id)
        if not session or not session.selected_result:
            await callback.answer("Сессия истекла. Начните новый поиск.", show_alert=True)
            return

        if not session.title_created:
            await callback.answer(_EXISTING_SERIES_NOTE, show_alert=True)
            return

        preset = callback_data.preset
        if preset not in _SEASON_PRESETS:
            await callback.answer("Неверный выбор", show_alert=True)
            return

        session.monitor_type = preset
        if not await db.update_session(user_id, session):
            await callback.answer("Сессия истекла. Начните новый поиск.", show_alert=True)
            return

    await callback.answer(f"Мониторинг: {season_preset_name(preset)}")
    _, add_service = await _search.get_services()
    has_qbittorrent = add_service.qbittorrent is not None
    result = session.selected_result
    text = Formatters.format_release_card(result, session.selected_content)
    await message.edit_text(
        f"{text}\n\n📺 Мониторинг сезонов: <b>{season_preset_name(preset)}</b>",
        reply_markup=Keyboards.release_details(
            result, session.content_type,
            show_force_grab=has_qbittorrent,
            content=session.selected_content,
            list_token=session.list_token,
            idx=_selected_index(session),
            season_menu=session.title_created,
        ),
        parse_mode="HTML",
    )


@router.callback_query(F.data == CallbackData.SEASON_BACK)
async def handle_season_back(callback: CallbackQuery, db_user: User, db: Database) -> None:
    """BUG-16: "Назад" from the season-monitoring picker must return to the
    release card WITHOUT clearing the user's selection — the generic
    CallbackData.BACK handler (handle_back) clears selected_result/
    selected_content and jumps back to the results list, which throws away
    the release the user was configuring. Re-renders the same card
    handle_release_selection/handle_season_preset show, by the same pattern.
    """
    message = accessible_message(callback)
    if message is None:
        return
    user_id = callback.from_user.id
    session = await db.get_session(user_id)
    if not session or not session.selected_result:
        await callback.answer("Сессия истекла. Начните новый поиск.", show_alert=True)
        return

    await callback.answer()
    _, add_service = await _search.get_services()
    has_qbittorrent = add_service.qbittorrent is not None
    result = session.selected_result
    # Audit UI-15: back to the whole card, not a bare release block.
    text = Formatters.format_release_card(result, session.selected_content)
    if session.monitor_type:
        text += f"\n\n📺 Мониторинг сезонов: <b>{season_preset_name(session.monitor_type)}</b>"
    await message.edit_text(
        text,
        reply_markup=Keyboards.release_details(
            result, session.content_type,
            show_force_grab=has_qbittorrent,
            content=session.selected_content,
            list_token=session.list_token,
            idx=_selected_index(session),
            season_menu=session.title_created,
        ),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith(CallbackData.SEASON_PRESET))
async def handle_legacy_season_preset(callback: CallbackQuery) -> None:
    """r5: legacy ``season_set:preset`` string buttons from messages sent
    before the SeasonPresetCB migration — surface an explicit alert instead
    of falling through unhandled.
    """
    await callback.answer(MSG_OLD_BUTTON, show_alert=True)


@router.callback_query(F.data == CallbackData.GRAB_BEST)
@router.callback_query(F.data == CallbackData.CONFIRM_GRAB)
@router.callback_query(F.data == CallbackData.FORCE_GRAB)
async def handle_legacy_grab(callback: CallbackQuery) -> None:
    """Audit 2026-09-23: plain ``grab_best``/``confirm_grab``/``force_grab``
    buttons from messages sent before list tokens — they name no list, so
    they must not act on whatever the session holds now."""
    await answer_stale(callback)


@router.callback_query(AutoSearchCB.filter())
async def handle_auto_search(
    callback: CallbackQuery, callback_data: AutoSearchCB, db_user: User, db: Database,
) -> None:
    """"🔍 Пусть *arr ищет сам" — the explicit form of what the push chain used
    to do silently (audit 2026-09-23 GRAB-08)."""
    message = accessible_message(callback)
    if message is None:
        return

    user_id = callback.from_user.id
    if await _load_pick(db, user_id, callback_data.tok, callback_data.idx) is None:
        await answer_stale(callback)
        return

    if not await _search._claim_grab(user_id):
        await callback.answer("⏳ Уже обрабатываю предыдущий запрос…")
        return
    try:
        session = await _load_pick(db, user_id, callback_data.tok, callback_data.idx)
        if session is None:
            await answer_stale(callback)
            return
        session.selected_result = session.results[callback_data.idx]
        search_service, add_service = await _search.get_services()
        await callback.answer("Запускаю поиск...")
        await _search._execute_grab(
            message, session, db_user, db, search_service, add_service, auto_search=True,
        )
    finally:
        _search._release_grab(user_id)
