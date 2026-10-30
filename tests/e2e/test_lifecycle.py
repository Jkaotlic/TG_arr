"""Настоящий `main()` от старта до остановки: проводка и порядок shutdown.

Polling настоящий: порядок `emit_shutdown` и нашей уборки задаёт aiogram, и подмена
`start_polling` проверяла бы подмену. Подделаны только сессия Telegram, фоновые циклы
и глобальные побочки процесса (логирование, watchdog).
"""

import asyncio
import signal
from typing import Any
from unittest.mock import MagicMock

from aiogram import Dispatcher, methods
from aiogram.types import Update

import bot.main as bot_main
from bot.db import Database
from tests.e2e.fakes import FakeTelegram


class _PollingTelegram(FakeTelegram):
    """На первый getUpdates бот уже запущен: просим его остановиться, как по SIGTERM."""

    def __init__(self, on_first_poll: Any) -> None:
        super().__init__()
        self._on_first_poll = on_first_poll
        self.on_method(methods.GetUpdates, self._get_updates)
        self.on_method(methods.DeleteWebhook, lambda bot, method: True)

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
