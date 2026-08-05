"""TorrServer section — the "watch it now" contour.

Kept apart from the Scryer search flow on purpose: Scryer answers "I want to
own this", TorrServer answers "I want to watch this tonight". They share no
state.
"""

import html

import structlog
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, ForceReply, InlineKeyboardMarkup, Message

from bot.clients.registry import get_torrserver, get_torrserver_service
from bot.clients.torrserver import TorrServerError
from bot.config import get_settings
from bot.handlers._cache import remember_lru
from bot.handlers.common import accessible_message, safe_edit
from bot.models import TorrServerRelease
from bot.ui.callbacks import TsAddCB, TsPageCB, TsReleaseCB
from bot.ui.formatters import Formatters
from bot.ui.keyboards import CallbackData, Keyboards
from bot.ui.menu import MENU_TORRSERVER, TORRSERVER_PROMPT

logger = structlog.get_logger()
router = Router(name="torrserver")

MAX_QUERY_LENGTH = 100
#: One torznab answer is ~470 KB; nobody scrolls past the top hits anyway.
SEARCH_LIMIT = 30

#: Per-user search hits. They are far too large for callback_data, so buttons
#: carry only an index into this list — the same trick the Soulseek flow uses.
_results: dict[int, list[TorrServerRelease]] = {}

#: Cap on remembered result sets, evicting the oldest — never clear() the whole
#: dict, that would wipe every other user's in-flight selection.
_MAX_CACHED_USERS = 50

_NOT_CONFIGURED = (
    "❌ TorrServer не настроен. Добавьте <code>TORRSERVER_URL</code>, "
    "<code>TORRSERVER_USERNAME</code> и <code>TORRSERVER_PASSWORD</code> в конфигурацию."
)


async def render_panel() -> tuple[str, InlineKeyboardMarkup | None]:
    """Build the panel card without touching Telegram, so both the command and
    the refresh button can decide themselves how to deliver it."""
    client = await get_torrserver()
    if not client:
        return _NOT_CONFIGURED, None

    try:
        stats = await client.get_stats()
    except TorrServerError as e:
        return Formatters.format_error(str(e)), Keyboards.torrserver_panel()
    except Exception as e:
        logger.error("torrserver_panel_failed", error=str(e), exc_info=True)
        return Formatters.format_error("Не удалось получить статус TorrServer"), Keyboards.torrserver_panel()

    return Formatters.format_torrserver_status(stats), Keyboards.torrserver_panel()


@router.message(F.text == MENU_TORRSERVER)
@router.message(Command("ts"))
async def cmd_torrserver(message: Message) -> None:
    """Open the TorrServer panel."""
    text, keyboard = await render_panel()
    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data == CallbackData.TS_REFRESH)
@router.callback_query(F.data == CallbackData.TS_BACK)
async def handle_panel_refresh(callback: CallbackQuery) -> None:
    """Re-render the panel in place (exactly one callback.answer())."""
    text, keyboard = await render_panel()
    if (message := accessible_message(callback)) is not None:
        await safe_edit(message, text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == CallbackData.TS_CLOSE)
async def handle_close(callback: CallbackQuery) -> None:
    """Close the panel message."""
    if (message := accessible_message(callback)) is not None:
        await message.delete()
    await callback.answer()


@router.callback_query(F.data == CallbackData.TS_SEARCH)
async def handle_search_prompt(callback: CallbackQuery) -> None:
    """Ask for a query with ForceReply.

    No state is stored: the reply carries the prompt with it, so a user who
    changes their mind and types something else simply gets the normal Scryer
    search instead of a stale "waiting for TorrServer query" flag.
    """
    if (message := accessible_message(callback)) is not None:
        await message.answer(
            TORRSERVER_PROMPT,
            reply_markup=ForceReply(input_field_placeholder="Название раздачи"),
        )
    await callback.answer()


