"""Background resilience (audit 2026-09-23, NOTIF-08/09/10/11).

NOTIF-08: a sleeping server caused a retry storm — 3 attempts every 60 s and
~7 log lines per call (~2700 `Connection error` lines in 3 days). Background
callers now make one attempt, a per-client circuit breaker keeps them off the
network while the service is down, and only the `service_down`/`service_up`
transitions are logged.
NOTIF-09: health alerts had no hysteresis and came one message per service.
NOTIF-10: an inner try swallowed qBittorrent errors, defeating the throttle.
NOTIF-11: Telegram flood control (RetryAfter) was not handled.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import structlog.testing

from bot.clients.base import (
    BaseAPIClient,
    CircuitBreaker,
    ServiceConnectionError,
    ServiceUnavailable,
    background_calls,
)
from bot.db import Database
from bot.services.health_monitor import HealthMonitor


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


# ------------------------------------------------------------------ breaker
def test_breaker_opens_backs_off_half_opens_and_closes():
    clock = FakeClock()
    breaker = CircuitBreaker("Radarr", clock=clock)
    assert breaker.allow(background=True)

    with structlog.testing.capture_logs() as logs:
        breaker.record_failure()
        assert breaker.is_open
        assert not breaker.allow(background=True)
        assert breaker.allow(background=False)  # interactive: one attempt anyway

        clock.t += 30
        assert breaker.allow(background=True)  # half-open probe
        breaker.record_failure()
        clock.t += 59
        assert not breaker.allow(background=True)  # backoff grew to 60 s
        clock.t += 1
        assert breaker.allow(background=True)

        breaker.record_success()
        assert not breaker.is_open
        assert breaker.allow(background=True)

    events = [e["event"] for e in logs]
    assert events == ["service_down", "service_up"]
    up = logs[-1]
    assert up["service"] == "Radarr"
    assert up["downtime_s"] == 90


def test_breaker_backoff_is_capped_at_ten_minutes():
    clock = FakeClock()
    breaker = CircuitBreaker("Sonarr", clock=clock)
    for _ in range(10):
        breaker.record_failure()
    clock.t += 599
    assert not breaker.allow(background=True)
    clock.t += 1
    assert breaker.allow(background=True)


# ------------------------------------------------------------- client wiring
def _client_with_refusing_server() -> tuple[BaseAPIClient, AsyncMock]:
    client = BaseAPIClient("http://svc.example.com", "test-key", "TestService")
    http = AsyncMock()
    http.is_closed = False
    http.request = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
    client._client = http
    return client, http


@pytest.mark.asyncio
async def test_background_calls_make_one_attempt_then_stay_off_the_network(monkeypatch):
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    client, http = _client_with_refusing_server()

    with structlog.testing.capture_logs() as logs:
        with background_calls():
            with pytest.raises(ServiceConnectionError):
                await client.get("/x")
            assert http.request.await_count == 1  # no tenacity retries

            for _ in range(10):
                with pytest.raises(ServiceUnavailable):
                    await client.get("/x")

    assert http.request.await_count == 1  # 10 calls while open: 0 HTTP attempts
    events = [e["event"] for e in logs]
    assert events.count("service_down") == 1
    assert "request_retry_attempt" not in events
    assert "request_retries_exhausted" not in events
    assert all(e.get("log_level") != "warning" or e["event"] == "service_down" for e in logs)


@pytest.mark.asyncio
async def test_interactive_call_while_open_tries_exactly_once(monkeypatch):
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    client, http = _client_with_refusing_server()
    with background_calls():
        with pytest.raises(ServiceConnectionError):
            await client.get("/x")
    http.request.reset_mock()

    with pytest.raises(ServiceConnectionError):
        await client.get("/x")  # user-triggered: attempts, but only once

    assert http.request.await_count == 1


@pytest.mark.asyncio
async def test_success_after_an_outage_logs_service_up(monkeypatch):
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    client, http = _client_with_refusing_server()
    with background_calls():
        with pytest.raises(ServiceConnectionError):
            await client.get("/x")

    ok = MagicMock()
    ok.status_code = 200
    ok.json = lambda: {"ok": True}
    http.request = AsyncMock(return_value=ok)
    with structlog.testing.capture_logs() as logs:
        assert await client.get("/x") == {"ok": True}

    assert [e["event"] for e in logs if e["event"].startswith("service_")] == ["service_up"]
    assert not client.breaker.is_open


@pytest.mark.asyncio
async def test_the_last_attempt_does_not_claim_it_will_retry(monkeypatch):
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    client, _ = _client_with_refusing_server()

    with structlog.testing.capture_logs() as logs:
        with pytest.raises(ServiceConnectionError):
            await client.get("/x")

    assert not any("will retry" in e["event"] for e in logs)


@pytest.mark.asyncio
async def test_library_watcher_skips_a_service_whose_breaker_is_open():
    from bot.services.library_watcher import LibraryWatcher

    radarr, sonarr = AsyncMock(), AsyncMock()
    radarr.breaker = CircuitBreaker("Radarr")
    radarr.breaker.record_failure()
    sonarr.get_history.return_value = []

    watcher = LibraryWatcher(radarr, sonarr)
    with structlog.testing.capture_logs() as logs:
        await watcher.poll()

    radarr.get_history.assert_not_awaited()
    sonarr.get_history.assert_awaited_once()
    assert not [e for e in logs if e.get("log_level") == "warning"]


# ------------------------------------------------------------------- health
def _clients(**down) -> dict:
    clients = {name: AsyncMock() for name in ("Radarr", "Sonarr", "Prowlarr")}
    for name, client in clients.items():
        client.check_connection.return_value = (name not in down, "1.0", 5.0)
    return clients


def _set(clients: dict, name: str, up: bool) -> None:
    clients[name].check_connection.return_value = (up, "1.0", 5.0)


@pytest.mark.asyncio
async def test_a_single_failed_check_does_not_alert_but_asks_for_a_recheck():
    sent: list[str] = []
    clients = _clients(Radarr=True)
    monitor = HealthMonitor(clients, notify=AsyncMock(side_effect=sent.append))

    await monitor.evaluate()
    assert sent == []
    assert monitor.needs_recheck

    _set(clients, "Radarr", True)  # it was a blip
    await monitor.evaluate()
    assert sent == []
    assert not monitor.needs_recheck


@pytest.mark.asyncio
async def test_two_services_down_in_one_cycle_are_one_message():
    sent: list[str] = []
    clients = _clients(Radarr=True, Sonarr=True)
    monitor = HealthMonitor(clients, notify=AsyncMock(side_effect=sent.append))

    await monitor.evaluate()
    await monitor.evaluate()

    assert len(sent) == 1
    assert "Radarr" in sent[0] and "Sonarr" in sent[0]


@pytest.mark.asyncio
async def test_open_problems_survive_a_restart(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    await db.connect()
    try:
        sent: list[str] = []
        clients = _clients(Radarr=True)
        first = HealthMonitor(clients, notify=AsyncMock(side_effect=sent.append), db=db)
        await first.evaluate()
        await first.evaluate()
        assert len(sent) == 1

        restarted = HealthMonitor(clients, notify=AsyncMock(side_effect=sent.append), db=db)
        await restarted.evaluate()
        await restarted.evaluate()
        assert len(sent) == 1  # still down: no second alert

        _set(clients, "Radarr", True)
        await restarted.evaluate()
        assert len(sent) == 2
        assert "Radarr" in sent[1] and "доступ" in sent[1]
        assert json.loads(await db.kv_get("health_open") or "{}") == {}
    finally:
        await db.close()


# ---------------------------------------------------------- telegram sending
@pytest.mark.asyncio
async def test_retry_after_is_honoured_once(monkeypatch):
    from aiogram.exceptions import TelegramRetryAfter

    from bot import main

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)
    bot = AsyncMock()
    bot.send_message = AsyncMock(side_effect=[
        TelegramRetryAfter(method=MagicMock(), message="Flood control", retry_after=7),
        None,
    ])

    await main.send_telegram(bot, 111, "hi")

    assert bot.send_message.await_count == 2
    assert 7 in sleeps


@pytest.mark.asyncio
async def test_a_second_retry_after_is_raised(monkeypatch):
    from aiogram.exceptions import TelegramRetryAfter

    from bot import main

    monkeypatch.setattr(main.asyncio, "sleep", AsyncMock())
    bot = AsyncMock()
    flood = TelegramRetryAfter(method=MagicMock(), message="Flood control", retry_after=3)
    bot.send_message = AsyncMock(side_effect=[flood, flood])

    with pytest.raises(TelegramRetryAfter):
        await main.send_telegram(bot, 111, "hi")
    assert bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_messages_to_one_chat_are_spaced_by_a_second(monkeypatch):
    from bot import main

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(main, "_last_sent_at", {})
    bot = AsyncMock()

    await main.send_telegram(bot, 111, "one")
    await main.send_telegram(bot, 111, "two")
    await main.send_telegram(bot, 222, "other chat")

    assert len(sleeps) == 1
    assert 0 < sleeps[0] <= 1.0


# ------------------------------------------------------------ qBit notifier
@pytest.mark.asyncio
async def test_qbit_errors_reach_the_monitor_loop_throttle():
    from bot.services.notification_service import NotificationService

    qbit = AsyncMock()
    qbit.get_maindata.side_effect = ConnectionError("qbit down")
    svc = NotificationService(qbit, AsyncMock())

    with pytest.raises(ConnectionError):
        await svc._check_for_completions()


@pytest.mark.asyncio
async def test_completed_torrents_are_not_kept_in_tracking():
    from bot.services.notification_service import NotificationService

    qbit = AsyncMock()
    qbit.get_maindata.return_value = {
        "rid": 1, "full_update": True, "torrents_removed": [],
        "torrents": {
            "a" * 40: {"name": "Done", "progress": 1.0, "state": "uploading"},
            "b" * 40: {"name": "Busy", "progress": 0.3, "state": "downloading"},
        },
    }
    svc = NotificationService(qbit, AsyncMock())
    await svc._initial_sync()

    assert set(svc._tracked_torrents) == {"b" * 40}
