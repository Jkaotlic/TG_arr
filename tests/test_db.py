"""Tests for database operations."""

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from bot.db import Database
from bot.models import (
    ActionLog,
    ActionType,
    ContentType,
    QualityInfo,
    SearchResult,
    SearchSession,
    User,
    UserPreferences,
    UserRole,
)


@pytest_asyncio.fixture
async def db():
    """Create an in-memory database for testing."""
    database = Database(":memory:")
    await database.connect()
    yield database
    await database.close()


@pytest.mark.asyncio
class TestUserOperations:
    """Test user CRUD operations."""

    async def test_create_user(self, db):
        """Test creating a new user."""
        user = User(
            tg_id=123456789,
            username="testuser",
            first_name="Test",
            role=UserRole.USER,
        )

        created = await db.create_user(user)

        assert created.tg_id == 123456789
        assert created.username == "testuser"
        assert created.role == UserRole.USER

    async def test_get_user(self, db):
        """Test retrieving a user."""
        user = User(
            tg_id=123456789,
            username="testuser",
            first_name="Test",
        )
        await db.create_user(user)

        retrieved = await db.get_user(123456789)

        assert retrieved is not None
        assert retrieved.tg_id == 123456789
        assert retrieved.username == "testuser"

    async def test_get_nonexistent_user(self, db):
        """Test retrieving a user that doesn't exist."""
        retrieved = await db.get_user(999999999)
        assert retrieved is None

    async def test_update_user_preferences(self, db):
        """Test updating user preferences."""
        user = User(tg_id=123456789)
        await db.create_user(user)

        new_prefs = UserPreferences(
            radarr_quality_profile_id=5,
            radarr_root_folder_id=1,
            preferred_resolution="1080p",
            auto_grab_enabled=True,
        )
        await db.update_user_preferences(123456789, new_prefs)

        retrieved = await db.get_user(123456789)
        assert retrieved.preferences.radarr_quality_profile_id == 5
        assert retrieved.preferences.preferred_resolution == "1080p"
        assert retrieved.preferences.auto_grab_enabled is True


