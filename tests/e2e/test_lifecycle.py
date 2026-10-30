"""Настоящий `main()` от старта до остановки: проводка и порядок shutdown.

Polling настоящий: порядок `emit_shutdown` и нашей уборки задаёт aiogram, и подмена
`start_polling` проверяла бы подмену. Подделаны только сессия Telegram, фоновые циклы
и глобальные побочки процесса (логирование, watchdog).
"""

import asyncio
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import structlog
from aiogram import Dispatcher, methods
from aiogram.exceptions import TelegramServerError
from aiogram.types import Update

import bot.main as bot_main
from bot.db import Database
from tests.e2e.fakes import BOT_TOKEN, BotEnv, FakeTelegram


class _PollingTelegram(FakeTelegram):
    """На первый getUpdates бот уже запущен: просим его остановиться, как по SIGTERM."""

    def __init__(self, on_first_poll: Any) -> None:
        super().__init__()
        self._on_first_poll = on_first_poll
        self.on_method(methods.GetUpdates, self._get_updates)

    def _get_updates(self, bot: Any, method: Any) -> list[Update]:
        self._on_first_poll()
        return []

    async def make_request(self, bot: Any, method: Any, timeout: Any = None) -> Any:
        # Настоящий getUpdates ждёт в сети. Без паузы polling крутится, не отдавая
        # управление, и сигнал остановки не обрабатывается никогда.
        await asyncio.sleep(0.01)
        return await super().make_request(bot, method, timeout)


async def _run_main(monkeypatch, make_e2e_stack) -> dict[str, Any]:
    """Прогнать `main()` и вернуть журнал событий остановки и созданные объекты."""
    await make_e2e_stack()
    events: list[str] = []
    seen: dict[str, Any] = {"events": events}

    def recording_loop(name: str):
        async def loop(*args: Any, **kwargs: Any) -> None:
            try:
                await asyncio.sleep(3600)
            finally:
                events.append(f"cancelled:{name}")

        return loop

    for name in ("_periodic_cleanup", "_health_watch", "_library_watch", "_live_status_watch", "_digest_watch"):
        monkeypatch.setattr(bot_main, name, recording_loop(name))
    monkeypatch.setattr(bot_main, "_liveness_watchdog", lambda: None)
    monkeypatch.setattr(bot_main, "setup_logging", lambda *args, **kwargs: None)
    monkeypatch.setattr(bot_main, "faulthandler", MagicMock())

    real_close_db = Database.close
    real_connect_db = Database.connect
    opened: list[Database] = []

    async def connect_db(self: Database) -> None:
        opened.append(self)
        await real_connect_db(self)

    async def close_db(self: Database) -> None:
        events.append("db_closed")
        await real_close_db(self)

    monkeypatch.setattr(Database, "connect", connect_db)
    monkeypatch.setattr(Database, "close", close_db)

    real_close_clients = bot_main.close_all_clients

    async def close_clients() -> None:
        events.append("clients_closed")
        await real_close_clients()

    monkeypatch.setattr(bot_main, "close_all_clients", close_clients)

    real_build = bot_main.build_dispatcher

    def build(db: Database, notification_service: Any = None) -> Dispatcher:
        dispatcher = real_build(db, notification_service)
        seen.update(dispatcher=dispatcher, db=db, notification_service=notification_service)
        return dispatcher

    monkeypatch.setattr(bot_main, "build_dispatcher", build)

    real_start_polling = Dispatcher.start_polling

    async def start_polling(self: Dispatcher, *bots: Any, **kwargs: Any) -> None:
        seen["polled_dispatcher"] = self
        seen["polling_kwargs"] = kwargs
        await real_start_polling(self, *bots, **kwargs, handle_signals=False)

    monkeypatch.setattr(Dispatcher, "start_polling", start_polling)

    real_bot = bot_main.Bot

    def make_bot(*args: Any, **kwargs: Any) -> Any:
        session = _PollingTelegram(lambda: seen["dispatcher"]._signal_stop_polling(signal.SIGTERM))
        seen["tg"] = session
        return real_bot(*args, session=session, **kwargs)

    monkeypatch.setattr(bot_main, "Bot", make_bot)

    try:
        await asyncio.wait_for(bot_main.main(), timeout=10)
    finally:
        # Если main() упал раньше shutdown: поток aiosqlite не демон и не даст
        # процессу pytest выйти. Для уже закрытой БД вызов безвреден.
        for database in opened:
            await real_close_db(database)
    return seen


