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


# ---------------------------------------------------------------- LOGIC-09
T0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
HASH_A = "a" * 40
HASH_B = "b" * 40


class _Clock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _maindata(rid: int = 1, torrents: dict | None = None, *, full: bool = False, removed: list | None = None) -> dict:
    return {"rid": rid, "torrents": torrents or {}, "torrents_removed": removed or [], "full_update": full}


def _done(name: str = "Film", finished: datetime | None = None, **extra) -> dict:
    return {
        "name": name, "progress": 1.0, "state": "uploading",
        "completion_on": int(finished.timestamp()) if finished else -1, **extra,
    }


def _busy(name: str = "Film", **extra) -> dict:
    return {"name": name, "progress": 0.4, "state": "downloading", **extra}


def _qbit_service(clock: _Clock, *, subscribers=(111,), **kwargs):
    from unittest.mock import AsyncMock

    from bot.services.notification_service import NotificationService

    qbit, sender = AsyncMock(), AsyncMock(return_value=True)
    service = NotificationService(qbit, sender, clock=clock, **kwargs)
    for user_id in subscribers:
        service.subscribe_user(user_id)
    return service, qbit, sender


async def test_torrent_in_progress_at_restart_notifies_when_it_finishes():
    clock = _Clock()
    service, qbit, sender = _qbit_service(clock)
    qbit.get_maindata.return_value = _maindata(torrents={HASH_A: _busy()}, full=True)
    await service._initial_sync()
    sender.assert_not_awaited()

    clock.advance(60)
    qbit.get_maindata.return_value = _maindata(2, {HASH_A: {"progress": 1.0, "state": "uploading"}})
    await service._check_for_completions()
    assert sender.await_count == 1

    clock.advance(60)
    qbit.get_maindata.return_value = _maindata(3, {HASH_A: {"upspeed": 5}})
    await service._check_for_completions()
    assert sender.await_count == 1  # раздача в каждой дельте не повторяет сообщение


async def test_torrent_added_and_finished_between_polls_notifies_once():
    clock = _Clock()
    service, qbit, sender = _qbit_service(clock)
    qbit.get_maindata.return_value = _maindata(full=True)
    await service._initial_sync()

    clock.advance(300)  # простой: опрос раз в 5 минут
    finished = clock.now - timedelta(seconds=30)
    qbit.get_maindata.return_value = _maindata(2, {HASH_A: _done(finished=finished)})
    await service._check_for_completions()
    assert sender.await_count == 1

    clock.advance(60)
    qbit.get_maindata.return_value = _maindata(3, {HASH_A: {"upspeed": 5}})
    await service._check_for_completions()
    clock.advance(60)
    qbit.get_maindata.return_value = _maindata(4, {HASH_A: _done(finished=finished)}, full=True)
    await service._check_for_completions()
    assert sender.await_count == 1


async def test_clock_skew_does_not_repeat_a_tracked_completion():
    """Часы qBit впереди: completion_on позже прошлого опроса уже после уведомления."""
    clock = _Clock()
    service, qbit, sender = _qbit_service(clock)
    qbit.get_maindata.return_value = _maindata(torrents={HASH_A: _busy()}, full=True)
    await service._initial_sync()

    clock.advance(60)
    ahead = clock.now + timedelta(seconds=90)
    qbit.get_maindata.return_value = _maindata(2, {HASH_A: _done(finished=ahead)})
    await service._check_for_completions()
    clock.advance(60)
    qbit.get_maindata.return_value = _maindata(3, {HASH_A: _done(finished=ahead)})
    await service._check_for_completions()

    assert sender.await_count == 1


async def test_old_or_unknown_completion_first_seen_midrun_stays_silent():
    clock = _Clock()
    service, qbit, sender = _qbit_service(clock)
    qbit.get_maindata.return_value = _maindata(full=True)
    await service._initial_sync()

    clock.advance(60)
    qbit.get_maindata.return_value = _maindata(2, {
        HASH_A: _done(finished=T0 - timedelta(days=30)),
        HASH_B: _done(finished=None),
    })
    await service._check_for_completions()
    sender.assert_not_awaited()


async def test_no_flood_at_first_sync_even_for_a_torrent_finished_a_moment_ago():
    clock = _Clock()
    service, qbit, sender = _qbit_service(clock)
    torrents = {f"{n:040x}": _done(f"Film {n}", finished=clock.now - timedelta(seconds=n)) for n in range(50)}
    qbit.get_maindata.return_value = _maindata(torrents=torrents, full=True)
    await service._initial_sync()
    clock.advance(60)
    qbit.get_maindata.return_value = _maindata(2, full=True, torrents=torrents)
    await service._check_for_completions()
    sender.assert_not_awaited()


async def test_no_flood_when_the_first_successful_sync_is_a_later_poll():
    """qBit лежал при старте: первым успешным чтением становится обычный опрос."""
    clock = _Clock()
    service, qbit, sender = _qbit_service(clock)
    qbit.get_maindata.side_effect = ConnectionError("qbit down")
    await service._initial_sync()

    clock.advance(600)
    qbit.get_maindata.side_effect = None
    qbit.get_maindata.return_value = _maindata(torrents={
        HASH_A: _done(finished=clock.now - timedelta(seconds=5)),
        HASH_B: _busy(),
    }, full=True)
    await service._check_for_completions()
    sender.assert_not_awaited()

    clock.advance(60)
    qbit.get_maindata.return_value = _maindata(2, {HASH_B: {"progress": 1.0, "state": "uploading"}})
    await service._check_for_completions()
    assert sender.await_count == 1


async def test_torrent_finished_during_a_qbit_outage_notifies_after_resync():
    clock = _Clock()
    service, qbit, sender = _qbit_service(clock)
    old = T0 - timedelta(days=3)
    qbit.get_maindata.return_value = _maindata(torrents={HASH_A: _done(finished=old)}, full=True)
    await service._initial_sync()

    clock.advance(1800)
    qbit.get_maindata.return_value = _maindata(1, {
        HASH_A: _done(finished=old),
        HASH_B: _done("Новый", finished=clock.now - timedelta(seconds=600)),
    }, full=True)
    await service._check_for_completions()

    assert sender.await_count == 1
    assert "Новый" in sender.await_args.args[1]


async def test_first_seen_arr_download_is_left_to_library_watch():
    from unittest.mock import MagicMock, patch

    clock = _Clock()
    with patch("bot.services.notification_service.get_settings") as settings:
        settings.return_value = MagicMock(arr_torrent_category_set={"radarr"})
        service, qbit, sender = _qbit_service(clock)
    qbit.get_maindata.return_value = _maindata(full=True)
    await service._initial_sync()

    clock.advance(60)
    qbit.get_maindata.return_value = _maindata(2, {
        HASH_A: _done(finished=clock.now, category="radarr"),
    })
    await service._check_for_completions()
    sender.assert_not_awaited()


