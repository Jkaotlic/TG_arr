"""Proactive health monitoring: tell the admins when the stack degrades.

Written after the 2026-07-29 incident. The indexers had been failing since
11:00 — 378 failures an hour, every hour — and nobody found out until a user
search broke at 16:56 and someone read the Scryer logs by hand. Every signal
needed was already available through `systemHealth` and `wantedItems`; nothing
was watching them.

The monitor alerts on *transitions*, not on state: a sustained outage produces
one message, not one per cycle, and recovery is announced too. Anything else
turns into noise the user learns to ignore.
"""

import enum
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import structlog

from bot.models import ScryerHealth

logger = structlog.get_logger()

#: An indexer failing more than this share of its queries is broken, not flaky.
#: The prod signature was 2143/2641 = 81%; normal tracker flakiness sits well
#: under half.
_INDEXER_FAILURE_RATIO = 0.5

#: Below this many queries there isn't enough evidence to call it broken.
_INDEXER_MIN_QUERIES = 20

#: Wanted items above this cost more indexer queries per pass than the daily
#: quota allows (127 items × 3 indexers ≈ 400 queries vs a 500/day limit).
_WANTED_BACKLOG_LIMIT = 60


class HealthState(enum.Enum):
    """Coarse state, surfaced by /health."""

    OK = "ok"
    DEGRADED = "degraded"


@dataclass(frozen=True)
class Problem:
    """One detected problem. `key` identifies it across cycles."""

    key: str
    text: str


def diagnose(health: ScryerHealth, wanted_total: int) -> list[Problem]:
    """Turn a health snapshot into the list of things worth waking someone for."""
    problems: list[Problem] = []

    if not health.service_ready:
        problems.append(Problem("service_down", "🗂 Scryer не готов обслуживать запросы"))

    for stat in health.indexers:
        total = stat.successful_24h + stat.failed_24h
        if total < _INDEXER_MIN_QUERIES:
            continue
        if stat.failure_rate > _INDEXER_FAILURE_RATIO:
            problems.append(Problem(
                f"indexer:{stat.name}",
                f"🔎 {stat.name}: {stat.failed_24h} из {total} запросов падают "
                f"({stat.failure_rate:.0%}) — вероятно, исчерпан суточный лимит",
            ))

    if wanted_total > _WANTED_BACKLOG_LIMIT:
        problems.append(Problem(
            "wanted_backlog",
            f"📋 В очереди поиска {wanted_total} позиций — столько Scryer не осилит "
            f"за сутки, лимиты индексеров выгорят. Посмотрите /wanted",
        ))

    return problems


class HealthMonitor:
    """Tracks which problems are open and notifies on every change."""

    def __init__(self, notify: Callable[[str], Awaitable[None]]):
        self.notify = notify
        self._open: dict[str, Problem] = {}

    @property
    def state(self) -> HealthState:
        return HealthState.DEGRADED if self._open else HealthState.OK

    async def evaluate(self, health: ScryerHealth, wanted_total: int) -> None:
        """Diagnose and announce anything that changed since the last call."""
        current = {p.key: p for p in diagnose(health, wanted_total)}

        for key, problem in current.items():
            if key not in self._open:
                await self._announce(f"⚠️ <b>Проблема</b>\n\n{problem.text}")

        for key, problem in self._open.items():
            if key not in current:
                await self._announce(f"✅ <b>Восстановлено</b>\n\n{problem.text}")

        self._open = current

    async def _announce(self, text: str) -> None:
        """Send one alert. A delivery failure must not stop the monitor —
        otherwise a Telegram blip would leave the state machine stuck."""
        try:
            await self.notify(text)
        except Exception as e:
            logger.warning("health_alert_failed", error=str(e))
            return
        # Logged on success too: otherwise "no alert was needed" and "the alert
        # was sent" are indistinguishable afterwards.
        logger.info("health_alert_sent", text=" ".join(text.split())[:200])
