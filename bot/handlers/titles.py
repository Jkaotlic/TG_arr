"""Managing a library title: monitoring toggle and removal (2026-07-29).

Added after the incident where 102 unobtainable Paw Patrol episodes had to be
unmonitored with a hand-written GraphQL script. Anything the operator has to
drop to a script for is a missing button.

Rollback 2026-08-10 (Task 13): there is no single shared catalog any more —
Radarr and Sonarr each own their own library. `/title <query>` guarded-
searches both (`SearchService.lookup_movies`/`lookup_series` — the same
semaphore/circuit-breaker path detection uses; a raw `client.lookup_*` call
bypasses both, the exact bug Task 12 fixed once already) and keeps only the
matches that are already IN the library (`radarr_id`/`sonarr_id` set —
*arr's own lookup endpoint folds "already in the library" straight into its
TMDb/TVDB search results, so no separate "list the whole library" call is
needed).

Delete never touches files on disk (`delete_files=False`, matching the *arr
client defaults) — a catalog cleanup must never take the media with it.
"""

import html

import structlog
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot.clients.registry import get_radarr, get_sonarr
from bot.db import Database
from bot.handlers._cache import remember_lru
from bot.handlers.common import accessible_message, strip_command
from bot.models import ActionLog, ActionType, ContentType, MovieInfo, SeriesInfo, User
from bot.services.search_service import SearchService
from bot.ui.callbacks import TitleActionCB
from bot.ui.formatters import Formatters
from bot.ui.keyboards import Keyboards

logger = structlog.get_logger()
router = Router()

# Per-user cache of the titles /title's last search surfaced, keyed by the
# same "movie:<id>"/"series:<id>" string the title_actions/title_choices
# keyboards pack into TitleActionCB.title_id — same LRU discipline as
# trending.py/music.py's per-user candidate caches (LOGIC-21). Monitoring
# toggles/deletes act on the already-fetched object here instead of an extra
# *arr round-trip: Radarr/Sonarr have no cheap "get one by internal id" call
# among the interfaces this rollback carries forward.
_title_candidates: dict[int, dict[str, "MovieInfo | SeriesInfo"]] = {}
_MAX_TITLE_CANDIDATES = 100


async def _apply_monitor_toggle(client, resource_id: int, monitored: bool, is_movie: bool) -> bool:
    """Flip monitoring through the *arr client matching the title's content type."""
    if is_movie:
        return await client.set_movie_monitored(resource_id, monitored)
    return await client.set_series_monitored(resource_id, monitored)


async def _apply_delete(client, resource_id: int, is_movie: bool, delete_files: bool = False) -> bool:
    """Remove a library entry. `delete_files` defaults to False so a catalog
    cleanup can never take the user's media with it."""
    if is_movie:
        return await client.delete_movie(resource_id, delete_files=delete_files)
    return await client.delete_series(resource_id, delete_files=delete_files)


def _title_key(title: "MovieInfo | SeriesInfo") -> str:
    """`movie:<radarr_id>` / `series:<sonarr_id>` — mirrors
    `Keyboards._title_action_id`, which builds the same string for the
    buttons this cache key is looked up by."""
    if isinstance(title, MovieInfo):
        return f"movie:{title.radarr_id}"
    return f"series:{title.sonarr_id}"


def _remember_candidates(user_id: int, titles: list) -> None:
    cache: dict[str, "MovieInfo | SeriesInfo"] = {_title_key(t): t for t in titles}
    remember_lru(_title_candidates, user_id, cache, _MAX_TITLE_CANDIDATES)


@router.message(Command("title"))
async def cmd_title(message: Message, db_user: User, db: Database) -> None:
    """`/title <name>` — find a library entry and offer actions on it."""
    if not message.text:
        await message.answer("Укажите название: <code>/title Paw Patrol</code>")
        return

    query = strip_command(message.text, "/title")
    if not query:
        await message.answer("Укажите название: <code>/title Paw Patrol</code>")
        return

    status_msg = await message.answer("🔍 Ищу в библиотеке...")
    search_service = SearchService(await get_radarr(), await get_sonarr())

    try:
        movies = await search_service.lookup_movies(query)
        series_list = await search_service.lookup_series(query)
    except Exception as e:
        logger.error("title_lookup_failed", error=str(e), exc_info=True)
        await status_msg.edit_text(Formatters.format_error("Не удалось получить библиотеку"))
        return

    items: list = [m for m in movies if m.radarr_id]
    items += [s for s in series_list if s.sonarr_id]

    if not items:
        await status_msg.edit_text(
            Formatters.format_warning(f"В библиотеке нет: <b>{html.escape(query)}</b>"),
            parse_mode="HTML",
        )
        return

    user_id = message.from_user.id if message.from_user else 0
    _remember_candidates(user_id, items)

    if len(items) > 1:
        # More than one entry matches. Do not guess — the card below this offers
        # deletion, and acting on the wrong title is not recoverable from the
        # user's side (audit 2026-07-30, BUG-05).
        await status_msg.edit_text(
            f"🗂 Нашёл несколько совпадений на <b>{html.escape(query)}</b> — какой нужен?",
            reply_markup=Keyboards.title_choices(items),
            parse_mode="HTML",
        )
        return

    title = items[0]
    await status_msg.edit_text(
        _render_title_card(title),
        reply_markup=Keyboards.title_actions(title),
        parse_mode="HTML",
    )