async def _live_service(db, clock):
    from bot.services.live_status import LiveStatusService

    async def editor(*_args) -> str:
        return "ok"

    return LiveStatusService(db, editor, clock=clock)


async def test_grabber_gets_no_second_message_after_restart(db):
    """Живое сообщение следит за торрентом: хозяин граба отдельного не получает."""
    from bot.services import live_status

    clock = _Clock()
    live = await _live_service(db, clock)
    await live_status.register_grab(
        db, chat_id=111, message_id=5, kind="movie", title="Film", release="Film", arr="qbit",
        arr_id=None, guid=None, now=clock.now,
    )
    await db.live_update("111:5", download_id=HASH_A.upper(), state=live_status.DOWNLOADING)
    service, qbit, sender = _qbit_service(clock, subscribers=(111, 222), exclude_chats=live.owner_chats)
    qbit.get_maindata.return_value = _maindata(torrents={HASH_A: _busy()}, full=True)
    await service._initial_sync()

    clock.advance(60)
    qbit.get_maindata.return_value = _maindata(2, {HASH_A: {"progress": 1.0, "state": "uploading"}})
    await service._check_for_completions()

    assert [call.args[0] for call in sender.await_args_list] == [222]


async def test_grabber_gets_no_second_message_for_a_first_seen_linked_torrent(db):
    from bot.services import live_status

    clock = _Clock()
    live = await _live_service(db, clock)
    await live_status.register_grab(
        db, chat_id=111, message_id=5, kind="movie", title="Film", release="Film", arr="qbit",
        arr_id=None, guid=None, now=clock.now,
    )
    await db.live_update("111:5", download_id=HASH_A.upper(), state=live_status.DOWNLOADING)
    service, qbit, sender = _qbit_service(clock, subscribers=(111, 222), exclude_chats=live.owner_chats)
    qbit.get_maindata.return_value = _maindata(full=True)
    await service._initial_sync()

    clock.advance(300)
    qbit.get_maindata.return_value = _maindata(2, {HASH_A: _done(finished=clock.now - timedelta(seconds=10))})
    await service._check_for_completions()

    assert [call.args[0] for call in sender.await_args_list] == [222]


async def test_grabber_gets_no_second_message_when_the_living_row_is_not_linked_yet(db):
    """Мгновенный магнет: опрос qBit раньше, чем живое сообщение нашло свой торрент."""
    from bot.clients.qbittorrent import DIRECT_TAG
    from bot.services import live_status

    clock = _Clock()
    live = await _live_service(db, clock)
    await live_status.register_grab(
        db, chat_id=111, message_id=5, kind="movie", title="Film", release="Другое имя", arr="qbit",
        arr_id=None, guid=None, now=clock.now,
    )
    service, qbit, sender = _qbit_service(
        clock, subscribers=(111, 222), exclude_chats=live.owner_chats,
        pending_direct_chats=live.pending_direct_chats,
    )
    qbit.get_maindata.return_value = _maindata(full=True)
    await service._initial_sync()

    clock.advance(20)
    qbit.get_maindata.return_value = _maindata(2, {
        HASH_A: _done(finished=clock.now - timedelta(seconds=5), tags=DIRECT_TAG),
    })
    await service._check_for_completions()

    assert [call.args[0] for call in sender.await_args_list] == [222]


async def test_notification_monitor_task_death_is_logged():
    from unittest.mock import AsyncMock, patch

    from structlog.testing import capture_logs

    clock = _Clock()
    service, _qbit, _sender = _qbit_service(clock)
    with capture_logs() as logs, patch.object(service, "_monitor_loop", AsyncMock(side_effect=RuntimeError("boom"))):
        await service.start()
        await asyncio.wait([service._monitor_task])
        await asyncio.sleep(0)
    assert any(e["event"] == "background_task_died" for e in logs)


# ---------------------------------------------------------------- BUG-19
class _Arr:
    """*arr с настоящим различием между первым чтением и чтением по отметке."""

    def __init__(self) -> None:
        self.records: list[dict] = []
        self.fail: Exception | None = None

    async def get_history(self, **_kwargs) -> list[dict]:
        if self.fail:
            raise self.fail
        return list(self.records)

    async def get_history_since(self, since: datetime, **_kwargs) -> list[dict]:
        if self.fail:
            raise self.fail
        floor = since.replace(microsecond=0)
        return [r for r in self.records if datetime.fromisoformat(r["date"].replace("Z", "+00:00")) >= floor]


def _import_record(rid: int, title: str, when: datetime, download_id: str | None = "D1", **extra) -> dict:
    record = {
        "id": rid, "sourceTitle": title, "downloadId": download_id,
        "date": when.isoformat().replace("+00:00", "Z"), **extra,
    }
    return record


def _library_watcher(db, radarr, sonarr, **kwargs):
    from bot.services.library_watcher import LibraryWatcher

    sent: list[str] = []

    async def notify(text: str, category: str, **extra) -> None:
        sent.append(text)

    return LibraryWatcher(radarr, sonarr, notify=notify, db=db, **kwargs), sent


