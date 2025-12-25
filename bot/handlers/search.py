"""Search and content management handlers."""

from typing import Any, Optional

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
    SearchResult,
    SearchSession,
    SeriesInfo,
    User,
)
from bot.services.add_service import AddService
from bot.services.scoring import ScoringService
from bot.services.search_service import SearchService
from bot.ui.formatters import Formatters
from bot.ui.keyboards import CallbackData, Keyboards

logger = structlog.get_logger()
router = Router()


def get_services() -> tuple[SearchService, AddService, ScoringService]:
    """Get service instances."""
    from bot.clients import ProwlarrClient, RadarrClient, SonarrClient

    settings = get_settings()

    prowlarr = ProwlarrClient(settings.prowlarr_url, settings.prowlarr_api_key)
    radarr = RadarrClient(settings.radarr_url, settings.radarr_api_key)
    sonarr = SonarrClient(settings.sonarr_url, settings.sonarr_api_key)

    scoring = ScoringService()
    search_service = SearchService(prowlarr, radarr, sonarr, scoring)
    add_service = AddService(prowlarr, radarr, sonarr)

    return search_service, add_service, scoring


async def get_db() -> Database:
    """Get database instance."""
    settings = get_settings()
    db = Database(settings.database_path)
    await db.connect()
    return db


@router.message(Command("search"))
async def cmd_search(message: Message, db_user: User) -> None:
    """Handle /search <query> command - auto-detect content type."""
    if not message.text:
        await message.answer("Please provide a search query: `/search Dune 2021`", parse_mode="Markdown")
        return

    query = message.text.replace("/search", "").strip()
    if not query:
        await message.answer("Please provide a search query: `/search Dune 2021`", parse_mode="Markdown")
        return

    await process_search(message, query, ContentType.UNKNOWN, db_user)


@router.message(Command("movie"))
async def cmd_movie(message: Message, db_user: User) -> None:
    """Handle /movie <query> command."""
    if not message.text:
        await message.answer("Please provide a movie title: `/movie Dune 2021`", parse_mode="Markdown")
        return

    query = message.text.replace("/movie", "").strip()
    if not query:
        await message.answer("Please provide a movie title: `/movie Dune 2021`", parse_mode="Markdown")
        return

    await process_search(message, query, ContentType.MOVIE, db_user)


@router.message(Command("series"))
async def cmd_series(message: Message, db_user: User) -> None:
    """Handle /series <query> command."""
    if not message.text:
        await message.answer("Please provide a series title: `/series Breaking Bad`", parse_mode="Markdown")
        return

    query = message.text.replace("/series", "").strip()
    if not query:
        await message.answer("Please provide a series title: `/series Breaking Bad`", parse_mode="Markdown")
        return

    await process_search(message, query, ContentType.SERIES, db_user)


@router.message(F.text & ~F.text.startswith("/"))
async def handle_text_search(message: Message, db_user: User) -> None:
    """Handle plain text as search query."""
    if not message.text:
        return

    query = message.text.strip()
    await process_search(message, query, ContentType.UNKNOWN, db_user)


