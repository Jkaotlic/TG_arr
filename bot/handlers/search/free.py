"""Free-text release search — for titles Radarr/Sonarr's catalogue does not know.

The main flow answers "add this to my library": it resolves a metadata
candidate, adds it to *arr, and lists releases through *arr's own interactive
search, which carries *arr's verdict. It has nothing to say about a title
TMDb/TVDB never heard of — a concert, a rare rip, anything the metadata
providers do not carry. That gap is why `ProwlarrClient.search()` sat without a
caller and why the push chain in `AddService` was unreachable.

This flow asks Prowlarr directly. There is no library entry, so there is no
verdict and no import: the grab goes down the push chain and, when *arr refuses
a release it cannot map onto anything, lands in qBittorrent. Every message here
says that plainly rather than implying the file will show up in Emby.
"""

import html

import structlog
from aiogram import F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot.config import get_settings
from bot.db import Database
from bot.handlers.common import accessible_message, safe_edit, strip_command
from bot.models import ActionLog, ActionType, ContentType, SearchSession, User
from bot.services.search_service import describe_search_failure
from bot.ui.callbacks import FindGrabCB, FindPageCB, FindReleaseCB
from bot.ui.formatters import Formatters
from bot.ui.keyboards import CallbackData, Keyboards

from bot.handlers import search as _search
from .services import MAX_QUERY_LENGTH, router

logger = structlog.get_logger()

#: Повторяется на карточке релиза и в сообщении об успехе. «Скачалось, а в Emby
#: нет» без этой строки читается как поломка бота.
_NOT_IN_LIBRARY_NOTE = (
    "⚠️ <i>Тайтла нет в библиотеке: файл появится в загрузках, "
    "но Radarr/Sonarr его не импортируют.</i>"
)

_SEARCHING = "🔎 Ищу раздачи напрямую в Prowlarr..."


def _is_free_session(session) -> bool:
    """Сессия свободного поиска с непустым списком результатов.

    Каталожная сессия сюда попасть не должна: её грабом занимается
    `bot/handlers/search/grab.py`, который умеет arr_id.
    """
    return bool(session and session.free_search and session.results)