async def test_background_tasks_stop_before_db_and_clients_close(monkeypatch, make_e2e_stack):
    """BUG-09: фоновый цикл не должен биться в уже закрытые БД и клиенты."""
    seen = await _run_main(monkeypatch, make_e2e_stack)

    events = seen["events"]
    assert "db_closed" in events and "clients_closed" in events
    cancelled = [i for i, event in enumerate(events) if event.startswith("cancelled:")]
    assert len(cancelled) == 5, events
    assert max(cancelled) < events.index("clients_closed") < events.index("db_closed"), events
    assert events.count("db_closed") == 1, events


async def test_main_uses_build_dispatcher(monkeypatch, make_e2e_stack):
    """Тесты гоняют `build_dispatcher`, поэтому `main()` обязан поллить именно его результат."""
    seen = await _run_main(monkeypatch, make_e2e_stack)

    assert seen["polled_dispatcher"] is seen["dispatcher"]
    assert seen["notification_service"] is None
    assert seen["dispatcher"]["notification_service"] is None
    assert seen["tg"].violations == []


async def test_polling_asks_only_for_handled_updates_and_keeps_queued_ones(monkeypatch, make_e2e_stack):
    """Очередь на время деплоя обрабатывается намеренно. У `start_polling` нет
    `drop_pending_updates`: такой аргумент молча уходит в workflow data и обещает
    то, чего не делает. deleteWebhook не вызывается, иначе FakeTelegram упал бы."""
    seen = await _run_main(monkeypatch, make_e2e_stack)

    assert "drop_pending_updates" not in seen["polling_kwargs"]
    assert "drop_pending_updates" not in seen["dispatcher"].workflow_data
    polls = [call for call in seen["tg"].calls if isinstance(call.method, methods.GetUpdates)]
    assert polls
    assert all(call.method.allowed_updates == ["message", "callback_query"] for call in polls)
    assert seen["tg"].violations == []


async def _main_failing_at_startup(monkeypatch, make_e2e_stack) -> list[Database]:
    """Подготовить `main()` к старту с подменой только побочек процесса."""
    await make_e2e_stack()
    monkeypatch.setattr(bot_main, "_liveness_watchdog", lambda: None)
    monkeypatch.setattr(bot_main, "setup_logging", lambda *args, **kwargs: None)
    monkeypatch.setattr(bot_main, "faulthandler", MagicMock())
    real_connect = Database.connect
    opened: list[Database] = []

    async def connect_db(self: Database) -> None:
        opened.append(self)
        await real_connect(self)

    monkeypatch.setattr(Database, "connect", connect_db)
    return opened


def _non_daemon_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if not t.daemon and t is not threading.main_thread()]


async def _assert_no_thread_keeps_the_process() -> None:
    for _ in range(100):
        if not _non_daemon_threads():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"процесс не выйдет, живы потоки: {_non_daemon_threads()}")


async def test_failure_after_db_connect_closes_the_db_and_propagates(monkeypatch, make_e2e_stack):
    opened = await _main_failing_at_startup(monkeypatch, make_e2e_stack)

    async def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("сборка живых сообщений упала")

    monkeypatch.setattr(bot_main, "_build_live_status", boom)

    try:
        with pytest.raises(RuntimeError, match="сборка живых сообщений"):
            await asyncio.wait_for(bot_main.main(), timeout=10)
        assert opened and opened[0]._connection is None
        await _assert_no_thread_keeps_the_process()
    finally:
        for database in opened:
            await database.close()


def _bot_with_fake_session(real_bot: Any) -> Any:
    def make_bot(*args: Any, **kwargs: Any) -> Any:
        return real_bot(*args, session=FakeTelegram(), **kwargs)

    return make_bot


async def test_failure_in_on_startup_stops_tasks_and_closes_everything(monkeypatch, make_e2e_stack):
    """aiogram зовёт startup вне своего try: при сбое shutdown не срабатывает."""
    opened = await _main_failing_at_startup(monkeypatch, make_e2e_stack)
    cancelled: list[str] = []

    def recording_loop(name: str):
        async def loop(*args: Any, **kwargs: Any) -> None:
            try:
                await asyncio.sleep(3600)
            finally:
                cancelled.append(name)

        return loop

    for name in ("_periodic_cleanup", "_health_watch", "_library_watch", "_live_status_watch", "_digest_watch"):
        monkeypatch.setattr(bot_main, name, recording_loop(name))

    async def boom(*args: Any, **kwargs: Any) -> None:
        await asyncio.sleep(0)  # циклы успевают стартовать
        raise RuntimeError("startup упал")

    monkeypatch.setattr(bot_main, "on_startup", boom)
    monkeypatch.setattr(bot_main, "Bot", _bot_with_fake_session(bot_main.Bot))

    try:
        with pytest.raises(RuntimeError, match="startup упал"):
            await asyncio.wait_for(bot_main.main(), timeout=10)
        assert len(cancelled) == 5
        assert opened and opened[0]._connection is None
        await _assert_no_thread_keeps_the_process()
    finally:
        for database in opened:
            await database.close()