async def process_search(
    message: Message,
    query: str,
    content_type: ContentType,
    db_user: User,
) -> None:
    """Process a search query."""
    settings = get_settings()
    search_service, add_service, scoring = get_services()
    db = await get_db()

    try:
        user_id = message.from_user.id if message.from_user else 0
        log = logger.bind(user_id=user_id, query=query)

        # Parse query for metadata
        parsed = search_service.parse_query(query)
        log.info("Parsed query", parsed=parsed)

        # Detect content type if unknown
        if content_type == ContentType.UNKNOWN:
            status_msg = await message.answer("🔍 Detecting content type...")

            # If season info in query, it's likely a series
            if parsed["season"] is not None:
                content_type = ContentType.SERIES
            else:
                content_type = await search_service.detect_content_type(parsed["title"])

            if content_type == ContentType.UNKNOWN:
                # Ask user to choose
                await status_msg.edit_text(
                    f"🤔 Is **{query}** a movie or a series?",
                    reply_markup=Keyboards.content_type_selection(),
                    parse_mode="Markdown",
                )

                # Save partial session
                session = SearchSession(
                    user_id=user_id,
                    query=query,
                    content_type=ContentType.UNKNOWN,
                )
                await db.save_session(user_id, session)
                return

            await status_msg.delete()

        # Search for releases
        status_msg = await message.answer(f"🔍 Searching for releases...")

        results = await search_service.search_releases(query, content_type)

        if not results:
            await status_msg.edit_text(
                Formatters.format_warning(f"No releases found for **{query}**"),
                parse_mode="Markdown",
            )
            return

        # Save search to database
        await db.save_search(user_id, query, content_type, results)

        # Create session
        session = SearchSession(
            user_id=user_id,
            query=query,
            content_type=content_type,
            results=results,
            current_page=0,
        )
        await db.save_session(user_id, session)

        # Check if we should show "grab best" button
        best_result = results[0] if results else None
        show_grab_best = (
            best_result
            and best_result.calculated_score >= settings.auto_grab_score_threshold
            and db_user.preferences.auto_grab_enabled
        )

        # Show results
        per_page = settings.results_per_page
        total_pages = (len(results) + per_page - 1) // per_page
        page_results = results[:per_page]

        text = Formatters.format_search_results_page(
            page_results,
            0,
            total_pages,
            query,
            content_type,
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
            parse_mode="Markdown",
        )

        # Log action
        action = ActionLog(
            user_id=user_id,
            action_type=ActionType.SEARCH,
            content_type=content_type,
            query=query,
        )
        await db.log_action(action)

    except Exception as e:
        log.error("Search failed", error=str(e))
        await message.answer(Formatters.format_error(str(e)))
    finally:
        await db.close()
        # Close service clients
        await search_service.prowlarr.close()
        await search_service.radarr.close()
        await search_service.sonarr.close()


@router.callback_query(F.data.startswith(CallbackData.TYPE_MOVIE) | F.data.startswith(CallbackData.TYPE_SERIES))
async def handle_type_selection(callback: CallbackQuery, db_user: User) -> None:
    """Handle content type selection."""
    if not callback.data or not callback.message:
        return

    db = await get_db()

    try:
        user_id = callback.from_user.id
        session = await db.get_session(user_id)

        if not session:
            await callback.answer("Session expired. Please start a new search.", show_alert=True)
            return

        content_type = ContentType.MOVIE if callback.data == CallbackData.TYPE_MOVIE else ContentType.SERIES

        # Update session and continue search
        session.content_type = content_type
        await db.save_session(user_id, session)

        await callback.answer()

        # Create a fake message object to reuse process_search
        await callback.message.delete()
        await process_search(
            callback.message,
            session.query,
            content_type,
            db_user,
        )
    finally:
        await db.close()


@router.callback_query(F.data.startswith(CallbackData.PAGE))
async def handle_pagination(callback: CallbackQuery, db_user: User) -> None:
    """Handle pagination buttons."""
    if not callback.data or not callback.message:
        return

    settings = get_settings()
    db = await get_db()

    try:
        user_id = callback.from_user.id
        session = await db.get_session(user_id)

        if not session or not session.results:
            await callback.answer("Session expired. Please start a new search.", show_alert=True)
            return

        # Parse page number
        try:
            page = int(callback.data.replace(CallbackData.PAGE, ""))
        except ValueError:
            await callback.answer("Invalid page", show_alert=True)
            return

        per_page = settings.results_per_page
        total_pages = (len(session.results) + per_page - 1) // per_page

        if page < 0 or page >= total_pages:
            await callback.answer("Invalid page", show_alert=True)
            return

        # Update session
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
            parse_mode="Markdown",
        )

        await callback.answer()
    finally:
        await db.close()


