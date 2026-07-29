"""Proactive health alerts + /wanted (2026-07-29).

Both come straight out of the prod incident: the indexers had been failing
since 11:00 and nobody knew until a search broke at 16:56, and the cause —
102 Paw Patrol episodes stuck in the wanted queue — was only visible through
raw GraphQL.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.models import ContentType, IndexerStat, ScryerHealth, ScryerWantedItem
from bot.services.health_monitor import HealthMonitor, HealthState, diagnose


def _health(**overrides) -> ScryerHealth:
    data = dict(
        service_ready=True,
        total_titles=42,
        monitored_titles=39,
        version="0.17.2",
        indexers=[
            IndexerStat(name="RuTracker.org", queries_24h=100, successful_24h=95, failed_24h=5),
            IndexerStat(name="RinTor.NeT", queries_24h=100, successful_24h=90, failed_24h=10),
        ],
    )
    data.update(overrides)
    return ScryerHealth(**data)


# ------------------------------------------------------------------ diagnose
def test_a_healthy_system_produces_no_problems():
    assert diagnose(_health(), wanted_total=28) == []


def test_a_dead_service_is_reported():
    problems = diagnose(_health(service_ready=False), wanted_total=0)
    assert any(p.key == "service_down" for p in problems)


def test_indexers_failing_most_queries_are_reported():
    """The prod signature: 2143 failures out of 2641 queries."""
    burning = _health(indexers=[
        IndexerStat(name="RuTracker.org", queries_24h=2641, successful_24h=498, failed_24h=2143),
        IndexerStat(name="RinTor.NeT", queries_24h=100, successful_24h=95, failed_24h=5),
    ])
    problems = diagnose(burning, wanted_total=28)
    indexer_problems = [p for p in problems if p.key.startswith("indexer:")]
    assert len(indexer_problems) == 1
    assert "RuTracker" in indexer_problems[0].text


def test_an_idle_indexer_is_not_reported_as_failing():
    """0 of 0 queries is 0% success, but nothing is wrong."""
    idle = _health(indexers=[
        IndexerStat(name="Quiet", queries_24h=0, successful_24h=0, failed_24h=0),
    ])
    assert diagnose(idle, wanted_total=0) == []


def test_a_few_failures_are_normal_and_stay_quiet():
    """Trackers flake; only a sustained majority-failure rate is a problem."""
    flaky = _health(indexers=[
        IndexerStat(name="RuTracker.org", queries_24h=200, successful_24h=150, failed_24h=50),
    ])
    assert diagnose(flaky, wanted_total=0) == []


def test_an_oversized_wanted_queue_is_reported():
    """127 wanted items were burning the whole daily quota every pass."""
    problems = diagnose(_health(), wanted_total=127)
    assert any(p.key == "wanted_backlog" for p in problems)
    assert "127" in next(p.text for p in problems if p.key == "wanted_backlog")


def test_a_normal_backlog_stays_quiet():
    assert diagnose(_health(), wanted_total=30) == []


# --------------------------------------------------------------- transitions
@pytest.mark.asyncio
async def test_alert_fires_once_per_problem_not_every_cycle():
    """A sustained outage must not spam — alert on the transition only."""
    sent = []
    monitor = HealthMonitor(notify=AsyncMock(side_effect=lambda text: sent.append(text)))

    broken = _health(service_ready=False)
    await monitor.evaluate(broken, wanted_total=0)
    await monitor.evaluate(broken, wanted_total=0)
    await monitor.evaluate(broken, wanted_total=0)

    assert len(sent) == 1


@pytest.mark.asyncio
async def test_recovery_is_announced():
    sent = []
    monitor = HealthMonitor(notify=AsyncMock(side_effect=lambda text: sent.append(text)))

    await monitor.evaluate(_health(service_ready=False), wanted_total=0)
    await monitor.evaluate(_health(), wanted_total=0)

    assert len(sent) == 2
    assert "восстанов" in sent[1].lower()


@pytest.mark.asyncio
async def test_a_new_problem_alerts_even_while_another_is_open():
    sent = []
    monitor = HealthMonitor(notify=AsyncMock(side_effect=lambda text: sent.append(text)))

    await monitor.evaluate(_health(service_ready=False), wanted_total=0)
    await monitor.evaluate(_health(service_ready=False), wanted_total=200)

    assert len(sent) == 2
    assert "200" in sent[1]


@pytest.mark.asyncio
async def test_a_failing_notify_does_not_wedge_the_monitor():
    monitor = HealthMonitor(notify=AsyncMock(side_effect=Exception("telegram down")))

    await monitor.evaluate(_health(service_ready=False), wanted_total=0)
    await monitor.evaluate(_health(), wanted_total=0)  # must not raise


@pytest.mark.asyncio
async def test_state_is_reported_for_the_health_command():
    monitor = HealthMonitor(notify=AsyncMock())
    await monitor.evaluate(_health(service_ready=False), wanted_total=0)
    assert monitor.state is HealthState.DEGRADED

    await monitor.evaluate(_health(), wanted_total=0)
    assert monitor.state is HealthState.OK


# ------------------------------------------------------------------- /wanted
@pytest.mark.asyncio
async def test_wanted_command_groups_by_title():
    from bot.handlers import status as status_handler

    scryer = AsyncMock()
    scryer.get_wanted = AsyncMock(return_value=(
        [
            ScryerWantedItem(id="1", title_id="t1", title_name="Paw Patrol",
                             content_type=ContentType.SERIES, season_number=1, episode_number=1),
            ScryerWantedItem(id="2", title_id="t1", title_name="Paw Patrol",
                             content_type=ContentType.SERIES, season_number=1, episode_number=2),
            ScryerWantedItem(id="3", title_id="t2", title_name="Pantheon",
                             content_type=ContentType.SERIES, season_number=2, episode_number=1),
        ],
        3, False,
    ))

    message = MagicMock()
    status_msg = MagicMock()
    status_msg.edit_text = AsyncMock()
    message.answer = AsyncMock(return_value=status_msg)

    with patch.object(status_handler, "get_scryer", AsyncMock(return_value=scryer)):
        await status_handler.cmd_wanted(message)

    text = status_msg.edit_text.await_args.args[0]
    assert "Paw Patrol" in text
    assert "2" in text  # two episodes grouped under the title
    assert "Pantheon" in text


@pytest.mark.asyncio
async def test_wanted_command_on_an_empty_queue():
    from bot.handlers import status as status_handler

    scryer = AsyncMock()
    scryer.get_wanted = AsyncMock(return_value=([], 0, False))

    message = MagicMock()
    status_msg = MagicMock()
    status_msg.edit_text = AsyncMock()
    message.answer = AsyncMock(return_value=status_msg)

    with patch.object(status_handler, "get_scryer", AsyncMock(return_value=scryer)):
        await status_handler.cmd_wanted(message)

    assert "пуста" in status_msg.edit_text.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_wanted_command_escapes_title_names():
    from bot.handlers import status as status_handler

    scryer = AsyncMock()
    scryer.get_wanted = AsyncMock(return_value=(
        [ScryerWantedItem(id="1", title_id="t1", title_name="Tom & Jerry <hd>",
                          content_type=ContentType.SERIES)],
        1, False,
    ))

    message = MagicMock()
    status_msg = MagicMock()
    status_msg.edit_text = AsyncMock()
    message.answer = AsyncMock(return_value=status_msg)

    with patch.object(status_handler, "get_scryer", AsyncMock(return_value=scryer)):
        await status_handler.cmd_wanted(message)

    text = status_msg.edit_text.await_args.args[0]
    assert "<hd>" not in text
    assert "&lt;hd&gt;" in text
