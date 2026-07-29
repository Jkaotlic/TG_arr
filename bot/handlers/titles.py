"""Managing a catalog title: monitoring toggle and removal (2026-07-29).

Added after the incident where 102 unobtainable Paw Patrol episodes had to be
unmonitored with a hand-written GraphQL script. Anything the operator has to
drop to a script for is a missing button.
"""

import html

import structlog
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot.clients.registry import get_scryer
from bot.db import Database
from bot.handlers.common import accessible_message, strip_command
from bot.models import ActionLog, ActionType, ContentType, User
from bot.ui.callbacks import TitleActionCB
from bot.ui.formatters import Formatters
from bot.ui.keyboards import Keyboards

logger = structlog.get_logger()
router = Router()


@router.message(Command("title"))
async def cmd_title(message: Message, db_user: User, db: Database) -> None:
    """`/title <name>` — find a catalog entry and offer actions on it."""
    if not message.text:
        await message.answer("Укажите название: <code>/title Paw Patrol</code>")
        return

    query = strip_command(message.text, "/title")
    if not query:
        await message.answer("Укажите название: <code>/title Paw Patrol</code>")
        return

    status_msg = await message.answer("🔍 Ищу в каталоге...")
    scryer = await get_scryer()

    try:
        items, _total, _more = await scryer.get_titles(query=query, limit=5)
    except Exception as e:
        logger.error("title_lookup_failed", error=str(e), exc_info=True)
        await status_msg.edit_text(Formatters.format_error("Не удалось получить каталог"))
        return

    if not items:
        await status_msg.edit_text(
            Formatters.format_warning(f"В каталоге нет: <b>{html.escape(query)}</b>"),
            parse_mode="HTML",
        )
        return

    title = items[0]
    await status_msg.edit_text(
        _render_title_card(title),
        reply_markup=Keyboards.title_actions(title),
        parse_mode="HTML",
    )


def _render_title_card(title) -> str:
    """One card describing what the actions below will act on."""
    year = f" ({title.year})" if getattr(title, "year", None) else ""
    lines = [f"🗂 <b>{html.escape(title.title)}</b>{year}", ""]
    lines.append(f"👀 Мониторинг: {'включён' if title.monitored else 'выключен'}")
    if getattr(title, "episodes_total", 0):
        lines.append(f"📥 Серий: {title.episodes_owned}/{title.episodes_total}")
    if getattr(title, "quality_tier", None):
        lines.append(f"🎚 Профиль: {html.escape(title.quality_tier)}")
    return "\n".join(lines)


@router.callback_query(TitleActionCB.filter())
async def handle_title_action(
    callback: CallbackQuery, callback_data: TitleActionCB, db_user: User, db: Database
) -> None:
    """Apply a monitoring/removal action to a catalog title."""
    message = accessible_message(callback)
    if message is None:
        return

    action = callback_data.action
    title_id = callback_data.title_id
    scryer = await get_scryer()
    await callback.answer()

    try:
        if action in ("mon", "unmon"):
            monitored = action == "mon"
            await scryer.set_title_monitored(title_id, monitored)
            title = await scryer.get_title(title_id)
            await db.log_action(ActionLog(
                user_id=db_user.tg_id,
                action_type=ActionType.ADD,
                content_type=getattr(title, "content_type", ContentType.UNKNOWN)
                if title else ContentType.UNKNOWN,
                content_title=getattr(title, "title", None),
                content_id=title_id,
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
            preview = await scryer.delete_title_preview(title_id)
            files = preview.get("totalFileCount", 0)
            label = html.escape(str(preview.get("targetLabel") or "тайтл"))
            warning = (
                f"\n\n⚠️ На диске файлов: <b>{files}</b> — они <b>останутся</b>, "
                "удаляется только запись в каталоге."
                if files
                else "\n\nФайлов на диске нет."
            )
            await message.edit_text(
                f"🗑 Удалить <b>{label}</b> из каталога Scryer?{warning}",
                reply_markup=Keyboards.confirm_title_delete(title_id),
                parse_mode="HTML",
            )
            return

        if action == "delconf":
            preview = await scryer.delete_title_preview(title_id)
            await scryer.delete_title(
                title_id,
                fingerprint=preview.get("fingerprint"),
                delete_files=False,
            )
            await db.log_action(ActionLog(
                user_id=db_user.tg_id,
                action_type=ActionType.ADD,
                content_type=ContentType.UNKNOWN,
                content_title=preview.get("targetLabel"),
                content_id=title_id,
                details="deleted",
            ))
            await message.edit_text(
                Formatters.format_success("Удалено из каталога. Файлы на диске не тронуты.")
            )
            return

    except Exception as e:
        logger.error("title_action_failed", action=action, title_id=title_id, error=str(e), exc_info=True)
        await message.edit_text(
            Formatters.format_error(f"Не удалось выполнить действие: {html.escape(str(e))[:150]}")
        )