class _FlakyKv:
    """Сбой записи отметки: БД занята или карта отказала ровно в этот момент."""

    def __init__(self, db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
        self.failing: set[str] = set()
        real = db.kv_set

        async def kv_set(key: str, value: str) -> None:
            if key in self.failing:
                raise sqlite3.OperationalError("database is locked")
            await real(key, value)

        monkeypatch.setattr(db, "kv_set", kv_set)


async def test_db_error_at_watermark_write_neither_raises_nor_loses_the_import(db, monkeypatch):
    radarr, sonarr = _Arr(), _Arr()
    watcher, sent = _library_watcher(db, radarr, sonarr)
    await watcher.poll()  # посев
    await db.kv_set("import_watermark:radarr", T0.isoformat())
    await db.kv_set("import_watermark:sonarr", T0.isoformat())

    kv = _FlakyKv(db, monkeypatch)
    kv.failing.add("import_watermark:radarr")
    radarr.records = [_import_record(1, "Film", T0 + timedelta(minutes=5))]
    await watcher.poll()  # не падает
    assert sent == []

    kv.failing.clear()
    await watcher.poll()
    assert len(sent) == 1 and "Film" in sent[0]
    await watcher.poll()
    assert len(sent) == 1


async def test_db_error_on_one_service_does_not_hold_up_the_other(db, monkeypatch):
    radarr, sonarr = _Arr(), _Arr()
    watcher, sent = _library_watcher(db, radarr, sonarr)
    await watcher.poll()
    await db.kv_set("import_watermark:radarr", T0.isoformat())
    await db.kv_set("import_watermark:sonarr", T0.isoformat())

    kv = _FlakyKv(db, monkeypatch)
    kv.failing.add("import_watermark:sonarr")
    radarr.records = [_import_record(1, "Film", T0 + timedelta(minutes=5))]
    sonarr.records = [_import_record(1, "Show", T0 + timedelta(minutes=5), "D2")]
    await watcher.poll()
    assert len(sent) == 1 and "Film" in sent[0]


async def test_db_error_reading_the_watermark_does_not_raise(db, monkeypatch):
    radarr, sonarr = _Arr(), _Arr()
    watcher, sent = _library_watcher(db, radarr, sonarr)

    async def broken(_key: str):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(db, "kv_get", broken)
    await watcher.poll()
    assert sent == []


async def test_failed_import_step_does_not_stop_the_music_step(db, monkeypatch):
    from bot.services.library_watcher import LibraryWatcher

    calls: list[str] = []

    async def imports(self) -> None:
        calls.append("imports")
        raise sqlite3.OperationalError("database is locked")

    async def transfers(self) -> None:
        calls.append("transfers")
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(LibraryWatcher, "_poll_imports", imports)
    monkeypatch.setattr(LibraryWatcher, "_poll_transfers", transfers)
    watcher, _sent = _library_watcher(db, _Arr(), _Arr())

    await watcher.poll()
    assert calls == ["imports", "transfers"]


class _Slskd:
    def __init__(self) -> None:
        self.transfers: list = []

    async def get_transfers(self, *, include_completed: bool = False) -> list:
        return list(self.transfers)


ALBUM = "music\\Artist - Album (2023) [FLAC]"


def _track(n: int, state: str, ended: datetime | None = None):
    from bot.models import SlskdTransfer

    name = f"{n:02d} - Track {n}.flac"
    return SlskdTransfer(
        username="peer", filename=name, state=state, size=100, id=f"{ALBUM}-{n}", batch_id="b",
        directory=ALBUM, full_filename=f"{ALBUM}\\{name}",
        ended_at=ended if ended is not None else (T0 if "Completed" in state else None),
    )


def _music_watcher(db, clock, slskd):
    async def get_slskd():
        return slskd

    return _library_watcher(
        db, _Arr(), _Arr(), get_slskd=get_slskd, clock=clock, music_settle_seconds=300,
        music_stall_seconds=3600, poll_interval_seconds=3600,
    )


async def test_db_error_before_the_music_mark_retries_and_sends_once(db, monkeypatch):
    clock, slskd = _Clock(), _Slskd()
    watcher, sent = _music_watcher(db, clock, slskd)
    await watcher.poll()

    clock.advance(60)
    slskd.transfers = [_track(1, "Completed, Succeeded", clock.now)]
    await watcher.poll()
    clock.advance(301)

    real_mark = db.music_mark_notified

    async def broken(*_args, **_kwargs) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "music_mark_notified", broken)
    await watcher.poll()  # не падает и не шлёт: отметка раньше отправки
    assert sent == []

    monkeypatch.setattr(db, "music_mark_notified", real_mark)
    clock.advance(60)
    await watcher.poll()
    clock.advance(60)
    await watcher.poll()
    assert len(sent) == 1


async def test_db_error_at_the_seeding_mark_still_seeds_silently(db, monkeypatch):
    clock, slskd = _Clock(), _Slskd()
    watcher, sent = _music_watcher(db, clock, slskd)
    slskd.transfers = [_track(1, "Queued, Remotely")]
    real_mark = db.music_mark_notified

    async def broken(*_args, **_kwargs) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "music_mark_notified", broken)
    await watcher.poll()
    monkeypatch.setattr(db, "music_mark_notified", real_mark)
    await watcher.poll()

    clock.advance(4000)
    await watcher.poll()
    assert sent == []


# ---------------------------------------------------------------- LOGIC-21
async def _seeded_season_watcher(db, *, live=None):
    radarr, sonarr = _Arr(), _Arr()
    watcher, sent = _library_watcher(db, radarr, sonarr, live=live)
    await watcher.poll()
    await db.kv_set("import_watermark:sonarr", T0.isoformat())
    return watcher, sonarr, sent


def _episode(n: int, *, download_id: str | None = "PACK") -> dict:
    return _import_record(100 + n, "Show.S01.1080p", T0 + timedelta(seconds=n), download_id)


async def test_a_pack_imported_across_polls_is_one_message(db):
    watcher, sonarr, sent = await _seeded_season_watcher(db)

    sonarr.records = [_episode(1), _episode(2)]
    await watcher.poll()
    assert sent == []  # пак ещё может расти

    sonarr.records += [_episode(3), _episode(4), _episode(5)]
    await watcher.poll()
    assert sent == []

    await watcher.poll()  # опрос без новых файлов
    assert len(sent) == 1 and "5 серий" in sent[0]

    await watcher.poll()
    assert len(sent) == 1


async def test_a_single_episode_waits_one_poll_only(db):
    watcher, sonarr, sent = await _seeded_season_watcher(db)
    sonarr.records = [_episode(1)]
    await watcher.poll()
    assert sent == []
    await watcher.poll()
    assert len(sent) == 1 and "серий" not in sent[0] and "серии" not in sent[0]


async def test_a_pack_that_keeps_growing_is_released_by_the_deadline(db):
    watcher, sonarr, sent = await _seeded_season_watcher(db)

    for n in range(1, 5):  # файл на каждом опросе: первый опрос + 3
        sonarr.records = [*sonarr.records, _episode(n)]
        await watcher.poll()
    assert len(sent) == 1 and "4 серии" in sent[0]

    sonarr.records = [*sonarr.records, _episode(5)]
    await watcher.poll()
    await watcher.poll()
    assert len(sent) == 2  # остаток своим сообщением, те же файлы не считаются дважды
    assert "серий" not in sent[1] and "серии" not in sent[1]


async def test_a_silent_sonarr_outage_is_not_read_as_a_quiet_poll(db):
    watcher, sonarr, sent = await _seeded_season_watcher(db)
    sonarr.records = [_episode(1)]
    await watcher.poll()

    sonarr.fail = ConnectionError("sonarr asleep")
    await watcher.poll()
    await watcher.poll()
    assert sent == []  # неизвестно, рос ли пак

    await watcher.poll()
    assert len(sent) == 1  # потолок ожидания держит и при лежащем сервисе


async def test_radarr_and_records_without_download_id_are_not_held(db):
    radarr, sonarr = _Arr(), _Arr()
    watcher, sent = _library_watcher(db, radarr, sonarr)
    await watcher.poll()
    await db.kv_set("import_watermark:radarr", T0.isoformat())
    await db.kv_set("import_watermark:sonarr", T0.isoformat())

    radarr.records = [_import_record(1, "Film", T0 + timedelta(minutes=1))]
    sonarr.records = [_episode(1, download_id=None)]
    await watcher.poll()
    assert len(sent) == 2


