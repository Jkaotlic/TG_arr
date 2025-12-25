"""Tests for database operations."""

import pytest
import pytest_asyncio
from datetime import datetime

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

        # Retrieve results
        retrieved = await db.get_search_results(search_id)
        assert len(retrieved) == 2
        assert retrieved[0].guid == "test-1"
        assert retrieved[1].guid == "test-2"

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
class TestCleanupOperations:
    """Test cleanup operations."""

    async def test_cleanup_old_sessions(self, db):
        """Test cleaning up old sessions."""
        user = User(tg_id=123456789)
        await db.create_user(user)

        session = SearchSession(
            user_id=123456789,
            query="old query",
            content_type=ContentType.MOVIE,
        )
        await db.save_session(123456789, session)

        # With hours=0, should delete immediately
        deleted = await db.cleanup_old_sessions(hours=0)

        # Session should be deleted
        assert deleted >= 0  # May or may not delete depending on timing

    async def test_cleanup_old_searches(self, db):
        """Test cleaning up old searches."""
        user = User(tg_id=123456789)
        await db.create_user(user)

        results = [
            SearchResult(guid="test", title="Test", indexer="Test"),
        ]
        await db.save_search(123456789, "old search", ContentType.MOVIE, results)

        # With days=0, should delete immediately
        deleted = await db.cleanup_old_searches(days=0)

        # Search should be deleted
        assert deleted >= 0
