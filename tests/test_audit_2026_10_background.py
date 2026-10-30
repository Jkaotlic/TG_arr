"""Аудит 2026-10-30, задача 7: уведомления, фоновые циклы, БД.

Каждый раздел назван находкой. Время подставляется часами там, где код это
позволяет; настоящих пауз дольше секунды здесь нет.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import aiosqlite
import pytest

from bot.db import Database
from bot.models import ContentType, SearchSession, User

NOW = datetime.now(timezone.utc)


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "bot.db"))
    await database.connect()
    yield database
    await database.close()


# ---------------------------------------------------------------- PERF-04, BUG-08
async def test_every_session_read_parses_the_stored_json(db):
    """PERF-04: кэша нет, значит каждый get_session читает и разбирает строку."""
    await db.create_user(User(tg_id=1))
    await db.save_session(1, SearchSession(user_id=1, query="q", content_type=ContentType.MOVIE))

    with patch(
        "bot.db.SearchSession.model_validate", wraps=SearchSession.model_validate
    ) as validate:
        first = await db.get_session(1)
        second = await db.get_session(1)

    assert validate.call_count == 2
    assert first is not second
    assert not hasattr(db, "_session_cache")


async def test_session_read_racing_a_delete_does_not_raise(db):
    """BUG-08: двойной тап (Отмена и другая кнопка) давал KeyError в кэше."""
    await db.create_user(User(tg_id=1))
    for _ in range(20):
        await db.save_session(1, SearchSession(user_id=1, query="q", content_type=ContentType.MOVIE))
        await asyncio.gather(db.get_session(1), db.delete_session(1), db.get_session(1))
    assert await db.get_session(1) is None


async def test_locked_read_modify_write_still_keeps_both_edits(db):
    """Без кэша дисциплина session_lock та же: две правки одной сессии обе доживают."""
    await db.create_user(User(tg_id=1))
    await db.save_session(1, SearchSession(user_id=1, query="q", content_type=ContentType.MOVIE))

    async def edit_page() -> None:
        async with db.session_lock(1):
            session = await db.get_session(1)
            await asyncio.sleep(0.01)
            session.current_page = 4
            await db.save_session(1, session)

    async def edit_query() -> None:
        async with db.session_lock(1):
            session = await db.get_session(1)
            session.query = "другой"
            await db.save_session(1, session)

    await asyncio.gather(edit_page(), edit_query())
    stored = await db.get_session(1)
    assert stored.current_page == 4
    assert stored.query == "другой"


# ---------------------------------------------------------------- схема v8
#: Схема уведомлений как она лежит на диске у живого бота (v7): дословно из миграций v5 и v7.
_LIVE_SCHEMA_V7 = """
CREATE TABLE music_transfer (
    transfer_id TEXT PRIMARY KEY, batch_key TEXT NOT NULL, track_key TEXT NOT NULL,
    state TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX idx_music_transfer_batch ON music_transfer(batch_key);
CREATE TABLE music_batch (
    batch_key TEXT PRIMARY KEY, title TEXT NOT NULL, first_seen_at TEXT NOT NULL,
    last_change_at TEXT NOT NULL, notified_at TEXT
);
CREATE TABLE notify_kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE live_status (
    key TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
    kind TEXT NOT NULL, title TEXT NOT NULL, arr TEXT NOT NULL, arr_id INTEGER,
    download_id TEXT, state TEXT NOT NULL, progress INTEGER, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, release TEXT, guid TEXT, provider_id TEXT, detail TEXT,
    edited_at TEXT, stalled_since TEXT, emby_tries INTEGER NOT NULL DEFAULT 0
);
"""


async def _write_v7_database(path: Path, *, extra_sql: str = "") -> None:
    raw = await aiosqlite.connect(path)
    try:
        await raw.executescript(_LIVE_SCHEMA_V7 + extra_sql)
        await raw.execute("PRAGMA user_version = 7")
        await raw.commit()
    finally:
        await raw.close()


async def test_migration_to_v8_keeps_live_rows_and_is_idempotent(tmp_path):
    path = tmp_path / "bot.db"
    stamp = NOW.isoformat()
    await _write_v7_database(path)
    raw = sqlite3.connect(path)
    raw.execute(
        "INSERT INTO music_batch VALUES ('album:a', 'A', ?, ?, ?)", (stamp, stamp, stamp),
    )
    raw.execute(
        "INSERT INTO music_transfer VALUES ('t1', 'album:a', 'k', 'Queued, Remotely', ?)", (stamp,),
    )
    raw.execute("INSERT INTO notify_kv VALUES ('import_watermark:radarr', ?)", (stamp,))
    raw.commit()
    raw.close()

    database = Database(str(path))
    await database.connect()
    try:
        assert await database._get_schema_version() == 8
        async with database.conn.execute(
            "SELECT title, notified_at, partial_announced FROM music_batch"
        ) as cursor:
            row = await cursor.fetchone()
        # Существующие батчи не считаются объявленными частично: иначе первый старт
        # разослал бы итоговые сводки по старым застрявшим альбомам.
        assert tuple(row) == ("A", stamp, 0)
        assert await database.kv_get("import_watermark:radarr") == stamp
        assert await database.music_transfer_states() == {"t1": "Queued, Remotely"}
        await database._migrate_to_v8()  # повтор не падает на уже добавленной колонке
    finally:
        await database.close()

    again = Database(str(path))
    await again.connect()
    try:
        assert await again._get_schema_version() == 8
    finally:
        await again.close()


async def test_migration_survives_a_crash_between_alter_and_version_bump(tmp_path):
    path = tmp_path / "bot.db"
    await _write_v7_database(
        path, extra_sql="ALTER TABLE music_batch ADD COLUMN partial_announced INTEGER NOT NULL DEFAULT 0;",
    )
    database = Database(str(path))
    await database.connect()
    try:
        assert await database._get_schema_version() == 8
    finally:
        await database.close()


# ---------------------------------------------------------------- DB-01
def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


async def test_stale_backup_tmp_is_cleared_at_startup(tmp_path):
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    stale = backup_dir / f"bot-{_today()}.db.tmp"
    stale.write_bytes(b"partial")

    database = Database(str(tmp_path / "bot.db"))
    await database.connect()
    try:
        assert not stale.exists()
    finally:
        await database.close()


async def test_partial_tmp_does_not_count_as_todays_backup(db, tmp_path):
    """Обрыв между стартом и бэкапом: .tmp на месте, настоящий файл всё равно делается."""
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    (backup_dir / f"bot-{_today()}.db.tmp").write_bytes(b"partial")

    assert await db._backup(_keep=3) == 1

    final = backup_dir / f"bot-{_today()}.db"
    assert sqlite3.connect(final).execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not (backup_dir / f"bot-{_today()}.db.tmp").exists()


async def test_failed_backup_leaves_no_todays_file_and_next_try_works(db, tmp_path, monkeypatch):
    backup_dir = tmp_path / "backup"
    real_execute = db.conn.execute

    def broken_vacuum(sql: str, *args):
        if sql.startswith("VACUUM INTO"):
            (backup_dir / f"bot-{_today()}.db.tmp").write_bytes(b"half a file")
            raise sqlite3.OperationalError("database or disk is full")
        return real_execute(sql, *args)

    monkeypatch.setattr(db.conn, "execute", broken_vacuum)
    with pytest.raises(sqlite3.OperationalError):
        await db._backup(_keep=3)
    monkeypatch.undo()

    assert not (backup_dir / f"bot-{_today()}.db").exists()
    assert not (backup_dir / f"bot-{_today()}.db.tmp").exists()
    assert await db._backup(_keep=3) == 1
    assert (backup_dir / f"bot-{_today()}.db").exists()


async def test_rotation_does_not_count_tmp_files(db, tmp_path):
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    for day in ("20250101", "20250102", "20250103"):
        (backup_dir / f"bot-{day}.db").write_bytes(b"old")
    (backup_dir / "bot-20250104.db.tmp").write_bytes(b"partial")

    await db._backup(_keep=3)

    remaining = sorted(p.name for p in backup_dir.iterdir())
    # Сегодняшний и два самых новых; .tmp не занимал слот и убран.
    assert remaining == sorted(["bot-20250102.db", "bot-20250103.db", f"bot-{_today()}.db"])


# ---------------------------------------------------------------- DB-02
async def _tombstone(db: Database, key: str, *, age_days: int, state: str, notified: bool = True) -> None:
    when = NOW - timedelta(days=age_days)
    await db.music_record([(f"{key}-t", key, "k", state, key)], when)
    if notified:
        await db.music_mark_notified([key], when)


async def test_announced_batches_older_than_90_days_are_dropped_with_transfers(db):
    await _tombstone(db, "album:old-stuck", age_days=91, state="Queued, Remotely")
    await _tombstone(db, "album:young-stuck", age_days=89, state="Queued, Remotely")
    await _tombstone(db, "album:old-pending", age_days=120, state="Queued, Remotely", notified=False)

    assert await db.cleanup_music_notify() == 1

    states = await db.music_transfer_states()
    assert "album:old-stuck-t" not in states
    assert "album:young-stuck-t" in states
    assert "album:old-pending-t" in states  # не объявлен: его итог ещё впереди


async def test_old_notification_but_recent_change_keeps_the_batch(db):
    await _tombstone(db, "album:a", age_days=120, state="Queued, Remotely")
    await db.music_record([("album:a-t", "album:a", "k", "InProgress", "album:a")], NOW)

    assert await db.cleanup_music_notify() == 0
    assert "album:a-t" in await db.music_transfer_states()
