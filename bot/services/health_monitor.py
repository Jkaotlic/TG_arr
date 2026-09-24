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
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

import structlog

if TYPE_CHECKING:
    from bot.db import Database

logger = structlog.get_logger()

#: `notify_kv` key holding the announced-and-still-open problems (JSON).
_OPEN_PROBLEMS_KEY = "health_open"


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
    """Polls a set of *arr clients and notifies on availability changes.

    Audit 2026-09-23 (NOTIF-09):
    - hysteresis — a service is announced down only after two consecutive
      failed checks; after the first one `needs_recheck` asks the caller to
      look again soon (2 min) instead of waiting the full interval;
    - one message per cycle, listing every service that changed;
    - with a database, the open problems are kept in
      `notify_kv['health_open']`, so a restart during an outage does not
      announce it again (and still announces the recovery).
    """

    def __init__(
        self,
        clients: dict[str, Any],
        notify: Optional[Callable[[str], Awaitable[None]]] = None,
        db: Optional["Database"] = None,
    ):
        self.clients = clients
        self.notify = notify
        self._db = db
        self._open: dict[str, Problem] = {}
        #: Failed once, not yet confirmed.
        self._pending: dict[str, Problem] = {}
        self._loaded = db is None

    @property
    def state(self) -> HealthState:
        return HealthState.DEGRADED if (self._open or self._pending) else HealthState.OK

    @property
    def needs_recheck(self) -> bool:
        """A service failed once — check again soon to confirm or clear it."""
        return bool(self._pending)

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
        await self._load()
        report = await self.check_all()
        current = {p.key: p for p in diagnose(report)}

        went_down: list[Problem] = []
        for key, problem in current.items():
            if key in self._open:
                continue
            if key in self._pending:
                went_down.append(problem)
                self._open[key] = problem
                del self._pending[key]
            else:
                self._pending[key] = problem
        for key in [k for k in self._pending if k not in current]:
            del self._pending[key]  # a single blip: never announced
        came_back = [p for k, p in self._open.items() if k not in current]
        for problem in came_back:
            del self._open[problem.key]

        if went_down or came_back:
            parts = []
            if went_down:
                parts.append("⚠️ <b>Проблема</b>\n\n" + "\n".join(p.text for p in went_down))
            if came_back:
                parts.append("✅ <b>Восстановлено</b>\n\n" + "\n".join(p.resolved_text for p in came_back))
            await self._announce("\n\n".join(parts))
            await self._save()
        return report

    async def _load(self) -> None:
        if self._loaded or self._db is None:
            return
        self._loaded = True
        raw = await self._db.kv_get(_OPEN_PROBLEMS_KEY)
        if not raw:
            return
        try:
            stored = json.loads(raw)
            self._open = {
                key: Problem(key=key, text=item["text"], resolved_text=item["resolved_text"])
                for key, item in stored.items()
            }
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            logger.warning("health_open_problems_unreadable", error=str(e))

    async def _save(self) -> None:
        if self._db is None:
            return
        payload = {
            key: {"text": p.text, "resolved_text": p.resolved_text} for key, p in self._open.items()
        }
        try:
            await self._db.kv_set(_OPEN_PROBLEMS_KEY, json.dumps(payload, ensure_ascii=False))
        except Exception as e:
            logger.warning("health_open_problems_save_failed", error=str(e))

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
