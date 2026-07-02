"""Feature #6: runtime user management (DB-backed allowlist + admin commands)."""

import pytest

from bot.db import Database


@pytest.mark.asyncio
async def test_db_allowlist_roundtrip():
    db = Database(":memory:")
    await db.connect()
    try:
        assert await db.is_allowed_in_db(555) is False
        await db.add_allowed_user(555, added_by=1)
        assert await db.is_allowed_in_db(555) is True
        assert 555 in await db.list_allowed_users()
        # idempotent add
        await db.add_allowed_user(555, added_by=1)
        assert (await db.list_allowed_users()).count(555) == 1
        await db.remove_allowed_user(555)
        assert await db.is_allowed_in_db(555) is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_auth_authorization_combines_env_and_db():
    """A user absent from the env allowlist but present in the DB allowlist must
    be authorized; a user in neither must be rejected."""
    from bot.middleware.auth import AuthMiddleware

    db = Database(":memory:")
    await db.connect()
    try:
        mw = AuthMiddleware(db)
        # conftest env allowlist = {123456789, 987654321}
        assert await mw._is_authorized(123456789) is True   # env
        assert await mw._is_authorized(999) is False         # neither
        await db.add_allowed_user(999, added_by=123456789)
        assert await mw._is_authorized(999) is True          # now via DB
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_cmd_adduser_admin_grants_and_nonadmin_rejected():
    from unittest.mock import AsyncMock, MagicMock
    from bot.handlers import users

    db = AsyncMock()
    msg = MagicMock()
    msg.text = "/adduser 42"
    msg.from_user = MagicMock(id=1)
    msg.answer = AsyncMock()

    await users.cmd_adduser(msg, db=db, is_admin=True)
    db.add_allowed_user.assert_awaited_once()
    assert db.add_allowed_user.await_args.args[0] == 42

    db.add_allowed_user.reset_mock()
    await users.cmd_adduser(msg, db=db, is_admin=False)
    db.add_allowed_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_cmd_deluser_parses_and_revokes():
    from unittest.mock import AsyncMock, MagicMock
    from bot.handlers import users

    db = AsyncMock()
    msg = MagicMock()
    msg.text = "/deluser 999"
    msg.from_user = MagicMock(id=1)
    msg.answer = AsyncMock()

    await users.cmd_deluser(msg, db=db, is_admin=True)
    db.remove_allowed_user.assert_awaited_once()
    assert db.remove_allowed_user.await_args.args[0] == 999


# --- DB-04/BUG-15: runtime-allowlist users must be subscribed/unsubscribed
# from notifications so /adduser'd users actually receive download-completion
# and webhook alerts (not just gain bot access).


@pytest.mark.asyncio
async def test_cmd_adduser_subscribes_to_notifications():
    from unittest.mock import AsyncMock, MagicMock
    from bot.handlers import users

    db = AsyncMock()
    notification_service = MagicMock()
    msg = MagicMock()
    msg.text = "/adduser 42"
    msg.from_user = MagicMock(id=1)
    msg.answer = AsyncMock()

    await users.cmd_adduser(msg, db=db, is_admin=True, notification_service=notification_service)
    notification_service.subscribe_user.assert_called_once_with(42)


@pytest.mark.asyncio
async def test_cmd_adduser_works_when_notification_service_is_none():
    """qBittorrent not configured -> notification_service is None; /adduser
    must still grant DB access without raising."""
    from unittest.mock import AsyncMock, MagicMock
    from bot.handlers import users

    db = AsyncMock()
    msg = MagicMock()
    msg.text = "/adduser 42"
    msg.from_user = MagicMock(id=1)
    msg.answer = AsyncMock()

    await users.cmd_adduser(msg, db=db, is_admin=True, notification_service=None)
    db.add_allowed_user.assert_awaited_once()


@pytest.mark.asyncio
async def test_cmd_deluser_unsubscribes_from_notifications():
    from unittest.mock import AsyncMock, MagicMock
    from bot.handlers import users

    db = AsyncMock()
    notification_service = MagicMock()
    msg = MagicMock()
    msg.text = "/deluser 999"
    msg.from_user = MagicMock(id=1)
    msg.answer = AsyncMock()

    await users.cmd_deluser(msg, db=db, is_admin=True, notification_service=notification_service)
    notification_service.unsubscribe_user.assert_called_once_with(999)
