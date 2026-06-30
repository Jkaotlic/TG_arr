"""Tests for C6-db-notify cluster (DB-02, DB-04, DB-07, RACE-05)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from bot.db import Database
from bot.models import ActionLog, ActionType, ContentType, User
from bot.services.notification_service import NotificationService


@pytest_asyncio.fixture
async def db():
    """Create an in-memory database for testing."""
    database = Database(":memory:")
    await database.connect()
    # actions/searches have a FK to users(tg_id)
    await database.create_user(User(tg_id=123456789))
    yield database
    await database.close()


def _make_action(user_id: int = 123456789) -> ActionLog:
    return ActionLog(
        user_id=user_id,
        action_type=ActionType.SEARCH,
        content_type=ContentType.MOVIE,
        query="test",
    )


@pytest.mark.asyncio
class TestLogActionContract:
    """DB-07: log_action must honestly return an int."""

    async def test_log_action_returns_int(self, db):
        action_id = await db.log_action(_make_action())
        assert isinstance(action_id, int)
        assert action_id > 0


@pytest.mark.asyncio
class TestCleanupOldActions:
    """DB-02: actions table must be prunable."""

    async def test_cleanup_removes_old_keeps_recent(self, db):
        # Recent action via the normal path
        await db.log_action(_make_action())

        # Old action — inject directly with an old created_at
        old_ts = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
        async with db._write_lock:
            await db.conn.execute(
                """
                INSERT INTO actions (
                    user_id, action_type, content_type, query, content_title,
                    content_id, release_title, success, error_message, details, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    123456789,
                    ActionType.SEARCH.value,
                    ContentType.MOVIE.value,
                    "old",
                    None,
                    None,
                    None,
                    1,
                    None,
                    None,
                    old_ts,
                ),
            )
            await db.conn.commit()

        deleted = await db.cleanup_old_actions(days=90)
        assert deleted == 1

        remaining = await db.get_all_actions(limit=50)
        assert len(remaining) == 1
        assert remaining[0].query == "test"

    async def test_cleanup_returns_zero_when_nothing_old(self, db):
        await db.log_action(_make_action())
        deleted = await db.cleanup_old_actions(days=90)
        assert deleted == 0


@pytest.mark.asyncio
class TestWalCheckpointOnClose:
    """DB-04: close() must checkpoint the WAL before closing."""

    async def test_close_issues_wal_checkpoint(self):
        database = Database(":memory:")
        await database.connect()

        executed: list[str] = []
        real_execute = database._connection.execute

        async def spy_execute(sql, *args, **kwargs):
            executed.append(sql)
            return await real_execute(sql, *args, **kwargs)

        database._connection.execute = spy_execute  # type: ignore[assignment]
        await database.close()

        assert any("wal_checkpoint(TRUNCATE)" in sql for sql in executed)


@pytest.mark.asyncio
class TestNotificationServiceUsesInjectedClient:
    """RACE-05: the service must use the exact qBittorrent client injected.

    Startup injects the registry singleton (await get_qbittorrent()) so there
    is a single qBittorrent client; the service must not wrap/replace it.
    """

    async def test_uses_injected_qbittorrent_instance(self):
        sentinel_client = AsyncMock()
        sender = AsyncMock()
        service = NotificationService(sentinel_client, sender)
        assert service.qbittorrent is sentinel_client
