"""Database layer using aiosqlite."""

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import aiosqlite
import structlog

from bot.models import (
    ActionLog,
    ActionType,
    ContentType,
    SearchResult,
    SearchSession,
    User,
    UserPreferences,
    UserRole,
)

logger = structlog.get_logger()


class Database:
    """Async SQLite database manager."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._connection: Optional[aiosqlite.Connection] = None
        self._connect_lock = asyncio.Lock()

    async def connect(self) -> None:
        """Connect to the database and initialize tables."""
        async with self._connect_lock:
            if self._connection is not None:
                return

            # Ensure directory exists
            db_dir = os.path.dirname(self.db_path)
            if db_dir:
                Path(db_dir).mkdir(parents=True, exist_ok=True)

            self._connection = await aiosqlite.connect(self.db_path, isolation_level=None)
            self._connection.row_factory = aiosqlite.Row

            # Performance & durability pragmas (DB-02, DB-04)
            await self._connection.execute("PRAGMA journal_mode=WAL")
            await self._connection.execute("PRAGMA foreign_keys=ON")
            await self._connection.execute("PRAGMA synchronous=NORMAL")

            await self._create_tables()
            await self._run_migrations()
            logger.info("Database connected", path=self.db_path)

    async def close(self) -> None:
        """Close database connection."""
        if self._connection:
            await self._connection.close()
            self._connection = None
            logger.info("Database connection closed")

    @property
    def conn(self) -> aiosqlite.Connection:
        """Get database connection."""
        if self._connection is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._connection

    async def _create_tables(self) -> None:
        """Create database tables if they don't exist."""
        await self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                tg_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                role TEXT NOT NULL DEFAULT 'user',
                preferences TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS searches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                query TEXT NOT NULL,
                content_type TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(tg_id)
            );

            CREATE TABLE IF NOT EXISTS search_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                search_id INTEGER NOT NULL,
                results_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (search_id) REFERENCES searches(id)
            );

            CREATE TABLE IF NOT EXISTS actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                action_type TEXT NOT NULL,
                content_type TEXT NOT NULL,
                query TEXT,
                content_title TEXT,
                content_id TEXT,
                release_title TEXT,
                success INTEGER NOT NULL DEFAULT 1,
                error_message TEXT,
                details TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(tg_id)
            );

            CREATE TABLE IF NOT EXISTS sessions (
                user_id INTEGER PRIMARY KEY,
                session_data TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(tg_id)
            );

            CREATE INDEX IF NOT EXISTS idx_searches_user ON searches(user_id);
            CREATE INDEX IF NOT EXISTS idx_searches_created ON searches(created_at);
            CREATE INDEX IF NOT EXISTS idx_search_results_search ON search_results(search_id);
            CREATE INDEX IF NOT EXISTS idx_actions_user ON actions(user_id);
            CREATE INDEX IF NOT EXISTS idx_actions_created ON actions(created_at);
            CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at);
        """)
        await self.conn.commit()

    # ------------------------------------------------------------------
    # Schema migrations (DB-01)
    #
    # The schema version is stored in SQLite's built-in ``PRAGMA user_version``
    # so no extra table is required. Each migration method is idempotent
    # and bumps ``user_version`` to its target version.
    #
    # To add a new migration:
    #   1. Create ``_migrate_to_v<N>`` that applies the schema changes.
    #   2. Append ``if version < N: await self._migrate_to_v<N>()``
    #      inside ``_run_migrations``.
    #   3. ``_migrate_to_v<N>`` must call ``_set_schema_version(N)`` at the end.
    # ------------------------------------------------------------------
    async def _get_schema_version(self) -> int:
        """Return the current schema version via PRAGMA user_version."""
        async with self.conn.execute("PRAGMA user_version") as cursor:
            row = await cursor.fetchone()
            if row is None:
                return 0
            return int(row[0])

    async def _set_schema_version(self, version: int) -> None:
        """Persist schema version via PRAGMA user_version (cannot be parameterized)."""
        # PRAGMA user_version does not accept placeholders; we coerce to int
        # to keep this safe against injection.
        v = int(version)
        await self.conn.execute(f"PRAGMA user_version = {v}")
        await self.conn.commit()

    async def _run_migrations(self) -> None:
        """Apply pending schema migrations in order."""
        version = await self._get_schema_version()
        if version < 1:
            await self._migrate_to_v1()
        if version < 2:
            await self._migrate_to_v2()

    async def _migrate_to_v1(self) -> None:
        """
        Baseline migration — establishes schema-version tracking.

        For fresh databases this is a no-op because ``_create_tables`` has
        already ensured every required table exists. For legacy databases
        that pre-date the migration framework it simply records the version.
        """
        await self._set_schema_version(1)

    async def _migrate_to_v2(self) -> None:
        """
        OBS-06: Add ``details`` TEXT column to the ``actions`` table so we can
        store a JSON blob alongside each action (rejections, fallback_used, etc).

        Safe for both fresh databases (where ``_create_tables`` already added
        the column — ALTER is skipped) and legacy ones (ALTER adds it).
        """
        async with self.conn.execute("PRAGMA table_info(actions)") as cursor:
            cols = {row[1] for row in await cursor.fetchall()}
        if "details" not in cols:
            await self.conn.execute("ALTER TABLE actions ADD COLUMN details TEXT")
            await self.conn.commit()
        await self._set_schema_version(2)

    # User methods
    async def get_user(self, tg_id: int) -> Optional[User]:
        """Get user by Telegram ID."""
        async with self.conn.execute(
            "SELECT * FROM users WHERE tg_id = ?", (tg_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return self._row_to_user(row)
        return None

    async def create_user(self, user: User) -> User:
        """Create a new user."""
        now = datetime.now(timezone.utc).isoformat()
        prefs_json = user.preferences.model_dump_json()

        await self.conn.execute(
            """
            INSERT INTO users (tg_id, username, first_name, role, preferences, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user.tg_id, user.username, user.first_name, user.role.value, prefs_json, now, now),
        )
        await self.conn.commit()

        user.created_at = datetime.fromisoformat(now)
        user.updated_at = datetime.fromisoformat(now)
        return user

    async def update_user_preferences(self, tg_id: int, preferences: UserPreferences) -> None:
        """Update user preferences."""
        now = datetime.now(timezone.utc).isoformat()
        prefs_json = preferences.model_dump_json()

        await self.conn.execute(
            "UPDATE users SET preferences = ?, updated_at = ? WHERE tg_id = ?",
            (prefs_json, now, tg_id),
        )
        await self.conn.commit()

    def _row_to_user(self, row: aiosqlite.Row) -> User:
        """Convert database row to User model.

        BUG-24 / SEC-19: tolerate corrupt preferences JSON — fall back to
        defaults and log a warning instead of crashing the caller.
        """
        raw_prefs = row["preferences"]
        try:
            prefs = (
                UserPreferences(**json.loads(raw_prefs))
                if raw_prefs
                else UserPreferences()
            )
        except Exception as e:
            logger.warning(
                "Corrupt user preferences, using defaults",
                user_id=row["tg_id"],
                error=str(e),
            )
            prefs = UserPreferences()
        try:
            role = UserRole(row["role"])
        except ValueError:
            role = UserRole.USER
        return User(
            tg_id=row["tg_id"],
            username=row["username"],
            first_name=row["first_name"],
            role=role,
            preferences=prefs,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    # Search methods
    async def save_search(
        self, user_id: int, query: str, content_type: ContentType, results: list[SearchResult]
    ) -> int:
        """Save a search and its results. Returns search ID."""
        now = datetime.now(timezone.utc).isoformat()

        await self.conn.execute("BEGIN")
        try:
            # Insert search
            cursor = await self.conn.execute(
                """
                INSERT INTO searches (user_id, query, content_type, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, query, content_type.value, now),
            )
            search_id = cursor.lastrowid
            if search_id is None:
                await self.conn.rollback()
                raise RuntimeError("Failed to insert search record")

            # Insert results
            results_json = json.dumps([r.model_dump(mode="json") for r in results])
            await self.conn.execute(
                """
                INSERT INTO search_results (search_id, results_json, created_at)
                VALUES (?, ?, ?)
                """,
                (search_id, results_json, now),
            )

            await self.conn.commit()
        except Exception:
            await self.conn.rollback()
            raise
        return search_id

    async def get_search_results(self, search_id: int) -> list[SearchResult]:
        """Get search results by search ID."""
        async with self.conn.execute(
            "SELECT results_json FROM search_results WHERE search_id = ?", (search_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                results_data = json.loads(row["results_json"])
                return [SearchResult(**r) for r in results_data]
        return []

    # Session methods
    async def save_session(self, user_id: int, session: SearchSession) -> None:
        """Save or update user session.

        BUG-14: cap ``session.results`` at 500 entries to avoid unbounded
        growth of the stored session JSON (and Telegram's per-message limits).
        """
        now = datetime.now(timezone.utc).isoformat()
        if session.results and len(session.results) > 500:
            session.results = session.results[:500]
        try:
            session_json = session.model_dump_json()
        except Exception as e:
            logger.error("Failed to serialize session", user_id=user_id, error=str(e))
            raise

        await self.conn.execute(
            """
            INSERT INTO sessions (user_id, session_data, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                session_data = excluded.session_data,
                updated_at = excluded.updated_at
            """,
            (user_id, session_json, now, now),
        )
        await self.conn.commit()
        logger.debug("Session saved", user_id=user_id, results_count=len(session.results))

    async def get_session(self, user_id: int) -> Optional[SearchSession]:
        """Get user session."""
        row_data = None
        async with self.conn.execute(
            "SELECT session_data FROM sessions WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                row_data = row["session_data"]

        if row_data:
            try:
                session_data = json.loads(row_data)
                # Use model_validate for proper nested model deserialization
                session = SearchSession.model_validate(session_data)
                logger.debug(
                    "Session loaded",
                    user_id=user_id,
                    results_count=len(session.results),
                    has_selected=session.selected_result is not None,
                )
                return session
            except Exception as e:
                logger.error(
                    "Failed to deserialize session",
                    user_id=user_id,
                    error=str(e),
                    session_preview=row_data[:200] if row_data else None,
                )
                # Delete corrupted session
                await self.delete_session(user_id)
                return None
        return None

    async def delete_session(self, user_id: int) -> None:
        """Delete user session."""
        await self.conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        await self.conn.commit()

    # Action log methods
    async def log_action(self, action: ActionLog) -> int:
        """Log an action. Returns action ID."""
        now = datetime.now(timezone.utc).isoformat()

        cursor = await self.conn.execute(
            """
            INSERT INTO actions (
                user_id, action_type, content_type, query, content_title,
                content_id, release_title, success, error_message, details, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                action.user_id,
                action.action_type.value,
                action.content_type.value,
                action.query,
                action.content_title,
                action.content_id,
                action.release_title,
                1 if action.success else 0,
                action.error_message,
                action.details,
                now,
            ),
        )
        await self.conn.commit()
        return cursor.lastrowid

    async def get_user_actions(self, user_id: int, limit: int = 20) -> list[ActionLog]:
        """Get recent actions for a user."""
        async with self.conn.execute(
            """
            SELECT * FROM actions
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_action(row) for row in rows]

    async def get_all_actions(self, limit: int = 50) -> list[ActionLog]:
        """Get recent actions for all users (admin view)."""
        async with self.conn.execute(
            """
            SELECT * FROM actions
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_action(row) for row in rows]

    def _row_to_action(self, row: aiosqlite.Row) -> ActionLog:
        """Convert database row to ActionLog model."""
        try:
            action_type = ActionType(row["action_type"])
        except ValueError:
            action_type = ActionType.ERROR
        try:
            content_type = ContentType(row["content_type"])
        except ValueError:
            content_type = ContentType.UNKNOWN
        # OBS-06: details column may be absent on legacy rows fetched before migration ran
        try:
            details = row["details"]
        except (IndexError, KeyError):
            details = None
        return ActionLog(
            id=row["id"],
            user_id=row["user_id"],
            action_type=action_type,
            content_type=content_type,
            query=row["query"],
            content_title=row["content_title"],
            content_id=row["content_id"],
            release_title=row["release_title"],
            success=bool(row["success"]),
            error_message=row["error_message"],
            details=details,
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    # Utility methods
    async def cleanup_old_sessions(self, hours: int = 24) -> int:
        """Delete sessions older than specified hours. Returns count deleted."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

        cursor = await self.conn.execute(
            "DELETE FROM sessions WHERE updated_at < ?", (cutoff,)
        )
        await self.conn.commit()
        return cursor.rowcount

    async def cleanup_old_searches(self, days: int = 7) -> int:
        """Delete searches older than specified days. Returns count deleted."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        await self.conn.execute("BEGIN")
        try:
            # First delete related results
            await self.conn.execute(
                """
                DELETE FROM search_results
                WHERE search_id IN (SELECT id FROM searches WHERE created_at < ?)
                """,
                (cutoff,),
            )

            # Then delete searches
            cursor = await self.conn.execute(
                "DELETE FROM searches WHERE created_at < ?", (cutoff,)
            )
            await self.conn.commit()
        except Exception:
            await self.conn.rollback()
            raise
        return cursor.rowcount
