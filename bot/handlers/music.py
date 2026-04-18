"""Music (Lidarr) handlers: search artists, view details, add to Lidarr."""

import html

import structlog
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot.clients.registry import get_deezer, get_lidarr, get_prowlarr, get_qbittorrent, get_radarr, get_sonarr
from bot.config import get_settings
from bot.db import Database
from bot.models import (
    ActionLog,
    ActionType,
    ArtistInfo,
    ContentType,
    SearchSession,
    User,
)
from bot.services.add_service import AddService
from bot.services.scoring import ScoringService
from bot.services.search_service import SearchService
from bot.ui.formatters import Formatters
from bot.ui.keyboards import CallbackData, Keyboards

logger = structlog.get_logger()
router = Router()

MENU_MUSIC = "🎵 Музыка"

# Session key: store artist candidates list separately from SearchResult list
# We reuse SearchSession.results (as search releases) and selected_content (ArtistInfo)
# Per-user in-memory artist lookup cache (for select-by-index callbacks)
_artist_candidates: dict[int, list[ArtistInfo]] = {}
_trending_artists_cache: dict[int, list[dict]] = {}


async def _get_music_services() -> tuple[SearchService, AddService] | None:
    """Build SearchService and AddService wired to Lidarr. None if Lidarr is not configured."""
    lidarr = await get_lidarr()
    if lidarr is None:
        return None

    prowlarr = await get_prowlarr()
    radarr = await get_radarr()
    sonarr = await get_sonarr()
    qbittorrent = await get_qbittorrent()

    search_service = SearchService(prowlarr, radarr, sonarr, ScoringService(), lidarr=lidarr)
    add_service = AddService(prowlarr, radarr, sonarr, qbittorrent=qbittorrent, lidarr=lidarr)
    return search_service, add_service


@router.message(Command("music"))
async def cmd_music(message: Message, db_user: User, db: Database) -> None:
    """Handle /music <artist> command."""
    if not message.text:
        await message.answer("Укажите артиста: <code>/music Metallica</code>")
        return

    query = message.text.replace("/music", "", 1).strip()
    if not query:
        await message.answer("Укажите артиста: <code>/music Metallica</code>")
        return

    await process_music_search(message, query, db_user, db)


@router.message(F.text == MENU_MUSIC)
async def handle_menu_music(message: Message) -> None:
    """Handle 🎵 Музыка menu button — prompt user for query."""
    settings = get_settings()
    if not settings.lidarr_enabled:
        await message.answer("Lidarr не настроен. Добавьте LIDARR_URL и LIDARR_API_KEY в .env")
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
        log.error("Artist lookup failed", error=str(e))
        await status_msg.edit_text(Formatters.format_error("Не удалось найти артистов (Lidarr недоступен?)"))
        return

    if not artists:
        await status_msg.edit_text(
            Formatters.format_warning(f"Артист не найден: <b>{html.escape(query)}</b>"),
            parse_mode="HTML",
        )
        return

    # Cache for callback lookup by index
    _artist_candidates[user_id] = artists[:25]

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

    lines = [f"🎵 <b>Найдено артистов: {len(artists)}</b>\n"]
    for i, a in enumerate(_artist_candidates[user_id]):
        disamb = f" <i>[{html.escape(a.disambiguation)}]</i>" if a.disambiguation else ""
        in_lib = " ✅" if a.lidarr_id else ""
        lines.append(f"{i + 1}. <b>{html.escape(a.name)}</b>{disamb}{in_lib}")

    await status_msg.edit_text(
        "\n".join(lines),
        reply_markup=Keyboards.artist_list(_artist_candidates[user_id]),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith(CallbackData.ARTIST))
async def handle_artist_selection(callback: CallbackQuery, db_user: User, db: Database) -> None:
    """Handle artist selection from lookup results."""
    if not callback.data or not callback.message:
        return

    user_id = callback.from_user.id

    try:
        idx = int(callback.data.removeprefix(CallbackData.ARTIST))
    except ValueError:
        await callback.answer("Неверный выбор", show_alert=True)
        return

    artists = _artist_candidates.get(user_id) or []
    if idx < 0 or idx >= len(artists):
        await callback.answer("Выбор истёк. Начните новый поиск.", show_alert=True)
        return

    artist = artists[idx]

    session = await db.get_session(user_id)
    if session is None:
        session = SearchSession(user_id=user_id, query=artist.name, content_type=ContentType.MUSIC)
    session.selected_content = artist
    await db.save_session(user_id, session)

    await callback.answer()
    await callback.message.edit_text(
        Formatters.format_artist_info(artist),
        reply_markup=Keyboards.artist_details(artist, already_in_library=bool(artist.lidarr_id)),
        parse_mode="HTML",
    )