async def test_held_group_reaches_the_living_message_with_all_its_records(db):
    class _Live:
        def __init__(self) -> None:
            self.claims: list[tuple[str, str, int]] = []

        async def claim_import(self, service: str, download_id: str, records: list[dict]) -> set[int]:
            self.claims.append((service, download_id, len(records)))
            return {111}

    live = _Live()
    watcher, sonarr, sent = await _seeded_season_watcher(db, live=live)
    sonarr.records = [_episode(1)]
    await watcher.poll()
    sonarr.records += [_episode(2)]
    await watcher.poll()
    assert live.claims == []
    await watcher.poll()
    assert live.claims == [("sonarr", "PACK", 2)]


async def test_graceful_stop_announces_held_groups_and_restart_does_not_repeat_them(db):
    watcher, sonarr, sent = await _seeded_season_watcher(db)
    sonarr.records = [_episode(1), _episode(2)]
    await watcher.poll()
    assert sent == []

    await watcher.flush_held()
    assert len(sent) == 1 and "2 серии" in sent[0]
    await watcher.flush_held()
    assert len(sent) == 1

    restarted, later = _library_watcher(db, _Arr(), sonarr)
    await restarted.poll()
    await restarted.poll()
    assert later == []


async def test_crash_while_holding_knowingly_drops_the_group_without_duplicates(db):
    """Удержание в памяти, отметка уже впереди: после падения эти файлы не объявляются
    (решение задачи 7), а следующие файлы пака идут своим сообщением."""
    watcher, sonarr, sent = await _seeded_season_watcher(db)
    sonarr.records = [_episode(1), _episode(2)]
    await watcher.poll()

    restarted, later = _library_watcher(db, _Arr(), sonarr)  # без flush_held: падение
    await restarted.poll()
    await restarted.poll()
    assert later == []

    sonarr.records = [*sonarr.records, _episode(3)]
    await restarted.poll()
    await restarted.poll()
    assert len(later) == 1 and "серии" not in later[0] and "серий" not in later[0]


async def test_library_watch_flushes_held_groups_when_cancelled(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    from bot import main
    from bot.clients import registry
    from bot.services import library_watcher

    for name in ("get_radarr", "get_sonarr", "get_lidarr", "get_slskd"):
        monkeypatch.setattr(registry, name, AsyncMock(return_value=None))
    settings = MagicMock(notify_check_interval=60, music_notify_settle_seconds=300, music_notify_stall_seconds=3600)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    watcher = MagicMock(poll=AsyncMock(), flush_held=AsyncMock())
    monkeypatch.setattr(library_watcher, "LibraryWatcher", MagicMock(return_value=watcher))
    sleeps = 0

    async def fake_sleep(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)
    await main._library_watch(MagicMock(), MagicMock(), MagicMock())

    watcher.flush_held.assert_awaited_once()


# ---------------------------------------------------------------- LOGIC-20
async def _partial_announced(db, clock, slskd):
    """Альбом объявлен частично: 2 скачано, 1 застрял в очереди."""
    watcher, sent = _music_watcher(db, clock, slskd)
    await watcher.poll()
    clock.advance(60)
    slskd.transfers = [
        _track(1, "Completed, Succeeded", clock.now),
        _track(2, "Completed, Succeeded", clock.now),
        _track(3, "Queued, Remotely"),
    ]
    await watcher.poll()
    clock.advance(3700)
    await watcher.poll()
    assert len(sent) == 1 and "2/3 скачано" in sent[0] and "ещё 1 в очереди" in sent[0]
    return watcher, sent


async def test_partial_album_gets_one_final_summary_when_it_finishes(db):
    clock, slskd = _Clock(), _Slskd()
    watcher, sent = await _partial_announced(db, clock, slskd)

    clock.advance(600)
    await watcher.poll()
    assert len(sent) == 1  # очередь стоит: повторов нет

    clock.advance(60)
    slskd.transfers[2] = _track(3, "Completed, Succeeded", clock.now)
    await watcher.poll()
    assert len(sent) == 1  # только что изменилось: ждём успокоения

    clock.advance(301)
    await watcher.poll()
    assert len(sent) == 2 and "3/3 скачано" in sent[1] and "очереди" not in sent[1]

    clock.advance(4000)
    await watcher.poll()
    assert len(sent) == 2


async def test_final_summary_survives_a_restart(db):
    clock, slskd = _Clock(), _Slskd()
    await _partial_announced(db, clock, slskd)

    restarted, later = _music_watcher(db, clock, slskd)
    clock.advance(60)
    slskd.transfers[2] = _track(3, "Completed, Errored", clock.now)
    await restarted.poll()
    clock.advance(301)
    await restarted.poll()
    assert len(later) == 1 and "2/3 скачано" in later[0] and "1 не удалось" in later[0]


async def test_seeded_stuck_batch_stays_silent_when_it_finishes(db):
    clock, slskd = _Clock(), _Slskd()
    watcher, sent = _music_watcher(db, clock, slskd)
    slskd.transfers = [_track(1, "Queued, Remotely")]
    await watcher.poll()  # посев

    clock.advance(4000)
    await watcher.poll()
    clock.advance(60)
    slskd.transfers = [_track(1, "Completed, Succeeded", clock.now)]
    await watcher.poll()
    clock.advance(301)
    await watcher.poll()
    assert sent == []


async def test_album_announced_partially_before_this_version_stays_silent(db):
    """Старая строка: notified_at стоит, флага нет. Итог не приходит: не рассылка на старте."""
    clock, slskd = _Clock(), _Slskd()
    watcher, sent = _music_watcher(db, clock, slskd)
    await watcher.poll()
    clock.advance(60)
    slskd.transfers = [_track(1, "Completed, Succeeded", clock.now), _track(2, "Queued, Remotely")]
    await watcher.poll()
    await db.music_mark_notified([_batch_key(slskd.transfers[0])], clock.now)  # как писала прошлая версия

    clock.advance(60)
    slskd.transfers[1] = _track(2, "Completed, Succeeded", clock.now)
    await watcher.poll()
    clock.advance(301)
    await watcher.poll()
    assert sent == []


def _batch_key(transfer) -> str:
    from bot.services import music_notify

    return music_notify.batch_key(transfer)


async def test_partial_album_whose_rest_was_cancelled_gets_its_final_line(db):
    clock, slskd = _Clock(), _Slskd()
    watcher, sent = await _partial_announced(db, clock, slskd)
    clock.advance(60)
    slskd.transfers[2] = _track(3, "Completed, Cancelled", clock.now)
    await watcher.poll()
    clock.advance(301)
    await watcher.poll()
    assert len(sent) == 2 and "2/2 скачано" in sent[1]


async def test_partial_album_regrabbed_ends_with_one_more_summary(db):
    """Новый файл снимает отметку: дальше обычный путь, без лишней итоговой сводки."""
    clock, slskd = _Clock(), _Slskd()
    watcher, sent = await _partial_announced(db, clock, slskd)
    clock.advance(60)
    slskd.transfers[2] = _track(3, "Completed, Succeeded", clock.now)
    slskd.transfers.append(_track(4, "Completed, Succeeded", clock.now))
    await watcher.poll()
    clock.advance(301)
    await watcher.poll()
    clock.advance(301)
    await watcher.poll()
    assert len(sent) == 2 and "4/4 скачано" in sent[1]


# ---------------------------------------------------------------- LOGIC-07, PERF-02
MSK = timezone(timedelta(hours=3))


def _msk(hour: int, minute: int = 0, day: int = 25) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=MSK)