@pytest.mark.asyncio
class TestSearchOperations:
    """Test search and session operations."""

    async def test_save_search(self, db):
        """Test saving a search with results."""
        # Create user first
        user = User(tg_id=123456789)
        await db.create_user(user)

        results = [
            SearchResult(
                guid="test-1",
                title="Test.Movie.1080p",
                indexer="TestIndexer",
                size=5000000000,
                quality=QualityInfo(resolution="1080p"),
            ),
            SearchResult(
                guid="test-2",
                title="Test.Movie.720p",
                indexer="TestIndexer",
                size=2500000000,
                quality=QualityInfo(resolution="720p"),
            ),
        ]

        search_id = await db.save_search(123456789, "test movie", ContentType.MOVIE, results)

        assert search_id > 0

        # DB-03: search_results is gone; save_search only persists metadata
        # (query/content_type/result_count) in the `searches` table.
        async with db.conn.execute(
            "SELECT query, content_type, result_count FROM searches WHERE id = ?",
            (search_id,),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row["query"] == "test movie"
        assert row["content_type"] == ContentType.MOVIE.value
        assert row["result_count"] == 2

    async def test_save_and_get_session(self, db):
        """Test saving and retrieving a session."""
        user = User(tg_id=123456789)
        await db.create_user(user)

        session = SearchSession(
            user_id=123456789,
            query="test query",
            content_type=ContentType.MOVIE,
            current_page=2,
        )

        await db.save_session(123456789, session)

        retrieved = await db.get_session(123456789)
        assert retrieved is not None
        assert retrieved.query == "test query"
        assert retrieved.content_type == ContentType.MOVIE
        assert retrieved.current_page == 2

    async def test_delete_session(self, db):
        """Test deleting a session."""
        user = User(tg_id=123456789)
        await db.create_user(user)

        session = SearchSession(
            user_id=123456789,
            query="test query",
            content_type=ContentType.MOVIE,
        )
        await db.save_session(123456789, session)

        await db.delete_session(123456789)

        retrieved = await db.get_session(123456789)
        assert retrieved is None

    async def test_session_update(self, db):
        """Test updating an existing session."""
        user = User(tg_id=123456789)
        await db.create_user(user)

        # Create initial session
        session1 = SearchSession(
            user_id=123456789,
            query="first query",
            content_type=ContentType.MOVIE,
        )
        await db.save_session(123456789, session1)

        # Update session
        session2 = SearchSession(
            user_id=123456789,
            query="second query",
            content_type=ContentType.SERIES,
            current_page=5,
        )
        await db.save_session(123456789, session2)

        retrieved = await db.get_session(123456789)
        assert retrieved.query == "second query"
        assert retrieved.content_type == ContentType.SERIES
        assert retrieved.current_page == 5


@pytest.mark.asyncio
class TestActionLogOperations:
    """Test action log operations."""

    async def test_log_action(self, db):
        """Test logging an action."""
        user = User(tg_id=123456789)
        await db.create_user(user)

        action = ActionLog(
            user_id=123456789,
            action_type=ActionType.SEARCH,
            content_type=ContentType.MOVIE,
            query="test movie",
        )

        action_id = await db.log_action(action)
        assert action_id > 0

    async def test_get_user_actions(self, db):
        """Test retrieving user actions."""
        user = User(tg_id=123456789)
        await db.create_user(user)

        # Log multiple actions
        for i in range(5):
            action = ActionLog(
                user_id=123456789,
                action_type=ActionType.SEARCH,
                content_type=ContentType.MOVIE,
                query=f"query {i}",
            )
            await db.log_action(action)

        actions = await db.get_user_actions(123456789, limit=3)
        assert len(actions) == 3

    async def test_action_log_with_error(self, db):
        """Test logging a failed action."""
        user = User(tg_id=123456789)
        await db.create_user(user)

        action = ActionLog(
            user_id=123456789,
            action_type=ActionType.ADD,
            content_type=ContentType.MOVIE,
            content_title="Test Movie",
            success=False,
            error_message="Connection timeout",
        )

        await db.log_action(action)

        actions = await db.get_user_actions(123456789)
        assert len(actions) == 1
        assert actions[0].success is False
        assert actions[0].error_message == "Connection timeout"

    async def test_get_all_actions(self, db):
        """Test retrieving all actions (admin view)."""
        # Create multiple users
        for uid in [111, 222, 333]:
            user = User(tg_id=uid)
            await db.create_user(user)
            action = ActionLog(
                user_id=uid,
                action_type=ActionType.SEARCH,
                content_type=ContentType.MOVIE,
                query=f"query from {uid}",
            )
            await db.log_action(action)

        actions = await db.get_all_actions(limit=10)
        assert len(actions) == 3


@pytest.mark.asyncio
class TestMigrations:
    """Test schema migration framework (DB-01)."""

    async def test_migration_v0_to_v1_idempotent(self, tmp_path):
        """Migration framework (DB-01) is idempotent across re-connects.

        PRAGMA user_version must be set after the first connect and must
        not advance when re-opening an already-migrated DB.
        """
        db_path = str(tmp_path / "migrate.db")

        db1 = Database(db_path)
        await db1.connect()
        async with db1.conn.execute("PRAGMA user_version") as c:
            row = await c.fetchone()
            v_first = row[0]
        await db1.close()

        db2 = Database(db_path)
        await db2.connect()
        async with db2.conn.execute("PRAGMA user_version") as c:
            row = await c.fetchone()
            v_second = row[0]
        await db2.close()

        assert v_first >= 1, "Schema version must be at least 1 after first connect"
        assert v_second == v_first, "Schema version must not advance on reconnect"


@pytest.mark.asyncio
class TestCorruptPreferences:
    """Test BUG-24 / SEC-19: corrupt preferences JSON → defaults."""

    async def test_get_user_falls_back_on_corrupt_preferences(self, db):
        """Invalid preferences JSON should yield default UserPreferences (not crash)."""
        user = User(tg_id=7777777)
        await db.create_user(user)

        # Corrupt the preferences column directly
        await db.conn.execute(
            "UPDATE users SET preferences = ? WHERE tg_id = ?",
            ("{not valid json", 7777777),
        )
        await db.conn.commit()

        retrieved = await db.get_user(7777777)
        assert retrieved is not None
        assert retrieved.preferences is not None
        # Default values should be present
        defaults = UserPreferences()
        assert retrieved.preferences.auto_grab_enabled == defaults.auto_grab_enabled
        assert retrieved.preferences.preferred_resolution == defaults.preferred_resolution


@pytest.mark.asyncio
class TestCleanupOperations:
    """Test cleanup operations.

    TEST-04: these used to assert ``deleted >= 0`` — an always-true tautology
    that provided zero coverage. Insert a row with an explicitly old
    ``created_at``/``updated_at`` (bypassing the ``now()`` timestamp that
    ``save_session``/``save_search`` always write) and assert the exact count,
    with a fresh row surviving alongside it.
    """

    async def test_cleanup_old_sessions(self, db):
        """A session older than the cutoff is deleted; a fresh one survives."""
        user = User(tg_id=123456789)
        await db.create_user(user)

        old_ts = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        async with db._write_lock:
            await db.conn.execute(
                "INSERT INTO sessions (user_id, session_data, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (123456789, "{}", old_ts, old_ts),
            )
            await db.conn.commit()

        other_user = User(tg_id=555000111)
        await db.create_user(other_user)
        fresh_session = SearchSession(
            user_id=555000111,
            query="fresh query",
            content_type=ContentType.MOVIE,
        )
        await db.save_session(555000111, fresh_session)

        deleted = await db.cleanup_old_sessions(hours=24)

        assert deleted == 1
        assert await db.get_session(123456789) is None
        assert await db.get_session(555000111) is not None

    async def test_cleanup_old_searches(self, db):
        """A search older than the cutoff is deleted; a fresh one survives."""
        user = User(tg_id=123456789)
        await db.create_user(user)

        old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        async with db._write_lock:
            await db.conn.execute(
                "INSERT INTO searches (user_id, query, content_type, result_count, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (123456789, "old search", ContentType.MOVIE.value, 0, old_ts),
            )
            await db.conn.commit()

        await db.save_search(
            123456789,
            "fresh search",
            ContentType.MOVIE,
            [SearchResult(guid="test", title="Test", indexer="Test")],
        )

        deleted = await db.cleanup_old_searches(days=7)

        assert deleted == 1
        async with db.conn.execute("SELECT query FROM searches") as cursor:
            remaining = [row["query"] for row in await cursor.fetchall()]
        assert remaining == ["fresh search"]


@pytest.mark.asyncio
class TestUpdateUserPreference:
    """DB-05: point-update of a single preference key via json_set."""

    async def test_update_number_preference(self, db):
        user = User(tg_id=123456789)
        await db.create_user(user)

        ok = await db.update_user_preference(123456789, "radarr_quality_profile_id", 7)

        assert ok is True
        retrieved = await db.get_user(123456789)
        assert retrieved.preferences.radarr_quality_profile_id == 7

    async def test_update_string_preference(self, db):
        user = User(tg_id=123456789)
        await db.create_user(user)

        ok = await db.update_user_preference(123456789, "preferred_resolution", "1080p")

        assert ok is True
        retrieved = await db.get_user(123456789)
        assert retrieved.preferences.preferred_resolution == "1080p"

    async def test_update_null_preference(self, db):
        user = User(tg_id=123456789)
        await db.create_user(user)
        await db.update_user_preference(123456789, "preferred_resolution", "1080p")

        ok = await db.update_user_preference(123456789, "preferred_resolution", None)

        assert ok is True
        retrieved = await db.get_user(123456789)
        assert retrieved.preferences.preferred_resolution is None

    async def test_update_nonexistent_user_returns_false(self, db):
        ok = await db.update_user_preference(999999999, "auto_grab_enabled", True)
        assert ok is False

    async def test_concurrent_updates_to_different_keys_both_survive(self, db):
        """DB-05: two concurrent point-updates on different keys must not
        clobber each other the way a read-modify-write of the whole
        preferences blob would.
        """
        import asyncio

        user = User(tg_id=123456789)
        await db.create_user(user)

        await asyncio.gather(
            db.update_user_preference(123456789, "radarr_quality_profile_id", 3),
            db.update_user_preference(123456789, "sonarr_quality_profile_id", 9),
        )

        retrieved = await db.get_user(123456789)
        assert retrieved.preferences.radarr_quality_profile_id == 3
        assert retrieved.preferences.sonarr_quality_profile_id == 9


@pytest.mark.asyncio
class TestRemoveAllowedUserCleansSession:
    """DB-09: revoking runtime access also drops the user's session."""

    async def test_remove_allowed_user_deletes_session(self, db):
        user = User(tg_id=123456789)
        await db.create_user(user)
        await db.add_allowed_user(123456789, added_by=1)
        session = SearchSession(
            user_id=123456789, query="q", content_type=ContentType.MOVIE
        )
        await db.save_session(123456789, session)

        await db.remove_allowed_user(123456789)

        assert await db.get_session(123456789) is None
        assert await db.is_allowed_in_db(123456789) is False


@pytest.mark.asyncio
class TestActionsCompositeIndexMigration:
    """DB-06: idx_actions_user_created exists; idx_actions_user is dropped."""

    async def test_fresh_database_has_composite_index_only(self, db):
        async with db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='actions'"
        ) as cursor:
            names = {row["name"] for row in await cursor.fetchall()}
        assert "idx_actions_user_created" in names
        assert "idx_actions_user" not in names

    async def test_v2_database_migrates_index_on_connect(self, tmp_path):
        """A pre-v3 database (with the old single-column index) gets the
        composite index added and the old one dropped on next connect.
        """
        db_path = str(tmp_path / "legacy.db")

        db1 = Database(db_path)
        await db1.connect()
        # Roll back to v2 schema shape: drop the composite index, recreate
        # the legacy single-column one, and rewind user_version.
        await db1.conn.execute("DROP INDEX IF EXISTS idx_actions_user_created")
        await db1.conn.execute("CREATE INDEX idx_actions_user ON actions(user_id)")
        await db1.conn.execute("PRAGMA user_version = 2")
        await db1.conn.commit()
        await db1.close()

        db2 = Database(db_path)
        await db2.connect()
        async with db2.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='actions'"
        ) as cursor:
            names = {row["name"] for row in await cursor.fetchall()}
        await db2.close()

        assert "idx_actions_user_created" in names
        assert "idx_actions_user" not in names