async def _handle_confirm_music_add(callback: CallbackQuery, db_user: User, db: Database) -> None:
    """Confirm add artist to Lidarr."""
    user_id = db_user.tg_id
    session = await db.get_session(user_id)
    if not session or not isinstance(session.selected_content, ArtistInfo):
        return  # Not a music flow — fall through to other CONFIRM_GRAB handlers

    services = await _get_music_services()
    if services is None:
        await callback.answer("Lidarr не настроен", show_alert=True)
        return
    _search_service, add_service = services

    artist: ArtistInfo = session.selected_content
    prefs = db_user.preferences

    await callback.answer("Добавляю...")
    if callback.message:
        await callback.message.edit_text(f"⏳ Добавляю <b>{html.escape(artist.name)}</b> в Lidarr...", parse_mode="HTML")

    try:
        profiles = await add_service.get_lidarr_profiles()
        metadata_profiles = await add_service.get_lidarr_metadata_profiles()
        folders = await add_service.get_lidarr_root_folders()

        if not profiles or not folders or not metadata_profiles:
            if callback.message:
                await callback.message.edit_text(
                    Formatters.format_error("Нет профилей качества / папок / metadata-профилей в Lidarr"),
                )
            return

        profile_id = prefs.lidarr_quality_profile_id or profiles[0].id
        metadata_profile_id = prefs.lidarr_metadata_profile_id or metadata_profiles[0].id
        folder = next((f for f in folders if f.id == prefs.lidarr_root_folder_id), None) or folders[0]

        added, action = await add_service.add_artist(
            artist=artist,
            quality_profile_id=profile_id,
            metadata_profile_id=metadata_profile_id,
            root_folder_path=folder.path,
            monitor="all",
            search_for_missing=True,
        )
        action.user_id = user_id
        await db.log_action(action)

        if callback.message:
            if added:
                await callback.message.edit_text(
                    Formatters.format_success(
                        f"<b>{html.escape(added.name)}</b>\n\n"
                        f"Добавлен в Lidarr. Запущен автопоиск по всем альбомам."
                    ),
                    parse_mode="HTML",
                )
            else:
                await callback.message.edit_text(
                    Formatters.format_error(action.error_message or "Не удалось добавить артиста"),
                )

        await db.delete_session(user_id)
        _artist_candidates.pop(user_id, None)
    except Exception as e:
        logger.error("Add artist failed", error=str(e))
        if callback.message:
            await callback.message.edit_text(Formatters.format_error("Операция временно недоступна"))


# Register as secondary listener for CONFIRM_GRAB — music flow only triggers when selected_content is ArtistInfo
@router.callback_query(F.data == CallbackData.CONFIRM_GRAB)
async def handle_music_confirm(callback: CallbackQuery, db_user: User, db: Database) -> None:
    """Route CONFIRM_GRAB to music add when the current session holds an ArtistInfo."""
    user_id = db_user.tg_id
    session = await db.get_session(user_id)
    if session and isinstance(session.selected_content, ArtistInfo):
        await _handle_confirm_music_add(callback, db_user, db)


# =========================================================================
# Trending music (Deezer)
# =========================================================================


@router.callback_query(F.data == CallbackData.TRENDING_MUSIC)
async def handle_trending_music(callback: CallbackQuery) -> None:
    """Show trending artists from Deezer."""
    if not callback.message:
        return

    await callback.answer("🔍 Загружаю...")

    deezer = await get_deezer()
    if deezer is None:
        await callback.message.edit_text(Formatters.format_error("Deezer отключён"))
        return

    try:
        artists = await deezer.get_trending_artists(limit=10)
    except Exception as e:
        logger.error("Deezer trending failed", error=str(e))
        await callback.message.edit_text(Formatters.format_error("Не удалось загрузить трендовых артистов"))
        return

    if not artists:
        await callback.message.edit_text(Formatters.format_warning("Нет данных от Deezer"))
        return

    user_id = callback.from_user.id
    _trending_artists_cache[user_id] = artists

    await callback.message.edit_text(
        Formatters.format_trending_artists(artists),
        reply_markup=Keyboards.trending_artists(artists),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith(CallbackData.TRENDING_ARTIST))
async def handle_trending_artist_click(
    callback: CallbackQuery, db_user: User, db: Database,
) -> None:
    """Click a trending artist → lookup in Lidarr and show details."""
    if not callback.data or not callback.message:
        return

    try:
        idx = int(callback.data.removeprefix(CallbackData.TRENDING_ARTIST))
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
    if callback.message:
        await callback.message.edit_text(f"🔍 Ищу <b>{html.escape(name)}</b> в Lidarr...", parse_mode="HTML")
    await process_music_search(callback.message, name, db_user, db)
