"""Runtime user management commands (feature #6, admin-only).

Lets an admin grant/revoke bot access at runtime (persisted in the DB allowlist)
instead of editing ALLOWED_TG_IDS and restarting. The env allowlist stays
authoritative for admins; DB-granted users are regular users.
"""

from typing import Optional

import structlog
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.config import get_settings
from bot.db import Database
from bot.services.notification_service import NotificationService

logger = structlog.get_logger()
router = Router()


def _parse_user_id(text: str | None) -> int | None:
    if not text:
        return None
    parts = text.split()
    if len(parts) < 2:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


@router.message(Command("users"))
async def cmd_users(message: Message, db: Database, is_admin: bool) -> None:
    """List runtime-granted users (admin only)."""
    if not is_admin:
        await message.answer("⛔ Только для администратора.")
        return
    ids = await db.list_allowed_users()
    if not ids:
        await message.answer(
            "Runtime-allowlist пуст.\nДобавить: <code>/adduser &lt;tg_id&gt;</code>",
            parse_mode="HTML",
        )
        return
    lines = "\n".join(f"• <code>{uid}</code>" for uid in ids)
    await message.answer(
        f"👥 <b>Runtime-доступ ({len(ids)}):</b>\n{lines}\n\n"
        f"Убрать: <code>/deluser &lt;tg_id&gt;</code>",
        parse_mode="HTML",
    )


@router.message(Command("adduser"))
async def cmd_adduser(
    message: Message,
    db: Database,
    is_admin: bool,
    notification_service: Optional[NotificationService] = None,
) -> None:
    """Grant a user runtime access (admin only).

    DB-04/BUG-15/LOGIC-08: also subscribes the user to download-completion /
    webhook notifications immediately — without this, a runtime-granted user
    had bot access but silently never received any notification until the
    next bot restart (on_startup resubscribes from db.list_allowed_users()).
    """
    if not is_admin:
        await message.answer("⛔ Только для администратора.")
        return
    uid = _parse_user_id(message.text)
    if uid is None:
        await message.answer("Использование: <code>/adduser &lt;tg_id&gt;</code>", parse_mode="HTML")
        return
    added_by = message.from_user.id if message.from_user else 0
    await db.add_allowed_user(uid, added_by=added_by)
    if notification_service is not None:
        notification_service.subscribe_user(uid)
    logger.info("runtime_user_granted", user_id=uid, added_by=added_by)

    if get_settings().is_user_allowed(uid):
        # Access already came from the env allowlist; the DB row changes nothing
        # for this user, and saying "получил доступ" would imply it did.
        await message.answer(
            f"✅ Пользователь <code>{uid}</code> уже имеет доступ через "
            f"<code>ALLOWED_TG_IDS</code> — запись в боте добавлена, но она "
            f"ничего не меняет.",
            parse_mode="HTML",
        )
        return

    await message.answer(f"✅ Пользователь <code>{uid}</code> получил доступ.", parse_mode="HTML")


@router.message(Command("deluser"))
async def cmd_deluser(
    message: Message,
    db: Database,
    is_admin: bool,
    notification_service: Optional[NotificationService] = None,
) -> None:
    """Revoke a user's runtime access (admin only)."""
    if not is_admin:
        await message.answer("⛔ Только для администратора.")
        return
    uid = _parse_user_id(message.text)
    if uid is None:
        await message.answer("Использование: <code>/deluser &lt;tg_id&gt;</code>", parse_mode="HTML")
        return
    await db.remove_allowed_user(uid)
    if notification_service is not None:
        notification_service.unsubscribe_user(uid)

    # SEC-01 (audit 2026-07-30): authorization is `env allowlist OR db
    # allowlist`, so dropping the DB row does nothing for a user named in
    # ALLOWED_TG_IDS. Reporting "доступ отозван" there is worse than useless —
    # the admin stops looking while the user keeps full access.
    if get_settings().is_user_allowed(uid):
        logger.warning("runtime_user_revoke_ineffective", user_id=uid, reason="env_allowlist")
        await message.answer(
            f"⚠️ Запись в боте удалена, но <code>{uid}</code> остаётся в "
            f"<code>ALLOWED_TG_IDS</code> и доступ сохраняет.\n\n"
            f"Чтобы отозвать по-настоящему: убрать id из <code>ALLOWED_TG_IDS</code> "
            f"в <code>.env</code> и перезапустить бота.",
            parse_mode="HTML",
        )
        return

    logger.info("runtime_user_revoked", user_id=uid)
    await message.answer(f"🚫 Доступ пользователя <code>{uid}</code> отозван.", parse_mode="HTML")