async def test_failed_migration_closes_the_connection(monkeypatch, make_e2e_stack):
    opened = await _main_failing_at_startup(monkeypatch, make_e2e_stack)

    async def boom(self: Database) -> None:
        raise RuntimeError("миграция упала")

    monkeypatch.setattr(Database, "_run_migrations", boom)

    try:
        with pytest.raises(RuntimeError, match="миграция упала"):
            await asyncio.wait_for(bot_main.main(), timeout=10)
        assert opened and opened[0]._connection is None
        await _assert_no_thread_keeps_the_process()
    finally:
        for database in opened:
            await database.close()


def test_process_exits_with_an_error_when_startup_fails(tmp_path):
    """Сам процесс, а не корутина: контейнер должен перезапуститься, а не висеть."""
    script = "\n".join([
        "import asyncio",
        "import bot.main as m",
        "async def boom(*a, **k):",
        "    raise RuntimeError(chr(33))",
        "m._build_live_status = boom",
        "asyncio.run(m.main())",
    ])
    inherited = ("PATH", "SYSTEMROOT", "TEMP", "TMP")
    env = {
        **{k: v for k, v in os.environ.items() if k.upper() in inherited},
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        "TELEGRAM_BOT_TOKEN": "123456:e2e-test-token",
        "ADMIN_TG_IDS": "1",
        "DATABASE_PATH": str(tmp_path / "bot.db"),
        "RADARR_URL": "http://127.0.0.1:9",
        "RADARR_API_KEY": "x",
        "SONARR_URL": "http://127.0.0.1:9",
        "SONARR_API_KEY": "x",
        "PROWLARR_URL": "http://127.0.0.1:9",
        "PROWLARR_API_KEY": "x",
        "NOTIFY_DOWNLOAD_COMPLETE": "false",
        "DEEZER_ENABLED": "false",
    }
    done = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, env=env, capture_output=True, timeout=30,
    )
    assert done.returncode == 1, done.stderr.decode(errors="replace")[-2000:]


async def test_library_loop_over_real_clients_delivers_through_a_telegram_502(monkeypatch, make_e2e_stack):
    """Настоящий цикл, клиенты и сокеты: отметка берётся из /history, дальше только
    /history/since, а 502 Telegram стоит паузы, но не уведомления (LOGIC-08)."""
    stack = await make_e2e_stack()
    radarr, sonarr = stack.backends["radarr"], stack.backends["sonarr"]
    old = {"id": 1, "sourceTitle": "Старый", "downloadId": "OLD", "date": "2026-10-30T09:00:00Z"}
    new = {"id": 2, "sourceTitle": "Новый", "downloadId": "NEW", "date": "2026-10-30T09:30:00Z"}
    radarr.route("GET", "/api/v3/history", {"records": [old]})
    radarr.route("GET", "/api/v3/history/since", [old, new])
    sonarr.route("GET", "/api/v3/history", {"records": []})
    sonarr.route("GET", "/api/v3/history/since", [])

    telegram = FakeTelegram()
    real_send = telegram._send_message
    failures_left = [1]

    def flaky_send(bot: Any, method: Any) -> Any:
        if failures_left:
            failures_left.clear()
            raise TelegramServerError(method, "Bad Gateway")
        return real_send(bot, method)

    telegram.on_method(methods.SendMessage, flaky_send)
    bot = bot_main.Bot(BOT_TOKEN, session=telegram)

    real_sleep = asyncio.sleep
    pauses: list[float] = []

    async def quick_sleep(seconds: float, *args: Any, **kwargs: Any) -> None:
        pauses.append(seconds)
        await real_sleep(0.005)

    db = Database(str(stack.db_path))
    await db.connect()
    monkeypatch.setattr(bot_main.asyncio, "sleep", quick_sleep)
    task = asyncio.create_task(bot_main._library_watch(bot, db, structlog.get_logger()))
    try:
        async with asyncio.timeout(10):
            while len([c for c in telegram.calls if isinstance(c.method, methods.SendMessage)]) < 2:
                await real_sleep(0.01)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        monkeypatch.undo()
        await db.close()

    sent = [c for c in telegram.calls if isinstance(c.method, methods.SendMessage)]
    assert {c.method.chat_id for c in sent} == {BotEnv.ADMIN, BotEnv.USER}
    assert all("Новый" in c.method.text for c in sent)
    assert 2 in pauses
    assert telegram.violations == []
    assert len(radarr.calls("GET", "/api/v3/history")) == 1
    assert radarr.calls("GET", "/api/v3/history/since")
