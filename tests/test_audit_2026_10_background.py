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


# ---------------------------------------------------------------- LOGIC-08
def _server_error(message: str = "Bad Gateway"):
    from aiogram.exceptions import TelegramServerError
    from aiogram.methods import SendMessage

    return TelegramServerError(SendMessage(chat_id=1, text="x"), message)


def _network_error():
    from aiogram.exceptions import TelegramNetworkError
    from aiogram.methods import SendMessage

    return TelegramNetworkError(SendMessage(chat_id=1, text="x"), "Connection reset")


def _forbidden():
    from aiogram.exceptions import TelegramForbiddenError
    from aiogram.methods import SendMessage

    return TelegramForbiddenError(SendMessage(chat_id=1, text="x"), "bot was blocked")


class _RecordedSleep:
    """Подмена asyncio.sleep в bot.main: паузы пишутся, времени не занимают."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import bot.main as main

        self.calls: list[float] = []
        self._real = asyncio.sleep
        monkeypatch.setattr(main.asyncio, "sleep", self)
        monkeypatch.setattr(main, "_last_sent_at", {})

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        await self._real(0)


@pytest.mark.parametrize("make_error", [_server_error, _network_error])
async def test_send_telegram_retries_transient_errors(monkeypatch, make_error):
    from unittest.mock import AsyncMock

    from bot import main

    sleep = _RecordedSleep(monkeypatch)
    bot = AsyncMock()
    bot.send_message.side_effect = [make_error(), make_error(), "delivered"]

    assert await main.send_telegram(bot, 1, "text") == "delivered"
    assert bot.send_message.await_count == 3
    assert sleep.calls == [2, 5]


@pytest.mark.parametrize("make_error", [_server_error, _network_error])
async def test_send_telegram_gives_up_after_three_attempts_and_says_why(monkeypatch, make_error):
    from unittest.mock import AsyncMock

    from structlog.testing import capture_logs

    from bot import main

    sleep = _RecordedSleep(monkeypatch)
    bot = AsyncMock()
    error = make_error()
    bot.send_message.side_effect = error

    with capture_logs() as logs, pytest.raises(type(error)):
        await main.send_telegram(bot, 1, "text")

    assert bot.send_message.await_count == 3
    assert sleep.calls == [2, 5]  # потолок добавленной паузы: 7 с на сообщение
    failures = [e for e in logs if e["event"] == "telegram_send_failed"]
    assert len(failures) == 1
    assert failures[0]["log_level"] == "warning"
    assert failures[0]["error_type"] == type(error).__name__


async def test_send_telegram_does_not_retry_a_final_refusal(monkeypatch):
    from unittest.mock import AsyncMock

    from aiogram.exceptions import TelegramForbiddenError

    from bot import main

    sleep = _RecordedSleep(monkeypatch)
    bot = AsyncMock()
    bot.send_message.side_effect = _forbidden()

    with pytest.raises(TelegramForbiddenError):
        await main.send_telegram(bot, 1, "text")
    assert bot.send_message.await_count == 1
    assert sleep.calls == []


async def test_send_telegram_keeps_its_retry_after_handling(monkeypatch):
    from unittest.mock import AsyncMock

    from aiogram.exceptions import TelegramRetryAfter
    from aiogram.methods import SendMessage

    from bot import main

    sleep = _RecordedSleep(monkeypatch)
    bot = AsyncMock()
    bot.send_message.side_effect = [
        TelegramRetryAfter(SendMessage(chat_id=1, text="x"), "flood", retry_after=11),
        "delivered",
    ]

    assert await main.send_telegram(bot, 1, "text") == "delivered"
    assert sleep.calls == [11]


# ---------------------------------------------------------------- OBS-02
class _HealthClient:
    def __init__(self) -> None:
        self.up = True

    async def check_connection(self):
        return (self.up, "1.0", 1) if self.up else (False, None, None)


class _Admins:
    """Подмена `_notify`: сколько админов взяли сообщение, задаёт тест."""

    def __init__(self) -> None:
        self.delivered = 1
        self.texts: list[str] = []

    async def __call__(self, text: str) -> int:
        self.texts.append(text)
        return self.delivered


def _monitor(client, admins, db=None):
    from bot.services.health_monitor import HealthMonitor

    return HealthMonitor({"Radarr": client}, notify=admins, db=db)


async def test_undelivered_alert_stays_pending_and_is_retried(db):
    from structlog.testing import capture_logs

    client, admins = _HealthClient(), _Admins()
    monitor = _monitor(client, admins, db)
    client.up = False
    admins.delivered = 0

    with capture_logs() as logs:
        await monitor.evaluate()  # первый сбой
        await monitor.evaluate()  # подтверждён, но Telegram лежит

    assert len(admins.texts) == 1
    assert "health_alert_sent" not in [e["event"] for e in logs]
    assert "health_alert_undelivered" in [e["event"] for e in logs]
    assert monitor._open == {}
    assert monitor.needs_recheck
    assert await db.kv_get("health_open") is None

    admins.delivered = 1
    with capture_logs() as logs:
        await monitor.evaluate()
    assert len(admins.texts) == 2
    assert "недоступен" in admins.texts[-1]
    assert "health_alert_sent" in [e["event"] for e in logs]
    assert set(monitor._open) == {"service:Radarr"}
    assert not monitor.needs_recheck

    await monitor.evaluate()  # объявлено: дальше молчание
    assert len(admins.texts) == 2


async def test_undelivered_recovery_is_retried_too(db):
    client, admins = _HealthClient(), _Admins()
    monitor = _monitor(client, admins, db)
    client.up = False
    await monitor.evaluate()
    await monitor.evaluate()

    client.up = True
    admins.delivered = 0
    await monitor.evaluate()
    assert set(monitor._open) == {"service:Radarr"}

    admins.delivered = 1
    await monitor.evaluate()
    assert "снова доступен" in admins.texts[-1]
    assert monitor._open == {}
    assert len([t for t in admins.texts if "снова доступен" in t]) == 2


async def test_problem_gone_before_delivery_is_never_announced_as_recovered(db):
    client, admins = _HealthClient(), _Admins()
    monitor = _monitor(client, admins, db)
    client.up = False
    admins.delivered = 0
    await monitor.evaluate()
    await monitor.evaluate()

    client.up = True
    admins.delivered = 1
    admins.texts.clear()
    await monitor.evaluate()

    assert admins.texts == []
    assert monitor._open == {} and monitor._pending == {}


async def test_open_problem_survives_restart_in_the_stored_shape(db):
    """Форма `health_open` как у живого бота: {key: {text, resolved_text}}."""
    import json

    await db.kv_set(
        "health_open",
        json.dumps({"service:Radarr": {"text": "🔌 Radarr недоступен", "resolved_text": "🔌 Radarr снова доступен"}}),
    )
    client, admins = _HealthClient(), _Admins()
    client.up = False
    monitor = _monitor(client, admins, db)

    await monitor.evaluate()
    assert admins.texts == []  # уже объявлено до рестарта

    client.up = True
    await monitor.evaluate()
    assert len(admins.texts) == 1 and "снова доступен" in admins.texts[0]
    assert json.loads(await db.kv_get("health_open")) == {}


async def test_new_alert_is_stored_in_the_same_shape(db):
    import json

    client, admins = _HealthClient(), _Admins()
    monitor = _monitor(client, admins, db)
    client.up = False
    await monitor.evaluate()
    await monitor.evaluate()

    stored = json.loads(await db.kv_get("health_open"))
    assert stored == {
        "service:Radarr": {"text": "🔌 Radarr недоступен", "resolved_text": "🔌 Radarr снова доступен"},
    }


async def test_notify_raising_counts_as_not_delivered(db):
    client = _HealthClient()

    async def broken(_text: str) -> int:
        raise RuntimeError("boom")

    monitor = _monitor(client, broken, db)
    client.up = False
    await monitor.evaluate()
    await monitor.evaluate()
    assert monitor._open == {} and monitor.needs_recheck


async def test_notify_admins_counts_deliveries(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    from bot import main

    _RecordedSleep(monkeypatch)

    async def send_message(chat_id, *_args, **_kwargs):
        if chat_id == 1:
            raise _forbidden()
        return "ok"

    bot = AsyncMock()
    bot.send_message = send_message

    assert await main.notify_admins(bot, [1, 2], "text", MagicMock()) == 1
    assert await main.notify_admins(bot, [1], "text", MagicMock()) == 0


# ---------------------------------------------------------------- DB-03
async def test_first_maintenance_cycle_runs_a_minute_after_start(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    from bot import main

    sleeps: list[float] = []
    fake_db = MagicMock()
    fake_db.run_maintenance = AsyncMock(return_value={})

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise asyncio.CancelledError
        assert fake_db.run_maintenance.await_count == 0

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)
    await main._periodic_cleanup(fake_db, MagicMock())

    assert sleeps == [60, 6 * 3600]
    fake_db.run_maintenance.assert_awaited_once_with(backup=True)


# ---------------------------------------------------------------- мёртвые фоновые задачи
async def test_dead_background_task_is_logged_at_error():
    from structlog.testing import capture_logs

    from bot import main

    async def dies() -> None:
        raise RuntimeError("упал цикл")

    with capture_logs() as logs:
        task = main._spawn_background(dies(), "library_watch")
        await asyncio.wait([task])
        await asyncio.sleep(0)

    died = [e for e in logs if e["event"] == "background_task_died"]
    assert len(died) == 1
    assert died[0]["log_level"] == "error"
    assert died[0]["task"] == "library_watch"
    assert "упал цикл" in died[0]["error"]


async def test_background_loop_returning_on_its_own_is_logged_at_error():
    from structlog.testing import capture_logs

    from bot import main

    async def quits() -> None:
        return None

    with capture_logs() as logs:
        task = main._spawn_background(quits(), "digest")
        await task
        await asyncio.sleep(0)

    assert [e["task"] for e in logs if e["event"] == "background_task_died"] == ["digest"]


async def test_cancelled_background_task_is_silent_even_if_it_swallows_the_cancel():
    """Наши циклы ловят CancelledError и выходят штатно: это не смерть."""
    from structlog.testing import capture_logs

    from bot import main

    async def swallows() -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            return

    async def plain() -> None:
        await asyncio.sleep(60)

    with capture_logs() as logs:
        tasks = [main._spawn_background(swallows(), "a"), main._spawn_background(plain(), "b")]
        await asyncio.sleep(0)
        await main._cancel_background_tasks(tasks)
        await asyncio.sleep(0)

    assert [e for e in logs if e["event"] == "background_task_died"] == []


async def test_cancel_reports_a_task_that_had_already_died():
    """Исключение уже упавшей задачи не пропадает молча при остановке."""
    from structlog.testing import capture_logs

    from bot import main

    async def dies() -> None:
        raise RuntimeError("давно упал")

    with capture_logs() as logs:
        task = main._spawn_background(dies(), "health_watch")
        await asyncio.wait([task])
        await main._cancel_background_tasks([task])

    assert any(e["event"] == "background_task_died" and "давно упал" in e["error"] for e in logs)


@pytest.mark.parametrize("loop_name", ["_health_watch", "_library_watch", "_digest_watch"])
async def test_failed_setup_does_not_kill_the_loop(monkeypatch, loop_name):
    """Сбой подготовки цикла повторяется на следующем проходе, а не убивает задачу."""
    from unittest.mock import AsyncMock, MagicMock

    from bot import main
    from bot.clients import registry

    get_radarr = AsyncMock(side_effect=[RuntimeError("ещё не готов"), MagicMock()])
    monkeypatch.setattr(registry, "get_radarr", get_radarr)
    for name in ("get_sonarr", "get_prowlarr", "get_lidarr", "get_slskd"):
        monkeypatch.setattr(registry, name, AsyncMock(return_value=None))
    settings = MagicMock(
        timezone="UTC", digest_hour=9, qbittorrent_enabled=False, notify_check_interval=60,
        music_notify_settle_seconds=300, music_notify_stall_seconds=3600,
    )
    monkeypatch.setattr(main, "get_settings", lambda: settings)

    sleeps = 0

    async def fake_sleep(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)
    fake_db = MagicMock()
    fake_db.kv_get = AsyncMock(return_value=None)

    args = (MagicMock(), [], MagicMock(), fake_db) if loop_name == "_health_watch" else (MagicMock(), fake_db, MagicMock())
    await getattr(main, loop_name)(*args)

    assert get_radarr.await_count == 2