class _DigestRig:
    """Сводка с подделками всех источников; `users` и часы задаёт тест."""

    def __init__(self, db: Database, when: datetime, users=None, hour: int = 9) -> None:
        from unittest.mock import AsyncMock

        from bot.models import UserPreferences
        from bot.services.digest import DigestService

        self.clock = _Clock(when.astimezone(timezone.utc))
        self.sent: list[tuple[int, str]] = []
        self.radarr, self.sonarr, self.qbit, self.slskd = AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock()
        for client in (self.radarr, self.sonarr):
            client.get_history_since.return_value = []
            client.get_queue.return_value = []
        self.sonarr.get_calendar.return_value = []
        self.qbit.get_torrents.return_value = []
        self.slskd.get_transfers.return_value = []
        self.get_slskd = AsyncMock(return_value=self.slskd)
        self.db = db

        async def recipients():
            return users if users is not None else [(111, UserPreferences())]

        async def send(uid: int, text: str) -> None:
            self.sent.append((uid, text))

        self.service = DigestService(
            db, radarr=self.radarr, sonarr=self.sonarr, qbittorrent=self.qbit, get_slskd=self.get_slskd,
            recipients=recipients, send=send, clock=self.clock, tz=MSK, hour=hour,
        )

    def at(self, when: datetime) -> None:
        self.clock.now = when.astimezone(timezone.utc)

    def imported(self, title: str, when: datetime) -> None:
        self.radarr.get_history_since.return_value = [{
            "id": 1, "sourceTitle": "Rel", "movie": {"title": title},
            "date": when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        }]


async def test_failed_source_before_noon_is_retried_without_marking_the_day(db):
    from structlog.testing import capture_logs

    rig = _DigestRig(db, _msk(9, 1))
    rig.radarr.get_queue.side_effect = ConnectionError("radarr down")

    with capture_logs() as logs:
        assert await rig.service.maybe_send() == 0

    assert rig.sent == []
    assert await db.kv_get("digest_last_date") is None
    failed = [e for e in logs if e["event"] == "digest_source_failed"]
    assert failed and failed[0]["log_level"] == "warning" and failed[0]["source"] == "Radarr"

    rig.radarr.get_queue.side_effect = None
    rig.imported("Dune", _msk(8))
    rig.at(_msk(9, 6))
    assert await rig.service.maybe_send() == 1
    assert "Dune" in rig.sent[0][1] and "Недоступны" not in rig.sent[0][1]
    assert await db.kv_get("digest_last_date") == "2026-09-25"

    rig.at(_msk(9, 11))
    assert await rig.service.maybe_send() == 0


async def test_at_noon_digest_goes_out_with_the_unavailable_sources_and_is_marked(db):
    rig = _DigestRig(db, _msk(11, 59))
    rig.radarr.get_queue.side_effect = ConnectionError("radarr down")
    rig.qbit.get_torrents.side_effect = ConnectionError("qbit down")
    rig.sonarr.get_history_since.return_value = [{
        "id": 1, "series": {"title": "Show"}, "date": _msk(8).astimezone(timezone.utc).isoformat(),
    }]

    assert await rig.service.maybe_send() == 0  # 11:59 по местному времени

    rig.at(_msk(12, 0))
    assert await rig.service.maybe_send() == 1
    text = rig.sent[0][1]
    assert "Show" in text
    assert "Недоступны: qBittorrent, Radarr" in text
    assert await db.kv_get("digest_last_date") == "2026-09-25"

    rig.at(_msk(12, 5))
    assert await rig.service.maybe_send() == 0
    assert len(rig.sent) == 1


async def test_cutoff_is_local_noon_not_utc_noon(db):
    """09:00 UTC это уже 12:00 в Москве: сводка идёт, хотя по UTC ещё утро."""
    rig = _DigestRig(db, datetime(2026, 9, 25, 8, 59, tzinfo=timezone.utc))
    rig.sonarr.get_queue.side_effect = ConnectionError("down")
    assert await rig.service.maybe_send() == 0

    rig.clock.now = datetime(2026, 9, 25, 9, 0, tzinfo=timezone.utc)
    assert await rig.service.maybe_send() == 1
    assert "Недоступны: Sonarr" in rig.sent[0][1]


async def test_the_day_being_retried_is_the_local_one(db):
    """00:30 в Москве: по UTC ещё вчера, но день и полдень считаются по местному времени."""
    await db.kv_set("digest_last_date", "2026-09-25")
    rig = _DigestRig(db, datetime(2026, 9, 25, 21, 30, tzinfo=timezone.utc), hour=0)
    rig.radarr.get_queue.side_effect = ConnectionError("down")

    assert await rig.service.maybe_send() == 0  # местное 26-е, 00:30: до полудня ждём
    assert await db.kv_get("digest_last_date") == "2026-09-25"

    rig.at(_msk(12, 0, day=26))
    await rig.service.maybe_send()
    assert await db.kv_get("digest_last_date") == "2026-09-26"


@pytest.mark.parametrize(
    ("break_source", "name"),
    [
        (lambda r: setattr(r.radarr.get_history_since, "side_effect", ConnectionError("x")), "Radarr"),
        (lambda r: setattr(r.sonarr.get_history_since, "side_effect", ConnectionError("x")), "Sonarr"),
        (lambda r: setattr(r.radarr.get_queue, "side_effect", ConnectionError("x")), "Radarr"),
        (lambda r: setattr(r.sonarr.get_calendar, "side_effect", ConnectionError("x")), "Sonarr"),
        (lambda r: setattr(r.qbit.get_torrents, "side_effect", ConnectionError("x")), "qBittorrent"),
        (lambda r: setattr(r.slskd.get_transfers, "side_effect", ConnectionError("x")), "slskd"),
        (lambda r: setattr(r.get_slskd, "side_effect", ConnectionError("x")), "slskd"),
    ],
)
async def test_every_source_reports_its_failure(db, break_source, name):
    rig = _DigestRig(db, _msk(13))
    break_source(rig)
    assert await rig.service.maybe_send() == 1
    assert f"Недоступны: {name}" in rig.sent[0][1]


async def test_source_named_once_even_if_two_of_its_calls_failed(db):
    rig = _DigestRig(db, _msk(13))
    rig.radarr.get_history_since.side_effect = ConnectionError("x")
    rig.radarr.get_queue.side_effect = ConnectionError("x")
    await rig.service.maybe_send()
    assert "Недоступны: Radarr\n" in rig.sent[0][1] + "\n"


