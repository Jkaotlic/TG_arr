"""Database layer using aiosqlite."""

import asyncio
import json
import os
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import aiosqlite
import structlog

from bot.models import (
    ActionLog,
    ActionType,
    ContentType,
    SearchSession,
    User,
    UserPreferences,
)

logger = structlog.get_logger()

#: DB-02: сколько живёт надгробие объявленного батча с незавершёнными файлами.
MUSIC_TOMBSTONE_DAYS = 90


def _as_optional_int(value: Any) -> Optional[int]:
    """Coerce a stored preference id to int, or give up cleanly.

    The Scryer era stored these as **strings on purpose**: its quality-profile
    ids were slugs (`"4k"`) and its root folders had no id at all, so the bot
    used a 12-char sha1 digest of the path. Copying such a value into the
    `Optional[int]` fields it replaced makes the whole `UserPreferences` blob
    fail validation, and `_row_to_user` then falls back to *all* defaults —
    silently discarding the user's Lidarr profile, resolution and auto-grab
    settings too. Since the migration rewrites the row and bumps the schema
    version, that loss is permanent.

    Returning None for anything non-numeric leaves the field unset, which is
    exactly the "no preference yet" state the resolvers already handle.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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
            self._remove_stale_backup_tmp()
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
    def is_connected(self) -> bool:
        return self._connection is not None

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
                -- Колонка role не используется кодом (права — из .env), но остаётся
                -- в схеме: откат на старую версию читает её, DEFAULT закрывает вставки.
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
        if version < 4:
            await self._migrate_to_v4()
        if version < 5:
            await self._migrate_to_v5()
        if version < 6:
            await self._migrate_to_v6()
        if version < 7:
            await self._migrate_to_v7()
        if version < 8:
            await self._migrate_to_v8()

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

    async def _migrate_to_v4(self) -> None:
        """Rollback 2026-08-10 (Task 13, carried-forward item 2): copy the
        legacy combined profile/folder preference keys (written by the two
        weeks the bot ran on the previous backend) into BOTH `radarr_*` and
        `sonarr_*` — the fields `UserPreferences` split them into (Task 12;
        Radarr and Sonarr have independent id spaces, live-measured: both
        start at 1/2 but point at different paths).

        Real stored rows may still carry the old keys. pydantic's
        `extra="ignore"` drops unknown keys silently on load — no crash, but
        the user's chosen profile/folder quietly reverts to whatever "first
        available" resolves to (Radarr's first profile is id 1, "Any", the
        profile whose custom-format scores had to be repaired because they
        rewarded exactly the releases the language policy rejects).

        Idempotent and non-destructive: only fills a `radarr_*`/`sonarr_*`
        field that is still unset (`None`) — an already-explicit new-style
        value is never overwritten. Rows with no legacy keys at all are
        left untouched (no UPDATE issued).

        The two legacy key names read below are an intentional exception to
        this package's "no mention of the previous backend by name" guard
        (`tests/test_r4_C3-dead-clients.py`) — they are read verbatim because
        they must byte-match what that backend's era actually wrote to real
        users' stored preference JSON; renaming the string here would just
        break reading that historical data, not tidy anything. Marked
        `LEGACY-DATA-KEY` so the guard test can carve out exactly these two
        lines rather than exempting the whole file.
        """
        async with self.conn.execute("SELECT tg_id, preferences FROM users") as cursor:
            rows = await cursor.fetchall()

        now = datetime.now(timezone.utc).isoformat()
        async with self._write_lock:
            for row in rows:
                raw = row["preferences"]
                try:
                    data = json.loads(raw) if raw else {}
                except (TypeError, ValueError):
                    continue
                if not isinstance(data, dict):
                    continue

                legacy_profile = _as_optional_int(
                    data.get("scryer_quality_profile_id")  # LEGACY-DATA-KEY
                )
                legacy_folder = _as_optional_int(
                    data.get("scryer_root_folder_id")  # LEGACY-DATA-KEY
                )
                if legacy_profile is None and legacy_folder is None:
                    continue

                changed = False
                if legacy_profile is not None:
                    for key in ("radarr_quality_profile_id", "sonarr_quality_profile_id"):
                        if data.get(key) is None:
                            data[key] = legacy_profile
                            changed = True
                if legacy_folder is not None:
                    for key in ("radarr_root_folder_id", "sonarr_root_folder_id"):
                        if data.get(key) is None:
                            data[key] = legacy_folder
                            changed = True

                if changed:
                    await self.conn.execute(
                        "UPDATE users SET preferences = ?, updated_at = ? WHERE tg_id = ?",
                        (json.dumps(data), now, row["tg_id"]),
                    )
            await self.conn.commit()
        await self._set_schema_version(4)

    async def _migrate_to_v5(self) -> None:
        """Audit 2026-09-23 (NOTIF-01/05): persisted notification state.

        - ``music_transfer`` / ``music_batch`` — per-transfer state and the
          album (batch) it belongs to, so music summaries survive a restart
          and are sent exactly once (see ``bot.services.music_notify``).
        - ``notify_kv`` — small key/value store for watermarks and similar
          notification bookkeeping (``slskd_watermark``, per-service import
          watermarks, open health problems).

        Additive only (``CREATE ... IF NOT EXISTS``): idempotent and never
        touches existing data.
        """
        await self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS music_transfer (
                transfer_id TEXT PRIMARY KEY,
                batch_key TEXT NOT NULL,
                track_key TEXT NOT NULL,
                state TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_music_transfer_batch ON music_transfer(batch_key);

            CREATE TABLE IF NOT EXISTS music_batch (
                batch_key TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_change_at TEXT NOT NULL,
                notified_at TEXT
            );

            CREATE TABLE IF NOT EXISTS notify_kv (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        await self.conn.commit()
        await self._set_schema_version(5)

    async def _migrate_to_v6(self) -> None:
        """2026-09-24 (live): re-key music state from slskd batchId to album folder.

        v5 keyed ``music_batch`` by slskd's ``batchId``, but live slskd gives
        most files their own batchId, so every track became its own "album".
        This moves every v5 transfer to its album-folder key
        (`music_notify.album_key`) and its album-relative track key, both
        derived from the stored v5 ``track_key`` (``<directory>\\<file>``),
        and folds the per-file batch rows into one row per album:

        - ``first_seen_at`` = earliest, ``last_change_at`` = latest;
        - ``notified_at`` is inherited (latest stamp) only if EVERY contributing
          old batch was already announced/seeded — then the whole album is
          already "said" and a re-key must not say it again (the live DB: 16
          per-file batches of one album, all seeded silently at first run and
          still queued). If any contributing batch was still pending, the
          album starts unannounced and gets exactly one summary once settled.

        Idempotent: rows already carrying ``album:`` keys are left alone, so a
        re-run changes nothing. One transaction.
        """
        # Local import: keeps the storage layer free of service imports at
        # module load; music_notify itself only depends on bot.models.
        from bot.services import music_notify

        async with self._write_lock:
            await self.conn.execute("BEGIN")
            try:
                async with self.conn.execute(
                    "SELECT transfer_id, batch_key, track_key FROM music_transfer "
                    "WHERE batch_key NOT LIKE ?",
                    (music_notify.ALBUM_KEY_PREFIX + "%",),
                ) as cursor:
                    legacy = [tuple(row) for row in await cursor.fetchall()]
                sources: dict[str, set[str]] = {}
                titles: dict[str, str] = {}
                for tid, old_batch, old_track in legacy:
                    parts = music_notify.split_path(old_track)
                    filename = parts[-1] if parts else old_track
                    directory = "\\".join(parts[:-1])
                    if music_notify.album_label(directory) is None:
                        # Loose file in a share root: v5 rows carry no peer
                        # name, so keep each in its own old bucket rather
                        # than merge different peers' loose files.
                        new_batch = f"{music_notify.ALBUM_KEY_PREFIX}loose:v5:{old_batch}"
                    else:
                        new_batch = music_notify.album_key(directory)
                    await self.conn.execute(
                        "UPDATE music_transfer SET batch_key = ?, track_key = ? WHERE transfer_id = ?",
                        (new_batch, music_notify.album_track_key(directory, filename), tid),
                    )
                    sources.setdefault(new_batch, set()).add(old_batch)
                    titles.setdefault(new_batch, music_notify.album_title(directory))

                # Read every involved batch row before touching any: one old
                # batch may feed several albums.
                involved = sorted(set().union(*sources.values(), sources)) if sources else []
                batches: dict[str, tuple[str, str, Optional[str]]] = {}
                for key in involved:
                    async with self.conn.execute(
                        "SELECT first_seen_at, last_change_at, notified_at FROM music_batch "
                        "WHERE batch_key = ?",
                        (key,),
                    ) as cursor:
                        row = await cursor.fetchone()
                    if row is not None:
                        batches[key] = (row[0], row[1], row[2])
                await self.conn.executemany(
                    "DELETE FROM music_batch WHERE batch_key = ?", [(key,) for key in involved]
                )

                for new_batch, old_keys in sources.items():
                    rows = [batches[k] for k in sorted(old_keys | {new_batch}) if k in batches]
                    if not rows:
                        continue  # orphan transfers: the next change recreates the batch
                    first_seen = min(r[0] for r in rows)
                    last_change = max(r[1] for r in rows)
                    stamps = [r[2] for r in rows]
                    notified = (
                        None if any(x is None for x in stamps) else max(x for x in stamps if x)
                    )
                    await self.conn.execute(
                        "INSERT INTO music_batch (batch_key, title, first_seen_at, last_change_at, notified_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (new_batch, titles[new_batch], first_seen, last_change, notified),
                    )
                await self.conn.commit()
            except Exception:
                await self.conn.rollback()
                raise
        await self._set_schema_version(6)

    async def _migrate_to_v7(self) -> None:
        """Improvements round 2 (E1): one living status message per grab.

        ``live_status`` holds every message the bot keeps editing in place
        (``bot.services.live_status``): the grab confirmation that follows the
        download, and «в библиотеке» notices waiting for their Emby button.
        Persisted so a restart resumes editing the same message instead of
        sending a new one. Additive and idempotent.
        """
        await self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS live_status (
                key TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                arr TEXT NOT NULL,
                arr_id INTEGER,
                download_id TEXT,
                state TEXT NOT NULL,
                progress INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                release TEXT,
                guid TEXT,
                provider_id TEXT,
                detail TEXT,
                edited_at TEXT,
                stalled_since TEXT,
                emby_tries INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_live_status_download ON live_status(download_id);
            CREATE INDEX IF NOT EXISTS idx_live_status_state ON live_status(state);
        """)
        await self.conn.commit()
        await self._set_schema_version(7)

    async def _migrate_to_v8(self) -> None:
        """LOGIC-20 (аудит 2026-10-30): батч, объявленный с незавершёнными треками.

        ``partial_announced = 1``: альбому ушла частичная сводка, и он получит ещё
        одну, итоговую, когда очередь дойдёт до нуля. Только так его можно отличить
        от батча, засеянного молча при первом запуске: у обоих стоит ``notified_at``.
        Умолчание 0 значит, что ничего объявленного до этой версии итога не получит:
        первый старт на живой базе не разошлёт сводки по старым застрявшим альбомам.

        ALTER с константным умолчанием таблицу не переписывает. Идемпотентна:
        колонку добавляет, только если её нет (сбой между ALTER и записью версии).
        """
        async with self.conn.execute("PRAGMA table_info(music_batch)") as cursor:
            cols = {row[1] for row in await cursor.fetchall()}
        if "partial_announced" not in cols:
            await self.conn.execute(
                "ALTER TABLE music_batch ADD COLUMN partial_announced INTEGER NOT NULL DEFAULT 0"
            )
            await self.conn.commit()
        await self._set_schema_version(8)

    # ------------------------------------------------------------------
    # Living status messages (schema v7)
    # ------------------------------------------------------------------
    _LIVE_COLUMNS = frozenset({
        "key", "chat_id", "message_id", "kind", "title", "arr", "arr_id", "download_id",
        "state", "progress", "created_at", "updated_at", "release", "guid", "provider_id",
        "detail", "edited_at", "stalled_since", "emby_tries",
    })

    async def live_add(self, row: dict[str, Any]) -> None:
        """Insert (or replace) one living-message row."""
        cols = [c for c in row if c in self._LIVE_COLUMNS]
        async with self._write_lock:
            await self.conn.execute(
                f"INSERT OR REPLACE INTO live_status ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' for _ in cols)})",
                tuple(row[c] for c in cols),
            )
            await self.conn.commit()

    async def live_rows(self, states: Optional[Iterable[str]] = None) -> list[dict[str, Any]]:
        """Rows (optionally only those in ``states``), oldest first."""
        params: tuple[str, ...] = ()
        if states is None:
            query = "SELECT * FROM live_status ORDER BY created_at, key"
        else:
            wanted = tuple(states)
            marks = ", ".join("?" for _ in wanted)
            query = f"SELECT * FROM live_status WHERE state IN ({marks}) ORDER BY created_at, key"
            params = wanted
        async with self.conn.execute(query, params) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def live_update(self, key: str, **fields: Any) -> None:
        """Patch named columns of one row (unknown names are rejected)."""
        if not fields:
            return
        unknown = set(fields) - self._LIVE_COLUMNS
        if unknown:
            raise ValueError(f"unknown live_status columns: {sorted(unknown)}")
        sets = ", ".join(f"{name} = ?" for name in fields)
        async with self._write_lock:
            await self.conn.execute(
                f"UPDATE live_status SET {sets} WHERE key = ?", (*fields.values(), key),
            )
            await self.conn.commit()

    async def cleanup_live_status(self, days: int = 7) -> int:
        """Drop rows untouched for ``days`` (terminal ones, and any tracking
        that has gone nowhere for a week)."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        async with self._write_lock:
            cursor = await self.conn.execute(
                "DELETE FROM live_status WHERE updated_at < ?", (cutoff,),
            )
            await self.conn.commit()
            return cursor.rowcount

    # ------------------------------------------------------------------
    # Notification state (schema v5)
    # ------------------------------------------------------------------
    async def kv_get(self, key: str) -> Optional[str]:
        """Read one ``notify_kv`` value, None when unset."""
        async with self.conn.execute("SELECT value FROM notify_kv WHERE key = ?", (key,)) as cursor:
            row = await cursor.fetchone()
        return None if row is None else str(row["value"])

    async def kv_set(self, key: str, value: str) -> None:
        """Insert or replace one ``notify_kv`` value."""
        async with self._write_lock:
            await self.conn.execute(
                "INSERT INTO notify_kv (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            await self.conn.commit()

    async def kv_items(self, prefix: str) -> dict[str, str]:
        """Every ``notify_kv`` entry whose key starts with ``prefix``."""
        async with self.conn.execute(
            "SELECT key, value FROM notify_kv WHERE substr(key, 1, ?) = ?", (len(prefix), prefix),
        ) as cursor:
            rows = await cursor.fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}

    async def kv_delete(self, keys: list[str]) -> None:
        """Remove ``notify_kv`` entries (unknown keys are ignored)."""
        if not keys:
            return
        async with self._write_lock:
            await self.conn.executemany("DELETE FROM notify_kv WHERE key = ?", [(k,) for k in keys])
            await self.conn.commit()

    async def music_transfer_states(self) -> dict[str, str]:
        """All known transfers: ``transfer_id -> state`` (bounded by maintenance)."""
        async with self.conn.execute("SELECT transfer_id, state FROM music_transfer") as cursor:
            rows = await cursor.fetchall()
        return {row["transfer_id"]: row["state"] for row in rows}

    async def music_record(
        self, changes: list[tuple[str, str, str, str, str]], now: datetime
    ) -> None:
        """Apply one poll's transfer changes in a single transaction.

        ``changes`` rows are ``(transfer_id, batch_key, track_key, state,
        batch_title)``. Each upserts the transfer and creates its batch (or
        bumps the batch's ``last_change_at``).

        Batches are album folders (schema v6), so a later re-grab of an album
        already announced lands in the same batch. A transfer *new to the
        database* joining an announced batch reopens it (one more summary once
        it settles, over all of the album's transfers); a state change of a
        known transfer never does — that keeps seeded stuck queues silent.
        """
        if not changes:
            return
        stamp = now.isoformat()
        async with self._write_lock:
            await self.conn.execute("BEGIN")
            try:
                for tid, bkey, tkey, state, title in changes:
                    async with self.conn.execute(
                        "SELECT 1 FROM music_transfer WHERE transfer_id = ?", (tid,)
                    ) as cursor:
                        is_new = await cursor.fetchone() is None
                    await self.conn.execute(
                        "INSERT INTO music_transfer (transfer_id, batch_key, track_key, state, updated_at) "
                        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(transfer_id) DO UPDATE SET "
                        "batch_key = excluded.batch_key, track_key = excluded.track_key, "
                        "state = excluded.state, updated_at = excluded.updated_at",
                        (tid, bkey, tkey, state, stamp),
                    )
                    await self.conn.execute(
                        "INSERT INTO music_batch (batch_key, title, first_seen_at, last_change_at) "
                        "VALUES (?, ?, ?, ?) ON CONFLICT(batch_key) DO UPDATE SET "
                        "last_change_at = excluded.last_change_at, "
                        "notified_at = CASE WHEN ? THEN NULL ELSE notified_at END",
                        (bkey, title, stamp, stamp, int(is_new)),
                    )
                await self.conn.commit()
            except Exception:
                await self.conn.rollback()
                raise

    async def _music_batches(self, where: str) -> list[dict[str, Any]]:
        """Батчи по условию, каждый со своими строками ``(track_key, state)``."""
        async with self.conn.execute(
            "SELECT batch_key, title, first_seen_at, last_change_at FROM music_batch "
            f"WHERE {where} ORDER BY first_seen_at"
        ) as cursor:
            batches = [dict(row) for row in await cursor.fetchall()]
        for batch in batches:
            async with self.conn.execute(
                "SELECT track_key, state FROM music_transfer WHERE batch_key = ?",
                (batch["batch_key"],),
            ) as cursor:
                batch["tracks"] = [(row["track_key"], row["state"]) for row in await cursor.fetchall()]
            batch["last_change_at"] = datetime.fromisoformat(batch["last_change_at"])
        return batches

    async def music_pending_batches(self) -> list[dict[str, Any]]:
        """Batches not yet announced, each with its ``(track_key, state)`` rows."""
        return await self._music_batches("notified_at IS NULL")

    async def music_partial_batches(self) -> list[dict[str, Any]]:
        """LOGIC-20: батчи, которым ушла частичная сводка и ещё должна уйти итоговая."""
        return await self._music_batches("notified_at IS NOT NULL AND partial_announced = 1")

    async def music_mark_notified(
        self, batch_keys: list[str], now: datetime, *, partial: Iterable[str] = (),
    ) -> None:
        """Отметить батчи объявленными. Ключи из ``partial`` ждут итоговой сводки."""
        if not batch_keys:
            return
        stamp = now.isoformat()
        waiting_final = set(partial)
        async with self._write_lock:
            await self.conn.executemany(
                "UPDATE music_batch SET notified_at = ?, partial_announced = ? WHERE batch_key = ?",
                [(stamp, int(key in waiting_final), key) for key in batch_keys],
            )
            await self.conn.commit()

    async def music_touch_pending(self, now: datetime) -> None:
        """Restart the settle/stall timers of every not-yet-announced batch.

        Used after a stretch of unobserved time (slskd unreachable, bot down):
        those hours say nothing about whether an album is stuck.
        """
        async with self._write_lock:
            await self.conn.execute(
                "UPDATE music_batch SET last_change_at = ? WHERE notified_at IS NULL",
                (now.isoformat(),),
            )
            await self.conn.commit()

    async def music_albums_notified_since(self, since: datetime) -> int:
        """E3 digest: albums whose summary went out since ``since`` and that
        got at least one file (seeded or all-cancelled batches don't count)."""
        async with self.conn.execute(
            "SELECT COUNT(*) FROM music_batch b WHERE b.notified_at >= ? AND EXISTS ("
            "SELECT 1 FROM music_transfer t WHERE t.batch_key = b.batch_key "
            "AND t.state LIKE 'Completed, Succeeded%')",
            (since.isoformat(),),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def cleanup_music_notify(self, days: int = 7, tombstone_days: int = MUSIC_TOMBSTONE_DAYS) -> int:
        """Drop batches announced more than ``days`` ago, with their transfers.

        Only batches whose transfers are all finished (state ``Completed, …``)
        are dropped: a batch still holding a queued/in-progress transfer is
        kept as a tombstone, or the watcher would see that transfer as new
        and announce the same stuck queue again every week. A batch still owed
        its final summary (``partial_announced``) is kept too.

        DB-02 (аудит 2026-10-30): надгробие живёт ``tombstone_days`` с последнего
        изменения и объявления, иначе ``music_transfer`` растёт без предела, а эта
        таблица читается целиком на каждом опросе. Очередь, которую slskd держит
        дольше, заново не объявляется: её отсеивает `LibraryWatcher._poll_transfers`
        по ``requested_at``.
        """
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(days=days)).isoformat()
        tombstone_cutoff = (now - timedelta(days=tombstone_days)).isoformat()
        stale = (
            "SELECT b.batch_key FROM music_batch b WHERE b.notified_at IS NOT NULL "
            "AND b.notified_at < ? AND ((b.partial_announced = 0 AND NOT EXISTS ("
            "SELECT 1 FROM music_transfer t "
            "WHERE t.batch_key = b.batch_key AND t.state NOT LIKE 'Completed%')) "
            "OR (b.notified_at < ? AND b.last_change_at < ?))"
        )
        params = (cutoff, tombstone_cutoff, tombstone_cutoff)
        async with self._write_lock:
            await self.conn.execute("BEGIN")
            try:
                await self.conn.execute(
                    f"DELETE FROM music_transfer WHERE batch_key IN ({stale})", params
                )
                cursor = await self.conn.execute(
                    f"DELETE FROM music_batch WHERE batch_key IN ({stale})", params
                )
                await self.conn.commit()
            except Exception:
                await self.conn.rollback()
                raise
            return cursor.rowcount

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
                INSERT INTO users (tg_id, username, first_name, preferences, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user.tg_id, user.username, user.first_name, prefs_json, now, now),
            )
            await self.conn.commit()

        user.created_at = datetime.fromisoformat(now)
        user.updated_at = datetime.fromisoformat(now)
        return user


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
        return User(
            tg_id=row["tg_id"],
            username=row["username"],
            first_name=row["first_name"],
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

    # DEAD-02 (2026-07-30): `save_search` is gone. No production code had
    # called it in a long time — only its own tests did, and the `searches`
    # table was empty on the live database. Every search is already recorded in
    # `actions` (ActionType.SEARCH, with the query), which is what /history
    # reads, so the table only ever duplicated that trail. The table and
    # `cleanup_old_searches` stay: dropping a table needs a migration and buys
    # nothing, and the cleanup is one DELETE per maintenance pass that keeps any
    # pre-existing rows from lingering.

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
        """Get user session.

        PERF-04, BUG-08 (аудит 2026-10-30): кэша нет. Глубокая копия модели на
        каждое попадание обходилась дороже разбора JSON, а `await` между чтением
        записи и `del` давал KeyError при двойном тапе. Каждый вызов возвращает
        новый объект; цикл чтение-правка-запись защищает `session_lock`, как и раньше.
        """
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
        - When ``backup=True``: ``VACUUM INTO`` a defragmented copy under
          ``<db_dir>/backup/bot-YYYYMMDD.db``, written to ``.tmp`` and moved into
          place (safe to run against a live WAL database; DB-01: an interrupted
          run can't pass for a backup). Skipped if today's backup already exists (idempotent
          — safe to call more than once on the same day). Keeps only the
          ``_BACKUP_KEEP`` most recent backup files, deleting older ones.

          Note: this only protects against SQLite-level corruption / accidental
          deletes — the backup still lives on the same SD card as the primary
          DB. Copying ``backup/`` off-device (e.g. host cron) is out of scope
          for the bot itself.

        Returns counts: ``{"sessions", "searches", "actions", "music_batches", "live_status", "backup"}``;
        ``music_batches`` = music notification batches announced over 7 days ago.
        """
        sessions = await self.cleanup_old_sessions()
        searches = await self.cleanup_old_searches()
        actions = await self.cleanup_old_actions()
        music_batches = await self.cleanup_music_notify()
        live = await self.cleanup_live_status()

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
            "music_batches": music_batches,
            "live_status": live,
            "backup": backup_made,
        }
        logger.info("run_maintenance_completed", **result)
        return result

    def _backup_dir(self) -> Path:
        return Path(os.path.dirname(self.db_path) or ".") / "backup"

    def _remove_stale_backup_tmp(self) -> None:
        """DB-01: недописанные копии после обрыва питания или полной карты. Не бэкапы."""
        if self.db_path == ":memory:":
            return
        for leftover in self._backup_dir().glob("bot-*.db.tmp"):
            try:
                leftover.unlink()
                logger.warning("Removed interrupted backup", path=str(leftover))
            except OSError as e:
                logger.warning("Failed to remove interrupted backup", path=str(leftover), error=str(e))

    async def _backup(self, _keep: int = 3) -> int:
        """VACUUM INTO today's backup file (if not already present) + rotate.

        Returns 1 if a new backup file was created this call, 0 if today's
        backup already existed (no-op, still counts rotation).

        DB-01: сначала ``bot-YYYYMMDD.db.tmp``, затем ``os.replace``. Файл без
        ``.tmp`` появляется только целиком, поэтому проверка ``exists()`` ему верит,
        а маска ротации ``bot-*.db`` недописанных копий не видит.
        """
        if self.db_path == ":memory:":
            # Nothing to back up for an in-memory database (tests).
            return 0

        backup_dir = self._backup_dir()
        backup_dir.mkdir(parents=True, exist_ok=True)

        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        backup_path = backup_dir / f"bot-{today}.db"

        created = 0
        if backup_path.exists():
            logger.debug("Backup for today already exists, skipping", path=str(backup_path))
        else:
            # VACUUM INTO не пишет поверх существующего файла: прошлая неудача в этом
            # же процессе иначе ломала бы все следующие попытки.
            self._remove_stale_backup_tmp()
            tmp_path = backup_dir / f"bot-{today}.db.tmp"
            # VACUUM INTO works against a live WAL database without blocking
            # writers for long.
            #
            # SEC-R6-02: VACUUM INTO does not accept a bound parameter, so the
            # path must be inlined — but a bare f-string breaks (and would let
            # the tail of the path be read as SQL) as soon as the resolved
            # DATABASE_PATH contains a single quote. Escape it the SQL way by
            # doubling every quote inside the literal.
            target = tmp_path.as_posix().replace("'", "''")
            try:
                async with self._write_lock:
                    await self.conn.execute(f"VACUUM INTO '{target}'")
                os.replace(tmp_path, backup_path)
            except BaseException:
                tmp_path.unlink(missing_ok=True)
                raise
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
