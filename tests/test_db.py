"""Tests for database operations."""

import asyncio
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

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


@pytest.mark.asyncio
class TestSessionLock:
    """DB-02: per-user lock guarding the get_session -> mutate -> save_session
    read-modify-write cycle used by handlers (search.py/music.py hot paths)."""

    async def test_session_lock_returns_same_lock_for_same_user(self, db):
        lock_a = db.session_lock(111)
        lock_b = db.session_lock(111)
        assert lock_a is lock_b

    async def test_session_lock_returns_different_locks_for_different_users(self, db):
        assert db.session_lock(111) is not db.session_lock(222)

    async def test_concurrent_read_modify_write_without_lock_loses_an_update(self, db):
        """Control case: two concurrent callbacks racing get_session ->
        mutate -> save_session WITHOUT the lock — the loser's field is lost.
        This documents the exact failure DB-02 fixes; it is not itself a
        regression guard (it asserts the *bug*, not the fix)."""
        user_id = 999
        await db.create_user(User(tg_id=user_id))
        session = SearchSession(user_id=user_id, query="q", content_type=ContentType.MOVIE)
        await db.save_session(user_id, session)

        async def racer_a():
            s = await db.get_session(user_id)
            await asyncio.sleep(0.02)  # let racer_b read the same pre-mutation state
            s.current_page = 1
            await db.save_session(user_id, s)

        async def racer_b():
            s = await db.get_session(user_id)
            s.monitor_type = "all"
            await db.save_session(user_id, s)  # commits first, has no current_page change

        await asyncio.gather(racer_a(), racer_b())

        final = await db.get_session(user_id)
        # racer_b's save happened first (no sleep) and lacked racer_a's page
        # change at read time; racer_a's later save overwrites monitor_type
        # back to None because it read the session before racer_b wrote it.
        assert final.current_page == 1
        assert final.monitor_type is None  # lost update — this is the bug

    async def test_concurrent_read_modify_write_with_lock_preserves_both_updates(self, db):
        """RED->GREEN: wrapping the same race in `async with db.session_lock(user_id):`
        serializes the two read-modify-write cycles so both mutations survive."""
        user_id = 1000
        await db.create_user(User(tg_id=user_id))
        session = SearchSession(user_id=user_id, query="q", content_type=ContentType.MOVIE)
        await db.save_session(user_id, session)

        async def racer_a():
            async with db.session_lock(user_id):
                s = await db.get_session(user_id)
                await asyncio.sleep(0.02)
                s.current_page = 1
                await db.save_session(user_id, s)

        async def racer_b():
            async with db.session_lock(user_id):
                s = await db.get_session(user_id)
                s.monitor_type = "all"
                await db.save_session(user_id, s)

        await asyncio.gather(racer_a(), racer_b())

        final = await db.get_session(user_id)
        assert final.current_page == 1
        assert final.monitor_type == "all"