async def test_slskd_not_configured_is_not_a_failure(db):
    rig = _DigestRig(db, _msk(9, 1))
    rig.get_slskd.return_value = None
    await rig.service.maybe_send()
    assert await db.kv_get("digest_last_date") == "2026-09-25"


async def test_quiet_day_with_a_dead_source_says_so_after_noon(db):
    rig = _DigestRig(db, _msk(12))
    rig.radarr.get_queue.side_effect = ConnectionError("x")
    assert await rig.service.maybe_send() == 1
    assert "Недоступны: Radarr" in rig.sent[0][1]


async def test_unavailable_line_respects_muted_categories(db):
    from bot.models import UserPreferences

    users = [
        (111, UserPreferences(notify_library=False)),  # Radarr ему неинтересен
        (222, UserPreferences(notify_music="off")),
        (333, UserPreferences(digest_enabled=False)),
    ]
    rig = _DigestRig(db, _msk(13), users=users)
    rig.radarr.get_queue.side_effect = ConnectionError("x")
    await rig.service.maybe_send()
    assert [uid for uid, _ in rig.sent] == [222]


async def test_db_error_keeps_the_retry_even_after_noon(db, monkeypatch):
    rig = _DigestRig(db, _msk(13))

    async def broken(_since):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "music_albums_notified_since", broken)
    assert await rig.service.maybe_send() == 0
    assert rig.sent == []
    assert await db.kv_get("digest_last_date") is None


async def test_digest_reads_light_history_since_the_window_start(db):
    rig = _DigestRig(db, _msk(9, 5))
    window_start = rig.clock.now - timedelta(hours=24)
    rig.radarr.get_history_since.return_value = [
        {"id": 1, "movie": {"title": "На границе"}, "date": window_start.isoformat()},
        {"id": 2, "movie": {"title": "Старый"}, "date": (window_start - timedelta(seconds=1)).isoformat()},
        {"id": 3, "movie": {"title": "Смещение"}, "date": _msk(8).isoformat()},
    ]

    data = await rig.service.collect()

    assert data.library == ["На границе", "Смещение"]
    rig.radarr.get_history.assert_not_awaited()
    call = rig.radarr.get_history_since.await_args
    assert call.args[0] == window_start
    assert call.kwargs["event_type"] == "downloadFolderImported"
    assert call.kwargs["include_details"] is True


# ---------------------------------------------------------------- PERF-03
class _LiveArr:
    def __init__(self) -> None:
        self.grabbed: list[dict] = []
        self.imported: list[dict] = []
        self.since_calls: list[tuple[datetime, str]] = []
        self.paged_calls = 0
        self.fail = False

    async def get_history(self, **_kwargs):
        self.paged_calls += 1
        return []

    async def get_history_since(self, since: datetime, event_type: str = "downloadFolderImported", **_kwargs):
        self.since_calls.append((since, event_type))
        if self.fail:
            raise ConnectionError("down")
        records = self.grabbed if event_type == "grabbed" else self.imported
        return sorted(records, key=lambda r: r["date"])

    async def get_queue(self):
        return []


async def _live_rig(db, clock, radarr):
    from bot.services.live_status import LiveStatusService

    edits: list[str] = []

    async def editor(_chat, _message, text, _markup=None) -> str:
        edits.append(text)
        return "ok"

    class _Qbit:
        async def get_torrent(self, _hash):
            return None

        async def get_torrents(self, **_kwargs):
            return []

    return LiveStatusService(db, editor, radarr=radarr, qbittorrent=_Qbit(), clock=clock), edits


async def _grab(db, clock, *, message: int = 5, guid: str | None = "https://indexer.example.com/1"):
    from bot.services import live_status

    return await live_status.register_grab(
        db, chat_id=111, message_id=message, kind="movie", title="Film", release="Film.2020",
        arr="radarr", arr_id=7, guid=guid, now=clock.now,
    )


async def test_living_message_asks_only_for_history_since_its_oldest_row(db):
    clock, radarr = _Clock(), _LiveArr()
    service, _edits = await _live_rig(db, clock, radarr)
    await _grab(db, clock, message=5)
    first_created = clock.now
    clock.advance(120)
    await _grab(db, clock, message=6)

    await service.tick()

    assert radarr.paged_calls == 0
    assert len(radarr.since_calls) == 1  # одно чтение на тик, а не на строку
    since, event_type = radarr.since_calls[0]
    assert event_type == "grabbed"
    assert first_created - timedelta(minutes=10) <= since <= first_created - timedelta(seconds=60)


async def test_grab_recorded_just_before_the_row_was_created_still_links(db):
    """*arr пишет `grabbed` раньше, чем бот создаёт строку: граница с запасом."""
    from bot.services import live_status

    clock, radarr = _Clock(), _LiveArr()
    service, _edits = await _live_rig(db, clock, radarr)
    await _grab(db, clock)
    radarr.grabbed = [{
        "id": 1, "downloadId": "ABC", "sourceTitle": "Film.2020",
        "date": (clock.now - timedelta(seconds=30)).isoformat().replace("+00:00", "Z"),
        "data": {"guid": "https://indexer.example.com/1"},
    }]

    await service.tick()

    rows = await db.live_rows()
    assert rows[0]["download_id"] == "ABC"
    assert rows[0]["state"] != live_status.WAITING


async def test_own_search_still_links_the_newest_grab(db):
    """/history/since отдаёт старые первыми, а привязка ждёт новые первыми."""
    clock, radarr = _Clock(), _LiveArr()
    service, _edits = await _live_rig(db, clock, radarr)
    await _grab(db, clock, guid=None)

    def grab(download_id: str, seconds: int) -> dict:
        when = clock.now + timedelta(seconds=seconds)
        return {
            "id": seconds, "downloadId": download_id, "movieId": 7, "sourceTitle": "Film.2020",
            "date": when.isoformat().replace("+00:00", "Z"), "data": {},
        }

    radarr.grabbed = [grab("OLDER", 5), grab("NEWER", 25)]
    await service.tick()

    assert (await db.live_rows())[0]["download_id"] == "NEWER"