@router.callback_query(F.data.startswith(CallbackData.RELEASE))
async def handle_release_selection(callback: CallbackQuery, db_user: User) -> None:
    """Handle release selection."""
    if not callback.data or not callback.message:
        return

    db = await get_db()
    search_service, add_service, _ = get_services()

    try:
        user_id = callback.from_user.id
        session = await db.get_session(user_id)

        if not session or not session.results:
            await callback.answer("Session expired. Please start a new search.", show_alert=True)
            return

        # Parse release index
        try:
            idx = int(callback.data.replace(CallbackData.RELEASE, ""))
        except ValueError:
            await callback.answer("Invalid selection", show_alert=True)
            return

        if idx < 0 or idx >= len(session.results):
            await callback.answer("Invalid selection", show_alert=True)
            return

        result = session.results[idx]
        session.selected_result = result
        await db.save_session(user_id, session)

        # Show release details
        text = Formatters.format_release_details(result)

        # Now need to look up the actual content in Radarr/Sonarr
        await callback.message.edit_text(
            text + "\n\n🔍 Looking up content info...",
            parse_mode="Markdown",
        )

        # Look up content
        try:
            if session.content_type == ContentType.MOVIE:
                movies = await search_service.lookup_movie(session.query)
                if movies:
                    movie = movies[0]
                    session.selected_content = movie
                    await db.save_session(user_id, session)

                    movie_text = Formatters.format_movie_info(movie)
                    await callback.message.edit_text(
                        f"{text}\n\n---\n{movie_text}",
                        reply_markup=Keyboards.release_details(result, session.content_type),
                        parse_mode="Markdown",
                    )
                else:
                    await callback.message.edit_text(
                        f"{text}\n\n⚠️ Could not find movie info. Proceed anyway?",
                        reply_markup=Keyboards.release_details(result, session.content_type),
                        parse_mode="Markdown",
                    )
            else:
                series_list = await search_service.lookup_series(session.query)
                if series_list:
                    series = series_list[0]
                    session.selected_content = series
                    await db.save_session(user_id, session)

                    series_text = Formatters.format_series_info(series)
                    await callback.message.edit_text(
                        f"{text}\n\n---\n{series_text}",
                        reply_markup=Keyboards.release_details(result, session.content_type),
                        parse_mode="Markdown",
                    )
                else:
                    await callback.message.edit_text(
                        f"{text}\n\n⚠️ Could not find series info. Proceed anyway?",
                        reply_markup=Keyboards.release_details(result, session.content_type),
                        parse_mode="Markdown",
                    )
        except Exception as e:
            logger.warning("Failed to lookup content", error=str(e))
            await callback.message.edit_text(
                f"{text}\n\n⚠️ Could not load content info: {str(e)}",
                reply_markup=Keyboards.release_details(result, session.content_type),
                parse_mode="Markdown",
            )

        await callback.answer()
    finally:
        await db.close()
        await search_service.prowlarr.close()
        await search_service.radarr.close()
        await search_service.sonarr.close()


@router.callback_query(F.data == CallbackData.GRAB_BEST)
async def handle_grab_best(callback: CallbackQuery, db_user: User) -> None:
    """Handle 'Grab Best' button - grab the highest scored release."""
    if not callback.message:
        return

    db = await get_db()
    search_service, add_service, _ = get_services()

    try:
        user_id = callback.from_user.id
        session = await db.get_session(user_id)

        if not session or not session.results:
            await callback.answer("Session expired. Please start a new search.", show_alert=True)
            return

        result = session.results[0]  # Best result
        session.selected_result = result
        await db.save_session(user_id, session)

        await callback.answer("Grabbing best release...")
        await callback.message.edit_text("⏳ Grabbing best release...")

        # Lookup and grab
        await grab_release(callback.message, session, db_user, db, search_service, add_service)

    finally:
        await db.close()
        await search_service.prowlarr.close()
        await search_service.radarr.close()
        await search_service.sonarr.close()


@router.callback_query(F.data == CallbackData.CONFIRM_GRAB)
async def handle_confirm_grab(callback: CallbackQuery, db_user: User) -> None:
    """Handle grab confirmation."""
    if not callback.message:
        return

    db = await get_db()
    search_service, add_service, _ = get_services()

    try:
        user_id = callback.from_user.id
        session = await db.get_session(user_id)

        if not session or not session.selected_result:
            await callback.answer("Session expired. Please start a new search.", show_alert=True)
            return

        await callback.answer("Processing...")
        await callback.message.edit_text("⏳ Processing grab request...")

        await grab_release(callback.message, session, db_user, db, search_service, add_service)

    finally:
        await db.close()
        await search_service.prowlarr.close()
        await search_service.radarr.close()
        await search_service.sonarr.close()