@pytest.mark.asyncio
class TestSessionCache:
    """PERF-04: in-process write-through cache of active sessions.

    Every click (pagination/selection) used to round-trip a full JSON
    parse/pydantic-validate (~100 nested models) through SQLite via
    get_session/save_session. A cache hit still does one cheap SELECT to
    confirm the stored text matches what's cached (so out-of-band tampering,
    e.g. the corrupt-row regression tests, is still detected) but skips the
    expensive part — ``json.loads`` + ``SearchSession.model_validate`` of the
    full payload — entirely.
    """

    async def _execute_calls(self, db, monkeypatch) -> list[str]:
        """Patch db.conn.execute to record the SQL text of every call."""
        calls: list[str] = []
        real_execute = db.conn.execute

        def recording_execute(sql, *args, **kwargs):
            calls.append(sql)
            return real_execute(sql, *args, **kwargs)

        monkeypatch.setattr(db.conn, "execute", recording_execute)
        return calls

    async def test_cache_hit_skips_json_parse_and_validate(self, db, monkeypatch):
        """A cache hit may issue a cheap SELECT to confirm freshness, but must
        never re-run json.loads/model_validate — the actual PERF-04 cost."""
        user_id = 2001
        await db.create_user(User(tg_id=user_id))
        session = SearchSession(user_id=user_id, query="q", content_type=ContentType.MOVIE)
        await db.save_session(user_id, session)  # populates cache (write-through)

        with (
            patch("bot.db.json.loads") as mock_loads,
            patch(
                "bot.db.SearchSession.model_validate", wraps=SearchSession.model_validate
            ) as mock_validate,
        ):
            retrieved = await db.get_session(user_id)

        assert retrieved is not None
        assert retrieved.query == "q"
        mock_loads.assert_not_called()
        mock_validate.assert_not_called()

    async def test_cache_miss_falls_back_to_sqlite_and_populates_cache(self, db, monkeypatch):
        user_id = 2002
        await db.create_user(User(tg_id=user_id))
        session = SearchSession(user_id=user_id, query="q", content_type=ContentType.MOVIE)
        await db.save_session(user_id, session)
        db._cache_invalidate_session(user_id)  # force a miss

        with patch(
            "bot.db.SearchSession.model_validate", wraps=SearchSession.model_validate
        ) as mock_validate:
            first = await db.get_session(user_id)
            assert first is not None
            mock_validate.assert_called_once()  # had to parse the full row

            mock_validate.reset_mock()
            second = await db.get_session(user_id)
            assert second is not None
            mock_validate.assert_not_called()  # now served from cache

    async def test_write_through_visible_to_next_get_session(self, db):
        """save_session's write-through means the very next get_session (even
        with an untouched cache) sees the update without re-reading SQLite."""
        user_id = 2003
        await db.create_user(User(tg_id=user_id))
        session = SearchSession(user_id=user_id, query="first", content_type=ContentType.MOVIE)
        await db.save_session(user_id, session)

        session.query = "second"
        session.current_page = 3
        await db.save_session(user_id, session)

        retrieved = await db.get_session(user_id)
        assert retrieved.query == "second"
        assert retrieved.current_page == 3

    async def test_update_session_write_through_visible(self, db):
        user_id = 2004
        await db.create_user(User(tg_id=user_id))
        session = SearchSession(user_id=user_id, query="q", content_type=ContentType.MOVIE)
        await db.save_session(user_id, session)

        session.current_page = 7
        ok = await db.update_session(user_id, session)
        assert ok is True

        retrieved = await db.get_session(user_id)
        assert retrieved.current_page == 7

    async def test_update_session_rowcount_zero_does_not_resurrect_cache(self, db):
        """RACE-04: if the session row was deleted concurrently, update_session
        must not repopulate the cache with a session that no longer exists."""
        user_id = 2005
        await db.create_user(User(tg_id=user_id))
        session = SearchSession(user_id=user_id, query="q", content_type=ContentType.MOVIE)
        await db.save_session(user_id, session)

        await db.delete_session(user_id)  # row gone, cache invalidated

        session.current_page = 9
        ok = await db.update_session(user_id, session)
        assert ok is False

        assert await db._cache_get_session(user_id) is None
        assert await db.get_session(user_id) is None

    async def test_delete_session_invalidates_cache(self, db):
        user_id = 2006
        await db.create_user(User(tg_id=user_id))
        session = SearchSession(user_id=user_id, query="q", content_type=ContentType.MOVIE)
        await db.save_session(user_id, session)
        assert await db._cache_get_session(user_id) is not None

        await db.delete_session(user_id)

        assert await db._cache_get_session(user_id) is None
        assert await db.get_session(user_id) is None

    async def test_cleanup_old_sessions_invalidates_cache(self, db):
        user_id = 2007
        await db.create_user(User(tg_id=user_id))
        session = SearchSession(user_id=user_id, query="q", content_type=ContentType.MOVIE)
        await db.save_session(user_id, session)
        assert await db._cache_get_session(user_id) is not None

        # Backdate updated_at directly so cleanup treats it as stale.
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        async with db._write_lock:
            await db.conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE user_id = ?", (old_ts, user_id)
            )
            await db.conn.commit()

        deleted = await db.cleanup_old_sessions(hours=24)
        assert deleted == 1
        assert await db._cache_get_session(user_id) is None
        assert await db.get_session(user_id) is None

    async def test_remove_allowed_user_invalidates_session_cache(self, db):
        user_id = 2008
        await db.create_user(User(tg_id=user_id))
        session = SearchSession(user_id=user_id, query="q", content_type=ContentType.MOVIE)
        await db.save_session(user_id, session)
        assert await db._cache_get_session(user_id) is not None

        await db.remove_allowed_user(user_id)

        assert await db._cache_get_session(user_id) is None

    async def test_corrupt_session_row_invalidates_cache_and_deletes(self, db):
        user_id = 2009
        await db.create_user(User(tg_id=user_id))
        session = SearchSession(user_id=user_id, query="q", content_type=ContentType.MOVIE)
        await db.save_session(user_id, session)
        db._cache_invalidate_session(user_id)  # force re-read from (corrupted) row

        async with db._write_lock:
            await db.conn.execute(
                "UPDATE sessions SET session_data = ? WHERE user_id = ?",
                ("not-valid-json{{{", user_id),
            )
            await db.conn.commit()

        result = await db.get_session(user_id)
        assert result is None
        assert await db._cache_get_session(user_id) is None

    async def test_returned_session_is_independent_copy(self, db):
        """Mutating the object returned by get_session must not corrupt the
        cached copy — otherwise two racing handlers reading the same cached
        session would silently share state (breaking DB-02's guarantees)."""
        user_id = 2010
        await db.create_user(User(tg_id=user_id))
        session = SearchSession(user_id=user_id, query="q", content_type=ContentType.MOVIE)
        await db.save_session(user_id, session)

        first = await db.get_session(user_id)
        first.query = "mutated-locally"

        second = await db.get_session(user_id)
        assert second.query == "q"  # unaffected by the mutation on `first`

    async def test_save_session_stores_independent_copy_in_cache(self, db):
        """Mutating the `session` object after save_session() returns must not
        retroactively change what's cached (and later returned)."""
        user_id = 2011
        await db.create_user(User(tg_id=user_id))
        session = SearchSession(user_id=user_id, query="q", content_type=ContentType.MOVIE)
        await db.save_session(user_id, session)

        session.query = "mutated-after-save"  # caller keeps using the object

        retrieved = await db.get_session(user_id)
        assert retrieved.query == "q"

    async def test_cache_ttl_expiry_falls_back_to_sqlite(self, db, monkeypatch):
        user_id = 2012
        await db.create_user(User(tg_id=user_id))
        session = SearchSession(user_id=user_id, query="q", content_type=ContentType.MOVIE)
        await db.save_session(user_id, session)

        # Force the cached entry to look 24h+ old without waiting.
        cached_session, cached_json, _ = db._session_cache[user_id]
        db._session_cache[user_id] = (
            cached_session,
            cached_json,
            time.monotonic() - 25 * 60 * 60,
        )

        calls = await self._execute_calls(db, monkeypatch)
        retrieved = await db.get_session(user_id)
        assert retrieved is not None
        assert any("session_data" in sql for sql in calls)  # expired entry forced a re-read

    async def test_cache_eviction_caps_size(self, db):
        """Cache is bounded to ~50 entries; the oldest is evicted on overflow."""
        db._session_cache_cap = 3
        for i in range(5):
            user_id = 3000 + i
            await db.create_user(User(tg_id=user_id))
            session = SearchSession(user_id=user_id, query=f"q{i}", content_type=ContentType.MOVIE)
            await db.save_session(user_id, session)

        assert len(db._session_cache) == 3
        # Oldest two (3000, 3001) evicted; newest three remain.
        assert 3000 not in db._session_cache
        assert 3001 not in db._session_cache
        assert 3002 in db._session_cache
        assert 3003 in db._session_cache
        assert 3004 in db._session_cache

    async def test_concurrent_mutations_under_lock_keep_cache_and_db_in_sync(self, db):
        """Two locked read-modify-write cycles (DB-02 pattern) leave the cache
        holding exactly what SQLite has, even though each save_session call
        writes through to cache immediately under _write_lock."""
        user_id = 2013
        await db.create_user(User(tg_id=user_id))
        session = SearchSession(user_id=user_id, query="q", content_type=ContentType.MOVIE)
        await db.save_session(user_id, session)

        async def racer_a():
            async with db.session_lock(user_id):
                s = await db.get_session(user_id)
                await asyncio.sleep(0.02)
                s.current_page = 1
                await db.save_session(user_id, s)

        async def racer_b():
            async with db.session_lock(user_id):
                s = await db.get_session(user_id)
                s.monitor_type = "all"
                await db.save_session(user_id, s)

        await asyncio.gather(racer_a(), racer_b())

        from_cache = await db._cache_get_session(user_id)
        db._cache_invalidate_session(user_id)
        from_db = await db.get_session(user_id)

        assert from_cache.current_page == from_db.current_page
        assert from_cache.monitor_type == from_db.monitor_type
        assert from_cache.query == from_db.query