async def test_import_of_an_old_row_is_found_in_light_history(db):
    from bot.services import live_status

    clock, radarr = _Clock(), _LiveArr()
    service, edits = await _live_rig(db, clock, radarr)
    await _grab(db, clock)
    created = clock.now
    await db.live_update("111:5", state=live_status.COMPLETED, download_id="ABC")
    clock.advance(6 * 3600)
    radarr.imported = [{
        "id": 1, "downloadId": "abc", "date": (clock.now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
    }]

    await service.tick()

    assert "в библиотеке" in edits[-1]
    assert radarr.since_calls[0][0] <= created


async def test_history_failure_during_a_tick_is_survived(db):
    clock, radarr = _Clock(), _LiveArr()
    service, edits = await _live_rig(db, clock, radarr)
    await _grab(db, clock)
    radarr.fail = True
    await service.tick()
    assert edits == []


# ---------------------------------------------------------------- первый старт после деплоя
class _FirstStart:
    """Все источники уведомлений поверх одной базы; пересоздание объектов = рестарт."""

    def __init__(self, db: Database, clock: _Clock, radarr, sonarr, slskd, health, qbit_data) -> None:
        from unittest.mock import AsyncMock

        from bot.models import UserPreferences
        from bot.services.digest import DigestService
        from bot.services.health_monitor import HealthMonitor
        from bot.services.library_watcher import LibraryWatcher
        from bot.services.live_status import LiveStatusService
        from bot.services.notification_service import NotificationService

        self.sent: list[str] = []
        self.edits: list[str] = []
        self.clock = clock
        self.qbit_data = qbit_data

        async def library_notice(text: str, category: str, **extra) -> None:
            self.sent.append(text)

        async def admin_notice(text: str) -> int:
            self.sent.append(text)
            return 1

        async def user_notice(_uid: int, text: str) -> bool:
            self.sent.append(text)
            return True

        async def editor(_chat, _message, text, _markup=None) -> str:
            self.edits.append(text)
            return "ok"

        async def get_slskd():
            return slskd

        async def recipients():
            return [(111, UserPreferences())]

        async def digest_send(_uid: int, text: str) -> None:
            self.sent.append(text)

        qbit = AsyncMock()
        qbit.get_torrent.return_value = None
        qbit.get_torrents.return_value = []
        qbit.get_maindata.side_effect = self._maindata
        queue_client = AsyncMock(get_queue=AsyncMock(return_value=[]), get_calendar=AsyncMock(return_value=[]))
        queue_client.get_history_since.return_value = []

        self.live = LiveStatusService(db, editor, radarr=radarr, sonarr=sonarr, qbittorrent=qbit, clock=clock)
        self.watcher = LibraryWatcher(
            radarr, sonarr, notify=library_notice, get_slskd=get_slskd, db=db, clock=clock,
            music_settle_seconds=300, music_stall_seconds=3600, poll_interval_seconds=60, live=self.live,
        )
        self.monitor = HealthMonitor({"Radarr": health}, notify=admin_notice, db=db)
        self.digest = DigestService(
            db, radarr=queue_client, sonarr=queue_client, recipients=recipients, send=digest_send,
            clock=clock, tz=timezone.utc, hour=0,
        )
        self.qbit = NotificationService(
            qbit, user_notice, exclude_chats=self.live.owner_chats,
            pending_direct_chats=self.live.pending_direct_chats, clock=clock,
        )
        self.qbit.subscribe_user(111)

    async def _maindata(self, rid: int) -> dict:
        if rid == 0:
            return _maindata(1, dict(self.qbit_data["all"]), full=True)
        delta, self.qbit_data["delta"] = self.qbit_data["delta"], {}
        return _maindata(rid + 1, delta)

    async def start(self) -> None:
        await self.qbit._initial_sync()

    async def rounds(self, count: int, step: float = 60) -> None:
        for _ in range(count):
            self.clock.advance(step)
            await self.live.tick()
            await self.watcher.poll()
            await self.monitor.evaluate()
            await self.digest.maybe_send()
            await self.qbit._check_for_completions()


async def test_first_start_on_the_live_database_sends_no_storm_and_no_duplicates(tmp_path):
    """Review Focus 4. База как у живого бота: схема v7, отметки, старые батчи и строки."""
    import json

    from bot.models import SlskdTransfer
    from bot.services import music_notify

    start = NOW.replace(hour=10, minute=0, second=0, microsecond=0)
    clock = _Clock(start)
    watermark = start - timedelta(seconds=40)  # 40 секунд деплоя

    def listed(album: str, n: int, state: str, requested: timedelta, ended: timedelta | None = None) -> SlskdTransfer:
        directory = f"music\\{album}"
        name = f"{n:02d} - Track {n}.flac"
        return SlskdTransfer(
            username="peer", filename=name, state=state, size=100, id=f"{album}-{n}", batch_id=f"b{n}",
            directory=directory, full_filename=f"{directory}\\{name}", requested_at=start - requested,
            ended_at=start - ended if ended is not None else None,
        )

    day = timedelta(days=1)
    stuck_old = listed("Artist - Seeded (2020)", 1, "Queued, Remotely", 40 * day)
    stuck_ancient = listed("Artist - Ancient (2019)", 1, "Queued, Remotely", 100 * day)
    partial_done = listed("Artist - Partial (2021)", 1, "Completed, Succeeded", 5 * day, 5 * day)
    partial_queued = listed("Artist - Partial (2021)", 2, "Queued, Remotely", 5 * day)
    finished_old = listed("Artist - Old (2018)", 1, "Completed, Succeeded", 30 * day, 30 * day)
    never_seen_old = listed("Artist - Unknown (2017)", 1, "Completed, Succeeded", 20 * day, 20 * day)
    settling = listed("Artist - Settling (2022)", 1, "Completed, Succeeded", timedelta(minutes=5), timedelta(seconds=90))

    path = tmp_path / "bot.db"
    await _write_v7_database(path)
    raw = sqlite3.connect(path)
    for transfer, notified, changed in (
        (stuck_old, start - 40 * day, start - 40 * day),
        (stuck_ancient, start - 100 * day, start - 100 * day),
        (partial_done, start - 5 * day, start - 5 * day),
        (partial_queued, start - 5 * day, start - 5 * day),
        (finished_old, start - 30 * day, start - 30 * day),
        (settling, None, start - timedelta(seconds=90)),
    ):
        key = music_notify.batch_key(transfer)
        raw.execute(
            "INSERT OR REPLACE INTO music_batch VALUES (?, ?, ?, ?, ?)",
            (key, music_notify.album_title(transfer.directory), changed.isoformat(), changed.isoformat(),
             notified.isoformat() if notified else None),
        )
        raw.execute(
            "INSERT INTO music_transfer VALUES (?, ?, ?, ?, ?)",
            (music_notify.transfer_id(transfer), key, music_notify.track_key(transfer), transfer.state,
             changed.isoformat()),
        )
    for key, value in {
        "import_watermark:radarr": watermark.isoformat(),
        "import_watermark:sonarr": watermark.isoformat(),
        "slskd_watermark": (start - timedelta(seconds=100)).isoformat(),
        "digest_last_date": start.date().isoformat(),
        "health_open": json.dumps({"service:Radarr": {
            "text": "🔌 Radarr недоступен", "resolved_text": "🔌 Radarr снова доступен",
        }}, ensure_ascii=False),
    }.items():
        raw.execute("INSERT INTO notify_kv VALUES (?, ?)", (key, value))
    stamp = (start - timedelta(hours=1)).isoformat()
    raw.execute(
        "INSERT INTO live_status (key, chat_id, message_id, kind, title, arr, download_id, state, created_at, "
        "updated_at, edited_at) VALUES ('111:5', 111, 5, 'movie', 'Film', 'radarr', ?, 'downloading', ?, ?, ?)",
        (HASH_A.upper(), stamp, stamp, stamp),
    )
    raw.commit()
    raw.close()

    # *arr: уже объявленное (в том числе запись ровно на отметке) и пришедшее за простой.
    radarr, sonarr = _Arr(), _Arr()
    radarr.records = [
        _import_record(1, "Давний фильм", start - 3 * day, "OLD1"),
        _import_record(2, "Фильм на отметке", watermark, "OLD2"),
        _import_record(3, "Фильм за простой", watermark + timedelta(seconds=20), "NEW1"),
    ]
    sonarr.records = [
        _import_record(1, "Сериал на отметке", watermark, "OLD3"),
        _import_record(2, "Сериал за простой", watermark + timedelta(seconds=10), "NEW2"),
        _import_record(3, "Сериал за простой", watermark + timedelta(seconds=15), "NEW2"),
    ]
    slskd = _Slskd()
    slskd.transfers = [stuck_old, stuck_ancient, partial_done, partial_queued, finished_old, never_seen_old, settling]
    health = _HealthClient()
    health.up = False
    # qBit: большая раздача, торрент хозяина живого сообщения и обычная закачка.
    seeding = {f"{n:040x}": _done(f"Раздача {n}", finished=start - timedelta(seconds=n + 1)) for n in range(1, 31)}
    qbit_data = {"all": {**seeding, HASH_A: _busy("Film"), HASH_B: _busy("Обычная")}, "delta": {}}

    database = Database(str(path))
    await database.connect()
    try:
        assert await database._get_schema_version() == 8

        # ---- первый старт: только то, что случилось за простой, и каждое один раз
        rig = _FirstStart(database, clock, radarr, sonarr, slskd, health, qbit_data)
        await rig.start()
        await rig.rounds(6)
        assert sorted(rig.sent) == sorted([
            "✅ <b>Фильм за простой</b> — в библиотеке.",
            "✅ <b>Сериал за простой</b> — в библиотеке (2 серии).",
            "🎵 <b>Artist — Settling (2022)</b>: 1/1 скачано",
        ])

        # ---- первое обслуживание (через минуту после старта) и опросы после него
        rig.sent.clear()
        await database.run_maintenance(backup=True)
        await rig.rounds(3)
        assert rig.sent == []

        # ---- торренты: ждавший докачки уведомляет, хозяин граба второго не получает
        qbit_data["delta"] = {
            HASH_A: {"progress": 1.0, "state": "uploading"},
            HASH_B: {"progress": 1.0, "state": "uploading"},
        }
        await rig.rounds(1)
        assert len(rig.sent) == 1 and "Обычная" in rig.sent[0]
        rig.sent.clear()

        # ---- старые застрявшие альбомы молчат и через час, и когда докачались
        await rig.rounds(65)  # больше часа обычными опросами: правило застоя срабатывает
        assert rig.sent == []
        slskd.transfers[3] = listed("Artist - Partial (2021)", 2, "Completed, Succeeded", 5 * day, -timedelta(hours=3))
        await rig.rounds(2, step=301)
        assert rig.sent == []

        # ---- Radarr поднялся: одно сообщение о восстановлении
        health.up = True
        await rig.rounds(2)
        assert len(rig.sent) == 1 and "снова доступен" in rig.sent[0]
        rig.sent.clear()

        # ---- ещё один рестарт на той же базе: новых сообщений нет
        qbit_data["all"] = {**seeding, HASH_A: _done("Film"), HASH_B: _done("Обычная")}
        again = _FirstStart(database, clock, radarr, sonarr, slskd, health, qbit_data)
        await again.start()
        await again.rounds(5)
        assert again.sent == []
    finally:
        await database.close()


async def test_batch_dropped_by_maintenance_is_not_news_while_slskd_still_lists_it(db):
    """DB-02: надгробие убрано, а очередь всё ещё в slskd. Это не новая закачка."""
    from bot.models import SlskdTransfer

    clock, slskd = _Clock(), _Slskd()
    watcher, sent = _music_watcher(db, clock, slskd)
    await watcher.poll()

    clock.advance(60)
    slskd.transfers = [SlskdTransfer(
        username="peer", filename="01 - T.flac", state="Queued, Remotely", size=100, id="stuck", batch_id="b",
        directory=ALBUM, full_filename=f"{ALBUM}\\01 - T.flac", requested_at=clock.now - timedelta(days=100),
    )]
    await watcher.poll()
    clock.advance(4000)
    await watcher.poll()
    assert sent == []
    assert await db.music_transfer_states() == {}


async def test_new_queued_transfer_requested_after_the_last_poll_still_counts(db):
    from bot.models import SlskdTransfer

    clock, slskd = _Clock(), _Slskd()
    watcher, sent = _music_watcher(db, clock, slskd)
    await watcher.poll()

    clock.advance(60)
    slskd.transfers = [SlskdTransfer(
        username="peer", filename="01 - T.flac", state="Queued, Remotely", size=100, id="fresh", batch_id="b",
        directory=ALBUM, full_filename=f"{ALBUM}\\01 - T.flac", requested_at=clock.now - timedelta(seconds=10),
    )]
    await watcher.poll()
    clock.advance(4000)
    await watcher.poll()
    assert len(sent) == 1 and "ещё 1 в очереди" in sent[0]


async def test_flush_at_shutdown_is_bounded_when_telegram_hangs(monkeypatch):
    """Настоящая отмена: лежащий Telegram не съедает время на остановку контейнера."""
    from unittest.mock import AsyncMock, MagicMock

    from structlog.testing import capture_logs

    from bot import main
    from bot.clients import registry
    from bot.services import library_watcher

    for name in ("get_radarr", "get_sonarr", "get_lidarr", "get_slskd"):
        monkeypatch.setattr(registry, name, AsyncMock(return_value=None))
    settings = MagicMock(notify_check_interval=60, music_notify_settle_seconds=300, music_notify_stall_seconds=3600)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "_LIBRARY_FLUSH_TIMEOUT_S", 0.05)
    started = asyncio.Event()
    real_sleep = asyncio.sleep

    async def hanging_flush() -> None:
        await real_sleep(30)

    async def poll() -> None:
        started.set()

    watcher = MagicMock(poll=poll, flush_held=hanging_flush)
    monkeypatch.setattr(library_watcher, "LibraryWatcher", MagicMock(return_value=watcher))

    async def fake_sleep(seconds: float) -> None:
        await real_sleep(0 if not started.is_set() else 30)

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)
    with capture_logs() as logs:
        task = asyncio.create_task(main._library_watch(MagicMock(), MagicMock(), main.structlog.get_logger()))
        await asyncio.wait_for(started.wait(), 2)
        await real_sleep(0)
        task.cancel()
        await asyncio.wait_for(asyncio.gather(task), 2)

    assert any(e["event"] == "library_watch_flush_failed" for e in logs)