@pytest.mark.asyncio
class TestRunMaintenance:
    """DB-01/DB-08: run_maintenance() — cleanup + PRAGMA optimize + backup."""

    async def test_run_maintenance_returns_cleanup_counts(self, db):
        user = User(tg_id=123456789)
        await db.create_user(user)

        old_ts = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        async with db._write_lock:
            await db.conn.execute(
                "INSERT INTO sessions (user_id, session_data, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (123456789, "{}", old_ts, old_ts),
            )
            await db.conn.commit()

        result = await db.run_maintenance(backup=False)

        assert result["sessions"] == 1
        assert result["searches"] == 0
        assert result["actions"] == 0
        assert result["backup"] == 0

    async def test_run_maintenance_backup_creates_file(self, tmp_path):
        db_path = str(tmp_path / "bot.db")
        database = Database(db_path)
        await database.connect()
        try:
            result = await database.run_maintenance(backup=True)
            assert result["backup"] == 1

            backup_dir = tmp_path / "backup"
            today = datetime.now(timezone.utc).strftime("%Y%m%d")
            backup_file = backup_dir / f"bot-{today}.db"
            assert backup_file.exists()
        finally:
            await database.close()

    async def test_run_maintenance_backup_same_day_is_noop(self, tmp_path):
        """Calling run_maintenance(backup=True) twice on the same day must not
        raise (VACUUM INTO would fail if the target already existed and we
        didn't guard for it) and must not report a second backup created.
        """
        db_path = str(tmp_path / "bot.db")
        database = Database(db_path)
        await database.connect()
        try:
            first = await database.run_maintenance(backup=True)
            second = await database.run_maintenance(backup=True)

            assert first["backup"] == 1
            assert second["backup"] == 0

            backup_dir = tmp_path / "backup"
            today = datetime.now(timezone.utc).strftime("%Y%m%d")
            assert (backup_dir / f"bot-{today}.db").exists()
        finally:
            await database.close()

    async def test_backup_rotation_keeps_three_most_recent(self, tmp_path):
        db_path = str(tmp_path / "bot.db")
        database = Database(db_path)
        await database.connect()
        try:
            backup_dir = tmp_path / "backup"
            backup_dir.mkdir(parents=True, exist_ok=True)
            # Seed 4 fake older backup files (dates in the past).
            for day in ("20250101", "20250102", "20250103", "20250104"):
                (backup_dir / f"bot-{day}.db").write_bytes(b"fake")

            result = await database._backup(_keep=3)

            remaining = sorted(p.name for p in backup_dir.glob("bot-*.db"))
            assert len(remaining) == 3
            assert result in (0, 1)
        finally:
            await database.close()

    async def test_run_maintenance_backup_skipped_for_memory_db(self, db):
        """In-memory DB (used by most tests) has no on-disk path to back up."""
        result = await db.run_maintenance(backup=True)
        assert result["backup"] == 0