async def _render_results(message: Message, user_id: int, page: int) -> None:
    """Render one page of cached hits into `message` (edit in place)."""
    releases = _results.get(user_id) or []
    per_page = get_settings().results_per_page
    total_pages = max(1, (len(releases) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    chunk = releases[page * per_page:(page + 1) * per_page]

    await safe_edit(
        message,
        Formatters.format_torrserver_results(chunk, page, per_page, len(releases)),
        reply_markup=Keyboards.torrserver_results(
            chunk, page, total_pages, offset=page * per_page,
        ),
        parse_mode="HTML",
    )


@router.message(F.reply_to_message.text == TORRSERVER_PROMPT)
async def handle_search_reply(message: Message) -> None:
    """Search TorrServer for the text the user replied with."""
    query = (message.text or "").strip()
    if len(query) < 2:
        await message.answer("❌ Запрос слишком короткий (мин. 2 символа)")
        return
    if len(query) > MAX_QUERY_LENGTH:
        await message.answer(f"❌ Запрос слишком длинный (макс. {MAX_QUERY_LENGTH} символов)")
        return

    client = await get_torrserver()
    if not client:
        await message.answer(_NOT_CONFIGURED, parse_mode="HTML")
        return

    status_msg = await message.answer("🔎 Ищу раздачи в TorrServer...")
    try:
        releases = await client.search(query, limit=SEARCH_LIMIT)
    except TorrServerError as e:
        await status_msg.edit_text(Formatters.format_error(html.escape(str(e))))
        return
    except Exception as e:
        logger.error("torrserver_search_failed", error=str(e), exc_info=True)
        await status_msg.edit_text(Formatters.format_error("Поиск временно недоступен"))
        return

    logger.info("torrserver_search", query=query, results=len(releases))

    if not releases:
        await status_msg.edit_text(
            Formatters.format_warning(f"Ничего не найдено для <b>{html.escape(query)}</b>"),
            reply_markup=Keyboards.torrserver_panel(),
            parse_mode="HTML",
        )
        return

    user_id = message.from_user.id
    remember_lru(_results, user_id, releases, _MAX_CACHED_USERS)
    await _render_results(status_msg, user_id, 0)


@router.callback_query(TsPageCB.filter())
async def handle_page(callback: CallbackQuery, callback_data: TsPageCB) -> None:
    """Flip between result pages."""
    message = accessible_message(callback)
    if message is None:
        return
    if not _results.get(callback.from_user.id):
        await callback.answer("Результаты устарели — повторите поиск", show_alert=True)
        return
    await _render_results(message, callback.from_user.id, callback_data.page)
    await callback.answer()


@router.callback_query(TsReleaseCB.filter())
async def handle_release(callback: CallbackQuery, callback_data: TsReleaseCB) -> None:
    """Open the card of one hit."""
    message = accessible_message(callback)
    if message is None:
        return
    releases = _results.get(callback.from_user.id) or []
    # The button index is absolute (see Keyboards.torrserver_results' `offset`),
    # so it can be used directly against the cached list without knowing the
    # page it came from.
    absolute = callback_data.idx
    if absolute >= len(releases):
        await callback.answer("Результаты устарели — повторите поиск", show_alert=True)
        return

    release = releases[absolute]
    await safe_edit(
        message,
        Formatters.format_torrserver_release(release),
        reply_markup=Keyboards.torrserver_release(absolute),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(TsAddCB.filter())
async def handle_add(callback: CallbackQuery, callback_data: TsAddCB) -> None:
    """Add the chosen release and publish it to Emby."""
    message = accessible_message(callback)
    if message is None:
        return

    releases = _results.get(callback.from_user.id) or []
    if callback_data.idx >= len(releases):
        await callback.answer("Результаты устарели — повторите поиск", show_alert=True)
        return
    release = releases[callback_data.idx]

    service = await get_torrserver_service()
    if service is None:
        await callback.answer("TorrServer не настроен", show_alert=True)
        return

    await callback.answer("Добавляю...")
    await message.edit_text("⏳ Добавляю раздачу и жду метаданные...")

    try:
        result = await service.add_and_publish(release.link, release.title, "")
    except TorrServerError as e:
        await message.edit_text(Formatters.format_error(html.escape(str(e))))
        return
    except Exception as e:
        logger.error("torrserver_add_failed", error=str(e), exc_info=True)
        await message.edit_text(Formatters.format_error("Не удалось добавить раздачу"))
        return

    await message.edit_text(
        Formatters.format_torrserver_added(result),
        reply_markup=Keyboards.torrserver_panel(),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