async def grab_release(
    message: Message,
    session: SearchSession,
    db_user: User,
    db: Database,
    search_service: SearchService,
    add_service: AddService,
) -> None:
    """Perform the actual grab operation."""
    user_id = session.user_id
    result = session.selected_result
    prefs = db_user.preferences

    if not result:
        await message.edit_text(Formatters.format_error("No release selected"))
        return

    try:
        if session.content_type == ContentType.MOVIE:
            # Get or lookup movie
            movie = session.selected_content
            if not isinstance(movie, MovieInfo):
                movies = await search_service.lookup_movie(session.query)
                if not movies:
                    await message.edit_text(Formatters.format_error("Could not find movie in Radarr"))
                    return
                movie = movies[0]

            # Get quality profile and root folder
            profiles = await add_service.get_radarr_profiles()
            folders = await add_service.get_radarr_root_folders()

            if not profiles or not folders:
                await message.edit_text(Formatters.format_error("No quality profiles or root folders configured in Radarr"))
                return

            profile_id = prefs.radarr_quality_profile_id or profiles[0].id
            folder_path = None
            if prefs.radarr_root_folder_id:
                folder = next((f for f in folders if f.id == prefs.radarr_root_folder_id), None)
                folder_path = folder.path if folder else folders[0].path
            else:
                folder_path = folders[0].path

            # Grab
            success, action, msg = await add_service.grab_movie_release(
                movie=movie,
                release=result,
                quality_profile_id=profile_id,
                root_folder_path=folder_path,
            )

            action.user_id = user_id
            await db.log_action(action)

            if success:
                await message.edit_text(
                    Formatters.format_success(f"**{movie.title}** ({movie.year})\n\n{msg}\n\nRelease: _{result.title}_"),
                    parse_mode="Markdown",
                )
            else:
                await message.edit_text(Formatters.format_error(msg))

        else:
            # Series
            series = session.selected_content
            if not isinstance(series, SeriesInfo):
                series_list = await search_service.lookup_series(session.query)
                if not series_list:
                    await message.edit_text(Formatters.format_error("Could not find series in Sonarr"))
                    return
                series = series_list[0]

            # Get quality profile and root folder
            profiles = await add_service.get_sonarr_profiles()
            folders = await add_service.get_sonarr_root_folders()

            if not profiles or not folders:
                await message.edit_text(Formatters.format_error("No quality profiles or root folders configured in Sonarr"))
                return

            profile_id = prefs.sonarr_quality_profile_id or profiles[0].id
            folder_path = None
            if prefs.sonarr_root_folder_id:
                folder = next((f for f in folders if f.id == prefs.sonarr_root_folder_id), None)
                folder_path = folder.path if folder else folders[0].path
            else:
                folder_path = folders[0].path

            # Determine monitor type based on release
            monitor_type = "all"
            if result.detected_season is not None and not result.is_season_pack:
                if result.detected_episode is not None:
                    monitor_type = "none"  # Just this episode
                else:
                    monitor_type = "none"

            # Grab
            success, action, msg = await add_service.grab_series_release(
                series=series,
                release=result,
                quality_profile_id=profile_id,
                root_folder_path=folder_path,
                monitor_type=monitor_type,
            )

            action.user_id = user_id
            await db.log_action(action)

            if success:
                year_str = f" ({series.year})" if series.year else ""
                await message.edit_text(
                    Formatters.format_success(f"**{series.title}**{year_str}\n\n{msg}\n\nRelease: _{result.title}_"),
                    parse_mode="Markdown",
                )
            else:
                await message.edit_text(Formatters.format_error(msg))

        # Clean up session
        await db.delete_session(user_id)

    except Exception as e:
        logger.error("Grab failed", error=str(e))
        await message.edit_text(Formatters.format_error(str(e)))


@router.callback_query(F.data == CallbackData.BACK)
async def handle_back(callback: CallbackQuery, db_user: User) -> None:
    """Handle back button."""
    if not callback.message:
        return

    settings = get_settings()
    db = await get_db()

    try:
        user_id = callback.from_user.id
        session = await db.get_session(user_id)

        if not session or not session.results:
            await callback.answer("Session expired", show_alert=True)
            return

        # Clear selection and go back to results
        session.selected_result = None
        session.selected_content = None
        await db.save_session(user_id, session)

        # Show results page
        per_page = settings.results_per_page
        total_pages = (len(session.results) + per_page - 1) // per_page
        page = session.current_page

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
            parse_mode="Markdown",
        )

        await callback.answer()
    finally:
        await db.close()


@router.callback_query(F.data == CallbackData.CANCEL)
async def handle_cancel(callback: CallbackQuery) -> None:
    """Handle cancel button."""
    if not callback.message:
        return

    db = await get_db()

    try:
        user_id = callback.from_user.id
        await db.delete_session(user_id)

        await callback.message.edit_text("Operation cancelled. Send a new search query to start over.")
        await callback.answer()
    finally:
        await db.close()


@router.callback_query(F.data == "noop")
async def handle_noop(callback: CallbackQuery) -> None:
    """Handle no-op buttons (like page counter)."""
    await callback.answer()
