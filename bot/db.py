"""Database layer using aiosqlite."""

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

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
        # RACE-02 / DB-01: ONE shared autocommit connection cannot hold two
        # logical transactions at once. Serialize every writer (incl. single
        # commits) so one coroutine's commit/rollback can't terminate another's
        # explicit BEGIN..commit block. Must not be held across nested writes.
        self._write_lock = asyncio.Lock()
        # DB-02: per-user locks for the "get_session -> mutate -> save_session"
        # read-modify-write pattern used by handlers. `_write_lock` only
        # serializes the SQL statement itself, not the read-then-write cycle
        # around it — two concurrent callbacks from the same user (double-tap)
        # can both read the same session, mutate different fields, and the
        # second save_session silently clobbers the first. Lazily created, one
        # Lock per user_id, kept for the process lifetime (bounded by the
        # small number of distinct Telegram users this bot serves).
        self._session_locks: dict[int, asyncio.Lock] = {}

    def session_lock(self, user_id: int) -> asyncio.Lock:
        """Return the per-user lock guarding session read-modify-write cycles.

        Handlers wrap their `get_session -> mutate -> save_session/
        update_session` sequence in `async with db.session_lock(user_id):` so
        concurrent callbacks from the same user serialize instead of racing.
        """
        lock = self._session_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[user_id] = lock
        return lock

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

            # Performance & durability pragmas (DB-02, DB-04, DB-13, PERF-12)
            await self._connection.execute("PRAGMA journal_mode=WAL")
            await self._connection.execute("PRAGMA foreign_keys=ON")
            await self._connection.execute("PRAGMA synchronous=NORMAL")
            # DB-13: SD-card on rpie4 sometimes blocks for >1s on fsync; without
            # busy_timeout concurrent callbacks raise SQLITE_BUSY, surfacing as
            # "поиск временно недоступен" to the user.
            await self._connection.execute("PRAGMA busy_timeout=5000")
            # PERF-12: cap WAL growth so checkpointing happens predictably.
            await self._connection.execute("PRAGMA wal_autocheckpoint=200")
            await self._connection.execute("PRAGMA temp_store=MEMORY")
            await self._connection.execute("PRAGMA mmap_size=33554432")

            await self._create_tables()
            await self._run_migrations()
            logger.info("Database connected", path=self.db_path)

    async def close(self) -> None:
        """Close database connection.

        DB-04: checkpoint the WAL with TRUNCATE before closing so the
        bot.db-wal sidecar file doesn't persist/grow across restarts (SD-card
        wear on rpie4). Best-effort — a checkpoint failure must not prevent
        the connection from closing.
        """
        if self._connection:
            try:
                await self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception as e:
                logger.warning("WAL checkpoint on close failed", error=str(e))
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
                result_count INTEGER NOT NULL DEFAULT 0,
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

            CREATE TABLE IF NOT EXISTS allowed_users (
                tg_id INTEGER PRIMARY KEY,
                added_by INTEGER,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_searches_user ON searches(user_id);
            CREATE INDEX IF NOT EXISTS idx_searches_created ON searches(created_at);
            CREATE INDEX IF NOT EXISTS idx_search_results_search ON search_results(search_id);
            CREATE INDEX IF NOT EXISTS idx_actions_created ON actions(created_at);
            CREATE INDEX IF NOT EXISTS idx_actions_user_created ON actions(user_id, created_at DESC);
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
        if version < 3:
            await self._migrate_to_v3()

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

    async def _migrate_to_v3(self) -> None:
        """
        DB-06: composite index for the ``get_user_actions`` hot path
        (``WHERE user_id = ? ORDER BY created_at DESC``); the old single-column
        ``idx_actions_user`` is superseded and dropped.

        DB-03: add ``result_count`` to ``searches`` for legacy databases (fresh
        ones already have it via ``_create_tables``); ``save_search`` no longer
        writes to ``search_results``.
        """
        async with self.conn.execute("PRAGMA table_info(searches)") as cursor:
            cols = {row[1] for row in await cursor.fetchall()}
        if "result_count" not in cols:
            await self.conn.execute(
                "ALTER TABLE searches ADD COLUMN result_count INTEGER NOT NULL DEFAULT 0"
            )

        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_actions_user_created "
            "ON actions(user_id, created_at DESC)"
        )
        await self.conn.execute("DROP INDEX IF EXISTS idx_actions_user")
        await self.conn.commit()
        await self._set_schema_version(3)

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

        async with self._write_lock:
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

        async with self._write_lock:
            await self.conn.execute(
                "UPDATE users SET preferences = ?, updated_at = ? WHERE tg_id = ?",
                (prefs_json, now, tg_id),
            )
            await self.conn.commit()

    async def update_user_preference(self, user_id: int, key: str, value: Any) -> bool:
        """DB-05: point-update a single preference key without a read-modify-write.

        ``update_user_preferences`` overwrites the *entire* ``preferences`` JSON
        with a snapshot the caller fetched earlier; two concurrent settings
        changes (fast double-tap on different menu items) race and the loser's
        edit is silently lost. This uses SQLite's JSON1 ``json_set`` to patch a
        single key in place, so two concurrent calls on *different* keys both
        survive regardless of ordering.

        ``value`` is JSON-encoded by the caller's data (via ``json.dumps``) and
        passed through SQLite's ``json()`` so it is stored as a native JSON
        value (not a doubly-quoted string) — this covers numbers, strings and
        ``null`` alike.

        Returns True if a row was updated (user existed), False otherwise.
        """
        now = datetime.now(timezone.utc).isoformat()
        value_json = json.dumps(value)

        async with self._write_lock:
            cursor = await self.conn.execute(
                "UPDATE users SET preferences = json_set(preferences, '$.' || ?, json(?)), "
                "updated_at = ? WHERE tg_id = ?",
                (key, value_json, now, user_id),
            )
            await self.conn.commit()
            return cursor.rowcount > 0

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

    # Runtime allowlist methods (feature #6)
    async def add_allowed_user(self, tg_id: int, added_by: int) -> None:
        """Grant a user runtime access (persisted, survives restarts)."""
        now = datetime.now(timezone.utc).isoformat()
        async with self._write_lock:
            await self.conn.execute(
                "INSERT INTO allowed_users (tg_id, added_by, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(tg_id) DO NOTHING",
                (tg_id, added_by, now),
            )
            await self.conn.commit()

    async def remove_allowed_user(self, tg_id: int) -> None:
        """Revoke a user's runtime access.

        DB-09: also drop the user's active session — access is revoked but a
        stale session would otherwise linger until the 24h cleanup sweep,
        keeping search state around for a user who should no longer be able
        to act on it. ``users``/``actions`` rows are intentionally kept for
        history.
        """
        async with self._write_lock:
            await self.conn.execute("DELETE FROM allowed_users WHERE tg_id = ?", (tg_id,))
            await self.conn.execute("DELETE FROM sessions WHERE user_id = ?", (tg_id,))
            await self.conn.commit()

    async def is_allowed_in_db(self, tg_id: int) -> bool:
        """Whether a user was granted access at runtime (DB allowlist)."""
        async with self.conn.execute(
            "SELECT 1 FROM allowed_users WHERE tg_id = ?", (tg_id,)
        ) as cursor:
            return await cursor.fetchone() is not None

    async def list_allowed_users(self) -> list[int]:
        """All runtime-granted user IDs (oldest first)."""
        async with self.conn.execute(
            "SELECT tg_id FROM allowed_users ORDER BY created_at"
        ) as cursor:
            return [row[0] for row in await cursor.fetchall()]

    # Search methods
    async def save_search(
        self, user_id: int, query: str, content_type: ContentType, results: list[SearchResult]
    ) -> int:
        """Save search metadata (query, content_type, result_count). Returns search ID.

        DB-03: ``results`` is no longer serialized to the write-only
        ``search_results`` table — the same payload is already persisted in
        ``sessions`` (search.py) and nothing ever reads ``search_results``
        back. Only the result *count* is worth keeping for history purposes.
        The ``results`` parameter is kept (rather than dropped) so existing
        call sites stay source-compatible; only ``len(results)`` is used.
        """
        now = datetime.now(timezone.utc).isoformat()

        async with self._write_lock:
            cursor = await self.conn.execute(
                """
                INSERT INTO searches (user_id, query, content_type, result_count, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, query, content_type.value, len(results), now),
            )
            await self.conn.commit()
            search_id = cursor.lastrowid
            if search_id is None:
                raise RuntimeError("Failed to insert search record")
            return search_id

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
            logger.error("Failed to serialize session", user_id=user_id, error=str(e), exc_info=True)
            raise

        async with self._write_lock:
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

    async def update_session(self, user_id: int, session: SearchSession) -> bool:
        """RACE-04: persist a session edit WITHOUT recreating it.

        Unlike ``save_session`` (INSERT ... ON CONFLICT), this is UPDATE-only —
        if the row was deleted by a concurrent Cancel/grab while a slow callback
        was running its lookups, the update affects 0 rows and we return False so
        the caller can abort instead of resurrecting a session the user dropped.
        Returns True when the session row still existed and was updated.
        """
        now = datetime.now(timezone.utc).isoformat()
        if session.results and len(session.results) > 500:
            session.results = session.results[:500]
        try:
            session_json = session.model_dump_json()
        except Exception as e:
            logger.error("Failed to serialize session", user_id=user_id, error=str(e), exc_info=True)
            raise

        async with self._write_lock:
            cursor = await self.conn.execute(
                "UPDATE sessions SET session_data = ?, updated_at = ? WHERE user_id = ?",
                (session_json, now, user_id),
            )
            await self.conn.commit()
            return cursor.rowcount > 0

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
                    exc_info=True,
                )
                # Delete corrupted session
                await self.delete_session(user_id)
                return None
        return None

    async def delete_session(self, user_id: int) -> None:
        """Delete user session."""
        async with self._write_lock:
            await self.conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            await self.conn.commit()

    # Action log methods
    async def log_action(self, action: ActionLog) -> int:
        """Log an action. Returns action ID."""
        now = datetime.now(timezone.utc).isoformat()

        async with self._write_lock:
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
            # DB-07: cursor.lastrowid is Optional per DB-API; the contract here
            # is an int action id, so guard against None instead of lying.
            row_id = cursor.lastrowid
            if row_id is None:
                raise RuntimeError("Failed to insert action record")
            return row_id

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

        async with self._write_lock:
            cursor = await self.conn.execute(
                "DELETE FROM sessions WHERE updated_at < ?", (cutoff,)
            )
            await self.conn.commit()
            return cursor.rowcount

    async def cleanup_old_actions(self, days: int = 90) -> int:
        """Delete actions older than specified days. Returns count deleted.

        DB-02: the ``actions`` table otherwise grows unbounded (every search /
        download appends a row). 90 days keeps the /history admin view useful
        while bounding SD-card usage.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        async with self._write_lock:
            cursor = await self.conn.execute(
                "DELETE FROM actions WHERE created_at < ?", (cutoff,)
            )
            await self.conn.commit()
            return cursor.rowcount

    async def cleanup_old_searches(self, days: int = 7) -> int:
        """Delete searches older than specified days. Returns count deleted.

        DB-03: ``save_search`` no longer writes to ``search_results`` (see
        above), but the table remains in the schema until a later migration
        drops it, and legacy rows written before this change may still be
        present. Clean both so pre-existing ``search_results`` data doesn't
        linger forever.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        async with self._write_lock:
            await self.conn.execute("BEGIN")
            try:
                # First delete related legacy results (if any)
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

    # DB-01/DB-08: maintenance (cleanup + optimize + optional backup)
    _BACKUP_KEEP = 3

    async def run_maintenance(self, backup: bool = False) -> dict[str, int]:
        """Periodic maintenance: prune old rows, ask SQLite to re-plan indexes,
        and optionally take an atomic on-disk backup.

        Called from ``bot.main._periodic_cleanup`` (Task E) instead of the
        three separate ``cleanup_old_*`` calls it used to make.

        - Deletes old sessions (24h)/searches (7d)/actions (90d).
        - ``PRAGMA optimize`` — cheap, SQLite-recommended after bulk deletes;
          updates query planner stats without a full ANALYZE.
        - When ``backup=True``: ``VACUUM INTO`` an atomic, defragmented copy
          under ``<db_dir>/backup/bot-YYYYMMDD.db`` (safe to run against a live
          WAL database). Skipped if today's backup already exists (idempotent
          — safe to call more than once on the same day). Keeps only the
          ``_BACKUP_KEEP`` most recent backup files, deleting older ones.

          Note: this only protects against SQLite-level corruption / accidental
          deletes — the backup still lives on the same SD card as the primary
          DB. Copying ``backup/`` off-device (e.g. host cron) is out of scope
          for the bot itself.

        Returns counts: ``{"sessions": n, "searches": n, "actions": n, "backup": 0|1}``.
        """
        sessions = await self.cleanup_old_sessions()
        searches = await self.cleanup_old_searches()
        actions = await self.cleanup_old_actions()

        try:
            await self.conn.execute("PRAGMA optimize")
        except Exception as e:
            logger.warning("PRAGMA optimize failed", error=str(e))

        backup_made = 0
        if backup:
            try:
                backup_made = await self._backup(_keep=self._BACKUP_KEEP)
            except Exception as e:
                logger.error("Database backup failed", error=str(e), exc_info=True)

        result = {
            "sessions": sessions,
            "searches": searches,
            "actions": actions,
            "backup": backup_made,
        }
        logger.info("run_maintenance_completed", **result)
        return result

    async def _backup(self, _keep: int = 3) -> int:
        """VACUUM INTO today's backup file (if not already present) + rotate.

        Returns 1 if a new backup file was created this call, 0 if today's
        backup already existed (no-op, still counts rotation).
        """
        if self.db_path == ":memory:":
            # Nothing to back up for an in-memory database (tests).
            return 0

        db_dir = os.path.dirname(self.db_path) or "."
        backup_dir = Path(db_dir) / "backup"
        backup_dir.mkdir(parents=True, exist_ok=True)

        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        backup_path = backup_dir / f"bot-{today}.db"

        created = 0
        if backup_path.exists():
            logger.debug("Backup for today already exists, skipping", path=str(backup_path))
        else:
            # VACUUM INTO requires the target not to exist and works against a
            # live WAL database without blocking writers for long.
            async with self._write_lock:
                await self.conn.execute(f"VACUUM INTO '{backup_path.as_posix()}'")
            created = 1
            logger.info("Database backup created", path=str(backup_path))

        # Rotate: keep only the _keep most recent bot-YYYYMMDD.db files.
        existing = sorted(backup_dir.glob("bot-*.db"), key=lambda p: p.name, reverse=True)
        for stale in existing[_keep:]:
            try:
                stale.unlink()
                logger.info("Rotated old backup", path=str(stale))
            except OSError as e:
                logger.warning("Failed to remove old backup", path=str(stale), error=str(e))

        return created