def _render_title_card(title: "MovieInfo | SeriesInfo") -> str:
    """One card describing what the actions below will act on."""
    year = f" ({title.year})" if getattr(title, "year", None) else ""
    lines = [f"🗂 <b>{html.escape(title.title)}</b>{year}", ""]
    lines.append(f"👀 Мониторинг: {'включён' if title.monitored else 'выключен'}")
    if isinstance(title, SeriesInfo):
        if title.episodes_total:
            lines.append(f"📥 Серий: {title.episodes_owned}/{title.episodes_total}")
    elif title.has_file:
        lines.append("📥 Файл: есть")
    else:
        lines.append("📥 Файл: нет")
    return "\n".join(lines)


@router.callback_query(TitleActionCB.filter())
async def handle_title_action(
    callback: CallbackQuery, callback_data: TitleActionCB, db_user: User, db: Database
) -> None:
    """Apply a monitoring/removal action to a title already in the library."""
    message = accessible_message(callback)
    if message is None:
        return

    action = callback_data.action
    is_movie = callback_data.kind == "movie"
    # Internal cache key only ("movie:15") — never packed into callback_data,
    # so the ':' here doesn't hit aiogram's own field-separator constraint
    # (see TitleActionCB's docstring for why `kind`/`title_id` are separate
    # fields on the callback itself).
    title_id = f"{callback_data.kind}:{callback_data.title_id}"

    try:
        resource_id = int(callback_data.title_id)
    except ValueError:
        await callback.answer("Неверный идентификатор", show_alert=True)
        return

    user_id = callback.from_user.id
    client = await get_radarr() if is_movie else await get_sonarr()
    await callback.answer()

    cache = _title_candidates.get(user_id, {})

    try:
        if action == "pick":
            # The user chose which of several matches they meant; now the card.
            title = cache.get(title_id)
            if title is None:
                await message.edit_text(
                    Formatters.format_warning("Тайтл больше не найден — повторите /title")
                )
                return
            await message.edit_text(
                _render_title_card(title),
                reply_markup=Keyboards.title_actions(title),
                parse_mode="HTML",
            )
            return

        if action in ("mon", "unmon"):
            monitored = action == "mon"
            ok = await _apply_monitor_toggle(client, resource_id, monitored, is_movie)
            title = cache.get(title_id)
            if title is not None and ok:
                title = title.model_copy(update={"monitored": monitored})
                cache[title_id] = title
            await db.log_action(ActionLog(
                user_id=db_user.tg_id,
                action_type=ActionType.MONITOR,
                content_type=ContentType.MOVIE if is_movie else ContentType.SERIES,
                content_title=getattr(title, "title", None),
                content_id=str(resource_id),
                details=f"monitored={monitored}",
            ))
            if title is not None:
                await message.edit_text(
                    _render_title_card(title),
                    reply_markup=Keyboards.title_actions(title),
                    parse_mode="HTML",
                )
            else:
                await message.edit_text(
                    Formatters.format_success(
                        "Мониторинг включён" if monitored else "Мониторинг снят"
                    )
                )
            return

        if action == "delete":
            # Destructive: show what it touches and require a second tap.
            title = cache.get(title_id)
            label = html.escape(getattr(title, "title", None) or "тайтл")
            if isinstance(title, MovieInfo):
                warning = (
                    "\n\n⚠️ Файл на диске <b>останется</b> — удаляется только запись в Radarr."
                    if title.has_file
                    else "\n\nФайла на диске нет."
                )
            elif isinstance(title, SeriesInfo) and title.episodes_owned:
                warning = (
                    f"\n\n⚠️ Файлов на диске: <b>{title.episodes_owned}</b> — они "
                    "<b>останутся</b>, удаляется только запись в Sonarr."
                )
            else:
                warning = "\n\nФайлов на диске нет."
            await message.edit_text(
                f"🗑 Удалить <b>{label}</b> из библиотеки?{warning}",
                reply_markup=Keyboards.confirm_title_delete(callback_data.kind, callback_data.title_id),
                parse_mode="HTML",
            )
            return

        if action == "delconf":
            title = cache.get(title_id)
            await _apply_delete(client, resource_id, is_movie, delete_files=False)
            await db.log_action(ActionLog(
                user_id=db_user.tg_id,
                action_type=ActionType.DELETE,
                content_type=ContentType.MOVIE if is_movie else ContentType.SERIES,
                content_title=getattr(title, "title", None),
                content_id=str(resource_id),
                details="deleted",
            ))
            cache.pop(title_id, None)
            await message.edit_text(
                Formatters.format_success("Удалено из библиотеки. Файлы на диске не тронуты.")
            )
            return

    except Exception as e:
        logger.error("title_action_failed", action=action, title_id=title_id, error=str(e), exc_info=True)
        await message.edit_text(
            Formatters.format_error(f"Не удалось выполнить действие: {html.escape(str(e))[:150]}")
        )