async def _render_page(message: Message, session: SearchSession, page: int) -> None:
    """Render one page of free-search hits into `message` (edit in place)."""
    per_page = get_settings().results_per_page
    total_pages = max(1, (len(session.results) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    chunk = session.results[page * per_page:(page + 1) * per_page]

    text = Formatters.format_search_results_page(
        chunk, page, total_pages, session.query, session.content_type, per_page=per_page,
    )
    await safe_edit(
        message,
        f"{text}\n\n🔎 <i>Свободный поиск — напрямую в Prowlarr, мимо каталога.</i>",
        reply_markup=Keyboards.free_results(chunk, page, total_pages, offset=page * per_page),
        parse_mode="HTML",
    )


async def run_free_search(
    status_msg: Message, query: str, db_user: User, db: Database,
) -> None:
    """Search Prowlarr for `query` and render the first page into `status_msg`."""
    search_service, _add_service = await _search.get_services()
    log = logger.bind(user_id=db_user.tg_id, query=query)

    try:
        results = await search_service.search_free_text(
            query, preferred_resolution=db_user.preferences.preferred_resolution,
        )
    except Exception as e:
        log.error("free_search_failed", error=str(e), exc_info=True)
        await status_msg.edit_text(
            Formatters.format_error(html.escape(describe_search_failure(e)))
        )
        return

    log.info("free_search_rendered", result_count=len(results))

    if not results:
        await status_msg.edit_text(
            Formatters.format_warning(
                f"Ничего не найдено по запросу <b>{html.escape(query)}</b> "
                "даже напрямую в индексерах."
            ),
            parse_mode="HTML",
        )
        return

    session = SearchSession(
        user_id=db_user.tg_id,
        query=query,
        content_type=ContentType.UNKNOWN,
        results=results,
        free_search=True,
    )
    await db.save_session(db_user.tg_id, session)
    await db.log_action(ActionLog(
        user_id=db_user.tg_id,
        action_type=ActionType.SEARCH,
        content_type=ContentType.UNKNOWN,
        query=query,
    ))
    await _render_page(status_msg, session, 0)


@router.message(Command("find"))
async def cmd_find(message: Message, db_user: User, db: Database) -> None:
    """Handle /find <query> — ask the indexers directly, skip the catalogue."""
    if not message.text:
        return

    query = strip_command(message.text, "/find")
    if len(query) < 2:
        await message.answer(
            "Укажите, что искать: <code>/find Дюна 2021</code>\n\n"
            "Свободный поиск идёт напрямую в Prowlarr — для того, чего нет в каталоге."
        )
        return
    if len(query) > MAX_QUERY_LENGTH:
        await message.answer(f"❌ Запрос слишком длинный (макс. {MAX_QUERY_LENGTH} символов)")
        return

    status_msg = await message.answer(_SEARCHING)
    await run_free_search(status_msg, query, db_user, db)


@router.callback_query(F.data == CallbackData.FREE_SEARCH)
async def handle_free_search_button(
    callback: CallbackQuery, db_user: User, db: Database,
) -> None:
    """The offer shown when the catalogue-backed search hit a dead end.

    The query is read from the session `process_search` saved alongside the
    dead-end message, so the user does not retype what they already typed.
    """
    message = accessible_message(callback)
    if message is None:
        return

    session = await db.get_session(callback.from_user.id)
    if not session or not session.query:
        await callback.answer("Сессия истекла. Начните новый поиск.", show_alert=True)
        return

    await callback.answer()
    await message.edit_text(_SEARCHING)
    await run_free_search(message, session.query, db_user, db)


@router.callback_query(FindPageCB.filter())
async def handle_find_page(
    callback: CallbackQuery, callback_data: FindPageCB, db_user: User, db: Database,
) -> None:
    """Flip between free-search result pages."""
    message = accessible_message(callback)
    if message is None:
        return

    session = await db.get_session(callback.from_user.id)
    if not _is_free_session(session):
        await callback.answer("Результаты устарели — повторите поиск", show_alert=True)
        return

    await callback.answer()
    await _render_page(message, session, callback_data.page)


@router.callback_query(FindReleaseCB.filter())
async def handle_find_release(
    callback: CallbackQuery, callback_data: FindReleaseCB, db_user: User, db: Database,
) -> None:
    """Open the card of one free-search hit."""
    message = accessible_message(callback)
    if message is None:
        return

    session = await db.get_session(callback.from_user.id)
    if not _is_free_session(session) or callback_data.idx >= len(session.results):
        await callback.answer("Результаты устарели — повторите поиск", show_alert=True)
        return

    release = session.results[callback_data.idx]
    await callback.answer()
    await safe_edit(
        message,
        f"{Formatters.format_release_details(release)}\n\n{_NOT_IN_LIBRARY_NOTE}",
        reply_markup=Keyboards.free_release(callback_data.idx),
        parse_mode="HTML",
    )


@router.callback_query(FindGrabCB.filter())
async def handle_find_grab(
    callback: CallbackQuery, callback_data: FindGrabCB, db_user: User, db: Database,
) -> None:
    """Grab one free-search hit.

    No `arr_id`: there is no library entry. `AddService.grab_release` then runs
    the push chain and skips its auto-search step, which needs an id there is
    none of.
    """
    message = accessible_message(callback)
    if message is None:
        return

    user_id = callback.from_user.id
    session = await db.get_session(user_id)
    if not _is_free_session(session) or callback_data.idx >= len(session.results):
        await callback.answer("Результаты устарели — повторите поиск", show_alert=True)
        return

    release = session.results[callback_data.idx]

    # RACE-01: same single-grab-per-user guard the catalogue flow uses.
    if not await _search._claim_grab(user_id):
        await callback.answer("⏳ Уже обрабатываю предыдущий запрос…")
        return
    try:
        await callback.answer("Скачиваю...")
        await message.edit_text("⏳ Отправляю раздачу...")

        _search_service, add_service = await _search.get_services()
        success, action = await add_service.grab_release(release, session.content_type)
        action.user_id = user_id
        await db.log_action(action)

        if success:
            await message.edit_text(
                Formatters.format_success(
                    f"Релиз: <i>{html.escape(release.title)}</i>\n\n"
                    "Тайтла нет в библиотеке — файл появится в загрузках, "
                    "но Radarr/Sonarr его не импортируют."
                ),
                parse_mode="HTML",
            )
        else:
            await message.edit_text(
                Formatters.format_error(
                    html.escape(action.error_message or "Не удалось скачать релиз")
                )
            )

        await db.delete_session(user_id)
    finally:
        _search._release_grab(user_id)
