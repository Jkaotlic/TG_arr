"""Grab confirmation, execution and season-monitoring preset handlers."""

import asyncio
import html

import structlog
from aiogram import F
from aiogram.types import CallbackQuery, Message

from bot.db import Database
from bot.models import ContentType, MovieInfo, SearchSession, SeriesInfo, User
from bot.services.add_service import AddService
from bot.services.search_service import SearchService
from bot.ui.callbacks import SeasonPresetCB
from bot.ui.formatters import Formatters
from bot.ui.keyboards import CallbackData, Keyboards

from bot.handlers import search as _search
from .results import _resolve_movie, _resolve_series
from .services import router

logger = structlog.get_logger()

# Feature #2: season-monitoring presets exposed on the series release card.
_SEASON_PRESETS = {"all", "future", "latestSeason", "firstSeason", "none"}


@router.callback_query(F.data == CallbackData.GRAB_BEST)
async def handle_grab_best(callback: CallbackQuery, db_user: User, db: Database) -> None:
    """Handle 'Grab Best' button - grab the highest scored release."""
    if not callback.message:
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
        await callback.message.edit_text("⏳ Скачиваю лучший релиз...")

        # Lookup and grab
        await _search.grab_release(callback.message, session, db_user, db, search_service, add_service)
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
    if not await _search._claim_grab(user_id):
        await callback.answer("⏳ Уже обрабатываю предыдущий запрос…")
        return
    try:
        search_service, add_service = await _search.get_services()
        await callback.answer("Обработка...")
        await callback.message.edit_text("⏳ Обрабатываю запрос...")

        await _search.grab_release(callback.message, session, db_user, db, search_service, add_service)
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


def _resolve_folder(folders: list, preferred_id: int | None) -> str:
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
                # LOGIC-06: reuse detection's lookup_candidates when possible
                # (grab_best skips handle_release_selection entirely, so this
                # is otherwise always a fresh lookup).
                movie = await _resolve_movie(
                    session, search_service, lookup_term, result.detected_year, parsed.get("year")
                )
                if not movie:
                    await message.edit_text(Formatters.format_error("Не удалось найти фильм в Radarr"))
                    return

            # PERF-07b: independent reads — one RTT instead of two sequential ones.
            profiles, folders = await asyncio.gather(
                add_service.get_radarr_profiles(), add_service.get_radarr_root_folders()
            )

            if not profiles or not folders:
                await message.edit_text(Formatters.format_error("Нет профилей качества или папок в Radarr"))
                return

            profile_id = AddService.resolve_profile(profiles, prefs.radarr_quality_profile_id).id
            folder_path = AddService.resolve_root_folder(folders, prefs.radarr_root_folder_id)

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
                # LOGIC-06: reuse detection's lookup_candidates when possible.
                series = await _resolve_series(
                    session, search_service, lookup_term, result.detected_year, parsed.get("year")
                )
                if not series:
                    await message.edit_text(Formatters.format_error("Не удалось найти сериал в Sonarr"))
                    return

            # PERF-07b: independent reads — one RTT instead of two sequential ones.
            profiles, folders = await asyncio.gather(
                add_service.get_sonarr_profiles(), add_service.get_sonarr_root_folders()
            )

            if not profiles or not folders:
                await message.edit_text(Formatters.format_error("Нет профилей качества или папок в Sonarr"))
                return

            profile_id = AddService.resolve_profile(profiles, prefs.sonarr_quality_profile_id).id
            folder_path = AddService.resolve_root_folder(folders, prefs.sonarr_root_folder_id)

            # Determine monitor type: user preset (#2) wins, else auto (BUG-04/BUG-32)
            monitor_type = _search._decide_monitor_type(result, force_download, override=session.monitor_type)

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


@router.callback_query(F.data == CallbackData.FORCE_GRAB)
async def handle_force_grab(callback: CallbackQuery, db_user: User, db: Database) -> None:
    """Handle force grab button - downloads directly via qBittorrent."""
    if not callback.message:
        return

    user_id = db_user.tg_id
    # RACE-01: reject a concurrent second grab (e.g. Confirm then Force) for this user.
    if not await _search._claim_grab(user_id):
        await callback.answer("⏳ Уже обрабатываю предыдущий запрос…")
        return
    try:
        await callback.answer("Загружаю напрямую...")

        message = callback.message
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


@router.callback_query(SeasonPresetCB.filter())
async def handle_season_preset(
    callback: CallbackQuery, callback_data: SeasonPresetCB, db_user: User, db: Database
) -> None:
    """Feature #2: store the chosen monitoring preset and return to the release card."""
    if not callback.message:
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
    await callback.message.edit_text(
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
    if not callback.message:
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
    await callback.message.edit_text(
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
