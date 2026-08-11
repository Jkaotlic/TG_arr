"""Proactive health alerts (2026-07-29, rewired for *arr 2026-08-10).

Born from a prod incident: the indexers had been failing since 11:00 and
nobody knew until a search broke at 16:56. Scryer's `systemHealth` query gave
one call visibility into indexer 24h success/fail ratios and the wanted
backlog size; neither has an *arr equivalent in this rollback's scope (the
backlog is still visible on demand via /wanted — see
tests/test_handlers_status_emby.py — just no longer proactively alerted on).
What every *arr client already exposes is `check_connection()`, i.e. "is the
service even reachable" — that is what `HealthMonitor` watches now.
"""

from unittest.mock import AsyncMock

import pytest

from bot.services.health_monitor import HealthMonitor, HealthState, Problem, diagnose


def _report(**overrides) -> dict:
    """A `check_all()`-shaped report with all four services healthy by default."""
    base = {
        name: {"available": True, "version": "1.0", "response_time_ms": 12.0}
        for name in ("Radarr", "Sonarr", "Prowlarr", "Lidarr")
    }
    base.update(overrides)
    return base


def _clients(**overrides) -> dict:
    """AsyncMock clients whose check_connection() answers (True, "1.0", 12.0)
    unless overridden with a `(available, version, ms)` tuple or an exception."""
    clients = {name: AsyncMock() for name in ("Radarr", "Sonarr", "Prowlarr", "Lidarr")}
    for name, client in clients.items():
        client.check_connection.return_value = (True, "1.0", 12.0)
    for name, outcome in overrides.items():
        if isinstance(outcome, Exception):
            clients[name].check_connection.side_effect = outcome
        else:
            clients[name].check_connection.return_value = outcome
    return clients


# ------------------------------------------------------------------ diagnose
def test_a_healthy_report_produces_no_problems():
    assert diagnose(_report()) == []


def test_a_down_service_is_reported():
    problems = diagnose(_report(Prowlarr={"available": False, "version": None, "response_time_ms": 50.0}))
    assert [p.key for p in problems] == ["service:Prowlarr"]
    assert "Prowlarr" in problems[0].text


def test_multiple_down_services_are_each_reported():
    problems = diagnose(_report(
        Radarr={"available": False, "version": None, "response_time_ms": None},
        Lidarr={"available": False, "version": None, "response_time_ms": None},
    ))
    assert {p.key for p in problems} == {"service:Radarr", "service:Lidarr"}


def test_the_error_detail_is_carried_into_the_problem_text():
    problems = diagnose(_report(
        Sonarr={"available": False, "version": None, "response_time_ms": None, "error": "timeout"},
    ))
    assert "timeout" in problems[0].text


# --------------------------------------------------------------------- poll
@pytest.mark.asyncio
async def test_check_all_covers_all_four_services():
    """The canonical shape `HealthMonitor` must produce — see task-14 brief."""
    clients = _clients()
    monitor = HealthMonitor(clients)

    report = await monitor.check_all()

    assert set(report) == {"Radarr", "Sonarr", "Prowlarr", "Lidarr"}
    assert all(entry["available"] for entry in report.values())


@pytest.mark.asyncio
async def test_check_all_delegates_the_api_prefix_to_each_client():
    """Radarr/Sonarr answer on /api/v3, Prowlarr/Lidarr on /api/v1 — a v3
    probe against a healthy Lidarr reports "API DOWN" (live-verified
    2026-08-10). `check_all` must call each client's own `check_connection()`
    rather than hardcoding a prefix, so it never has to know this itself."""
    clients = _clients()
    monitor = HealthMonitor(clients)

    await monitor.check_all()

    for client in clients.values():
        client.check_connection.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_a_raising_client_is_reported_unavailable_not_propagated():
    clients = _clients(Radarr=ConnectionError("refused"))
    monitor = HealthMonitor(clients)

    report = await monitor.check_all()

    assert report["Radarr"]["available"] is False
    assert "refused" in report["Radarr"]["error"]
    assert report["Sonarr"]["available"] is True


# --------------------------------------------------------------- transitions
@pytest.mark.asyncio
async def test_alert_fires_once_per_outage_not_every_cycle():
    """A sustained outage must not spam — alert on the transition only."""
    sent = []
    clients = _clients(Prowlarr=(False, None, None))
    monitor = HealthMonitor(clients, notify=AsyncMock(side_effect=lambda text: sent.append(text)))

    await monitor.evaluate()
    await monitor.evaluate()
    await monitor.evaluate()

    assert len(sent) == 1


@pytest.mark.asyncio
async def test_recovery_is_announced():
    sent = []
    down = _clients(Prowlarr=(False, None, None))
    monitor = HealthMonitor(down, notify=AsyncMock(side_effect=lambda text: sent.append(text)))

    await monitor.evaluate()
    down["Prowlarr"].check_connection.return_value = (True, "1.0", 12.0)
    await monitor.evaluate()

    assert len(sent) == 2
    assert "восстанов" in sent[1].lower()


@pytest.mark.asyncio
async def test_a_new_problem_alerts_even_while_another_is_open():
    sent = []
    clients = _clients(Prowlarr=(False, None, None))
    monitor = HealthMonitor(clients, notify=AsyncMock(side_effect=lambda text: sent.append(text)))

    await monitor.evaluate()
    clients["Radarr"].check_connection.return_value = (False, None, None)
    await monitor.evaluate()

    assert len(sent) == 2
    assert "Radarr" in sent[1]


@pytest.mark.asyncio
async def test_a_failing_notify_does_not_wedge_the_monitor():
    clients = _clients(Prowlarr=(False, None, None))
    monitor = HealthMonitor(clients, notify=AsyncMock(side_effect=Exception("telegram down")))

    await monitor.evaluate()
    clients["Prowlarr"].check_connection.return_value = (True, "1.0", 12.0)
    await monitor.evaluate()  # must not raise


@pytest.mark.asyncio
async def test_evaluate_without_a_notify_callback_does_not_raise():
    """`notify` is optional — a caller that only wants `state`/`check_all`
    (e.g. a future /health-style consumer) must not be forced to wire one up."""
    clients = _clients(Prowlarr=(False, None, None))
    monitor = HealthMonitor(clients)

    await monitor.evaluate()  # must not raise


@pytest.mark.asyncio
async def test_state_is_reported_for_the_health_command():
    clients = _clients(Prowlarr=(False, None, None))
    monitor = HealthMonitor(clients, notify=AsyncMock())

    await monitor.evaluate()
    assert monitor.state is HealthState.DEGRADED

    clients["Prowlarr"].check_connection.return_value = (True, "1.0", 12.0)
    await monitor.evaluate()
    assert monitor.state is HealthState.OK


def test_problem_resolved_text_never_equals_its_own_text():
    """A recovery notice must not reuse the problem's wording, or it reads as
    a contradiction — the live alert on 2026-07-30 said "✅ Восстановлено 🔎
    RuTracker.org: 88 из 88 запросов падают (100%)"."""
    problem = diagnose(_report(Radarr={"available": False, "version": None, "response_time_ms": None}))[0]
    assert problem.text != problem.resolved_text


def test_problem_is_a_frozen_dataclass_identified_by_key():
    p1 = Problem(key="service:Radarr", text="down", resolved_text="up")
    p2 = Problem(key="service:Radarr", text="down", resolved_text="up")
    assert p1 == p2
