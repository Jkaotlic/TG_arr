"""Proactive health monitoring: tell the admins when the stack degrades.

Written after the 2026-07-29 incident. The indexers had been failing since
11:00 — 378 failures an hour, every hour — and nobody found out until a user
search broke at 16:56 and someone read the logs by hand.

Rollback 2026-08-10 (Tasks 14/15): the previous backend's `systemHealth` query
gave one call visibility into per-indexer 24h success/fail ratios and the
wanted-queue size; neither has an *arr equivalent in this rollback's scope —
the queue size is still visible on demand via /wanted
(`bot/handlers/status.py`), just no longer proactively alerted on. What every
*arr client already exposes is `check_connection()` — "is the service even
reachable" — so that is the one signal this monitor watches now.

The monitor alerts on *transitions*, not on state: a sustained outage produces
one message, not one per cycle, and recovery is announced too. Anything else
turns into noise the user learns to ignore.
"""

import asyncio
import enum
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Optional

import structlog

logger = structlog.get_logger()


class HealthState(enum.Enum):
    """Coarse state, surfaced by /health."""

    OK = "ok"
    DEGRADED = "degraded"


@dataclass(frozen=True)
class Problem:
    """One detected problem. `key` identifies it across cycles.

    `resolved_text` is what to say when it goes away — a recovery notice must
    not reuse `text`, or it reads as a contradiction. The live alert on
    2026-07-30 said "✅ Восстановлено 🔎 RuTracker.org: 88 из 88 запросов
    падают (100%)".
    """

    key: str
    text: str
    resolved_text: str


def diagnose(report: dict[str, dict[str, Any]]) -> list[Problem]:
    """Turn a `HealthMonitor.check_all()` report into the list of things
    worth waking someone for: any service `check_connection()` couldn't reach.
    """
    problems: list[Problem] = []
    for name in sorted(report):
        status = report[name]
        if status.get("available"):
            continue
        detail = status.get("error")
        suffix = f": {detail}" if detail else ""
        problems.append(Problem(
            key=f"service:{name}",
            text=f"🔌 {name} недоступен{suffix}",
            resolved_text=f"🔌 {name} снова доступен",
        ))
    return problems


class HealthMonitor:
    """Polls a set of *arr clients and notifies on every availability change."""

    def __init__(
        self,
        clients: dict[str, Any],
        notify: Optional[Callable[[str], Awaitable[None]]] = None,
    ):
        self.clients = clients
        self.notify = notify
        self._open: dict[str, Problem] = {}

    @property
    def state(self) -> HealthState:
        return HealthState.DEGRADED if self._open else HealthState.OK

    async def check_all(self) -> dict[str, dict[str, Any]]:
        """Poll every configured client's own `check_connection()` in parallel.

        Delegates the API-version prefix entirely to each client (Radarr and
        Sonarr answer on `/api/v3`; Prowlarr and Lidarr on `/api/v1`) instead
        of hardcoding it here — a `/api/v3` probe against a healthy Lidarr
        reports "API DOWN" (live-verified 2026-08-10). Prowlarr subclasses
        `BaseAPIClient` directly (not `ArrBaseClient`) but exposes the same
        `check_connection()` contract, so it needs no special-casing either.
        """
        async def _check(name: str, client: Any) -> tuple[str, dict[str, Any]]:
            try:
                available, version, elapsed_ms = await client.check_connection()
                return name, {
                    "available": available,
                    "version": version,
                    "response_time_ms": elapsed_ms,
                }
            except Exception as e:
                logger.warning("health_check_failed", service=name, error=str(e))
                return name, {
                    "available": False,
                    "version": None,
                    "response_time_ms": None,
                    "error": str(e),
                }

        results = await asyncio.gather(*(_check(name, client) for name, client in self.clients.items()))
        return dict(results)

    async def evaluate(self) -> dict[str, dict[str, Any]]:
        """Poll every service and announce anything that changed since the
        last call. Returns the raw report, so a caller doesn't have to poll
        twice if it also wants the per-service detail (e.g. /health)."""
        report = await self.check_all()
        current = {p.key: p for p in diagnose(report)}

        for key, problem in current.items():
            if key not in self._open:
                await self._announce(f"⚠️ <b>Проблема</b>\n\n{problem.text}")

        for key, problem in self._open.items():
            if key not in current:
                await self._announce(f"✅ <b>Восстановлено</b>\n\n{problem.resolved_text}")

        self._open = current
        return report

    async def _announce(self, text: str) -> None:
        """Send one alert. A delivery failure must not stop the monitor —
        otherwise a Telegram blip would leave the state machine stuck."""
        if self.notify is None:
            return
        try:
            await self.notify(text)
        except Exception as e:
            logger.warning("health_alert_failed", error=str(e))
            return
        # Logged on success too: otherwise "no alert was needed" and "the alert
        # was sent" are indistinguishable afterwards.
        logger.info("health_alert_sent", text=" ".join(text.split())[:200])
