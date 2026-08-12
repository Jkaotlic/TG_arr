"""Grab confirmation, execution and season-monitoring preset handlers."""

import html

import structlog
from aiogram import F
from aiogram.types import CallbackQuery, Message

from bot.db import Database
from bot.handlers.common import accessible_message
from bot.models import MovieInfo, SearchSession, SeriesInfo, User
from bot.services.add_service import AddService
from bot.services.search_service import SearchService
from bot.ui.callbacks import SeasonPresetCB
from bot.ui.formatters import Formatters
from bot.ui.keyboards import CallbackData, Keyboards

from bot.handlers import search as _search
from .services import router

logger = structlog.get_logger()


async def _search_picked_season(sonarr, series_id: int, season_number: int) -> dict:
    """Search exactly the season the user picked.

    Sonarr has a dedicated SeasonSearch command; falling back to a full
    SeriesSearch would re-grab every other season the user did not ask for.
    """
    return await sonarr.search_season(series_id, season_number)


# Feature #2: season-monitoring presets exposed on the series release card.
# These strings match Sonarr's own `monitor` addOptions values verbatim (see
# SonarrClient.add_series) — rollback 2026-08-10 removed the previous
# backend's translation table this used to feed, since there is nothing left
# to translate to.
_SEASON_PRESETS = {"all", "future", "latestSeason", "firstSeason", "none"}


@router.callback_query(F.data == CallbackData.GRAB_BEST)
async def handle_grab_best(callback: CallbackQuery, db_user: User, db: Database) -> None:
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

        if not session or not session.results:
            await callback.answer("Сессия истекла. Начните новый поиск.", show_alert=True)
            return

        result = session.results[0]  # Best result
        session.selected_result = result
        await db.save_session(user_id, session)

        await callback.answer("Скачиваю лучший релиз...")
        await message.edit_text("⏳ Скачиваю лучший релиз...")

        # Lookup and grab
        await _search.grab_release(message, session, db_user, db, search_service, add_service)
    finally:
        _search._release_grab(user_id)


@router.callback_query(F.data == CallbackData.CONFIRM_GRAB)
async def handle_confirm_grab(callback: CallbackQuery, db_user: User, db: Database) -> None:
    """
    Handle grab confirmation — single dispatch point for movie/series/music.

    BUG-27: music handler previously attached its own `F.data == CONFIRM_GRAB`
    callback and was included before search_router, so it silently swallowed
    the event for movies/series (aiogram does not cascade handlers after a
    routed match). Now we dispatch by session.selected_content type here.
    """
    message = accessible_message(callback)
    if message is None:
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
    if not await _search._claim_grab(user_id):
        await callback.answer("⏳ Уже обрабатываю предыдущий запрос…")
        return
    try:
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
) -> None:
    """Common grab logic for normal and force grab.

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
            try:
                if isinstance(title, MovieInfo):
                    await add_service.radarr.set_movie_monitored(arr_id, True)
                else:
                    # Сериал добавлен с monitor="none", поэтому поднять один
                    # флаг сериала мало: все сезоны остаются немониторимыми, и
                    # новые серии не подхватываются. Область, которую
                    # пользователь выбрал в `handle_season_preset`, лежит в
                    # `session.monitor_type`; если меню не открывали, её
                    # решает `_decide_monitor_type`.
                    monitor = _decide_monitor_type(
                        result, force_download, override=session.monitor_type,
                    )
                    await add_service.sonarr.set_series_monitored(arr_id, True)
                    await add_service.sonarr.set_season_monitoring(arr_id, monitor)
                    logger.info(
                        "monitor_scope_applied",
                        arr_id=arr_id,
                        monitor=monitor,
                        from_user=session.monitor_type is not None,
                    )
            except Exception as e:
                logger.warning("monitor_enable_failed", arr_id=arr_id, error=str(e))

            year_str = f" ({title.year})" if title.year else ""
            await message.edit_text(
                Formatters.format_success(
                    f"<b>{html.escape(title.title)}</b>{year_str}\n\n"
                    f"Релиз: <i>{html.escape(result.title)}</i>"
                ),
                parse_mode="HTML",
            )
        else:
            error_text = action.error_message or "Не удалось скачать релиз"
            await message.edit_text(Formatters.format_error(error_text))

        await db.delete_session(user_id)

    except Exception as e:
        logger.error("Grab failed", error=str(e), exc_info=True)
        await message.edit_text(Formatters.format_error("Операция временно недоступна"))
        await db.delete_session(user_id)


@router.callback_query(F.data == CallbackData.FORCE_GRAB)
async def handle_force_grab(callback: CallbackQuery, db_user: User, db: Database) -> None:
    """Handle force grab button - downloads directly via qBittorrent."""
    message = accessible_message(callback)
    if message is None:
        return

    user_id = db_user.tg_id
    # RACE-01: reject a concurrent second grab (e.g. Confirm then Force) for this user.
    if not await _search._claim_grab(user_id):
        await callback.answer("⏳ Уже обрабатываю предыдущий запрос…")
        return
    try:
        await callback.answer("Загружаю напрямую...")

        session = await db.get_session(user_id)
        if not session or not session.selected_result:
            await message.edit_text(Formatters.format_error("Сессия истекла. Повторите поиск."))
            return

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
    await callback.answer()
    current = session.monitor_type or "auto"
    await message.edit_text(
        f"📺 <b>Мониторинг сезонов</b>\n\nТекущий: <code>{current}</code>\n\nВыберите, какие сезоны отслеживать:",
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

        preset = callback_data.preset
        if preset not in _SEASON_PRESETS:
            await callback.answer("Неверный выбор", show_alert=True)
            return

        session.monitor_type = preset
        if not await db.update_session(user_id, session):
            await callback.answer("Сессия истекла. Начните новый поиск.", show_alert=True)
            return

    await callback.answer(f"Мониторинг: {preset}")
    _, add_service = await _search.get_services()
    has_qbittorrent = add_service.qbittorrent is not None
    result = session.selected_result
    text = Formatters.format_release_details(result)
    await message.edit_text(
        f"{text}\n\n📺 Мониторинг: <b>{preset}</b>",
        reply_markup=Keyboards.release_details(
            result, session.content_type,
            show_force_grab=has_qbittorrent,
            content=session.selected_content,
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
    text = Formatters.format_release_details(result)
    await message.edit_text(
        text,
        reply_markup=Keyboards.release_details(
            result, session.content_type,
            show_force_grab=has_qbittorrent,
            content=session.selected_content,
        ),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith(CallbackData.SEASON_PRESET))
async def handle_legacy_season_preset(callback: CallbackQuery) -> None:
    """r5: legacy ``season_set:preset`` string buttons from messages sent
    before the SeasonPresetCB migration — surface an explicit alert instead
    of falling through unhandled.
    """
    await callback.answer("Кнопка устарела — откройте карточку релиза заново", show_alert=True)
