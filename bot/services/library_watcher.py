""""It's in the library now" notifications.

Before this, the only completion notice came from qBittorrent — "the torrent
finished", which is not the same as "the file is imported and you can watch
it". Music got nothing at all: slskd is a separate download client that
qBittorrent knows nothing about.

Rollback 2026-08-10 (Tasks 14/15): imports now come from Radarr's and
Sonarr's own history journal (`ArrBaseClient.get_history`, `GET
{prefix}/history?eventType=downloadFolderImported`) instead of the previous
backend's. The principle that made this necessary in the first place is
unchanged:

- **Imports come from the history journal**, not from the active download
  queue. A finished import leaves the queue, so diffing the queue only
  catches an import if a poll happens to land inside that window — on the
  live instance it never did, and every real import went unannounced (audit
  2026-07-30, BUG-01).
- **Music comes from terminal transfer states**, not from a key disappearing.
  "It vanished from the active list" also describes a cancelled transfer, and
  a slskd restart makes *everything* vanish at once (BUG-02).

Seen-import ids are scoped per service (Radarr and Sonarr both hand out their
own autoincrementing `id`, so the same integer can legitimately mean two
different records) and, across services, landed records are ordered by their
own `date` field rather than per-client return order, so a mixed movie+
episode burst reads in the order it actually happened.
"""

import html
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal, Optional

import structlog

from bot.clients.base import CircuitBreaker, ServiceConnectionError
from bot.services import music_notify

if TYPE_CHECKING:
    from bot.db import Database

logger = structlog.get_logger()

#: Notification categories, matched against per-user preferences on delivery.
NotifyCategory = Literal["music", "library"]

#: `notify_kv` key: start time of the last successful slskd poll (ISO, UTC).
_SLSKD_WATERMARK_KEY = "slskd_watermark"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


#: `notify_kv` key prefix: newest imported-record date seen per *arr service.
_IMPORT_WATERMARK_PREFIX = "import_watermark:"


def _parse_iso(value: str) -> datetime:
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _record_date(record: dict[str, Any]) -> Optional[datetime]:
    raw = record.get("date")
    if not raw or not isinstance(raw, str):
        return None
    try:
        return _parse_iso(raw)
    except ValueError:
        return None


def _breaker_open(client: Any) -> bool:
    """NOTIF-08: a client whose circuit breaker is open is skipped silently."""
    breaker = getattr(client, "breaker", None)
    return isinstance(breaker, CircuitBreaker) and breaker.is_open


def _log_poll_failure(event: str, error: Exception, **fields: Any) -> None:
    """Connection failures are already logged once by the client's breaker
    (`service_down`); repeating them every poll was the NOTIF-08 log storm."""
    log_fn = logger.debug if isinstance(error, ServiceConnectionError) else logger.warning
    log_fn(event, error=str(error), **fields)


def _episodes(count: int) -> str:
    """``2 серии`` / ``5 серий`` / ``21 серия``."""
    if count % 10 == 1 and count % 100 != 11:
        word = "серия"
    elif 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        word = "серии"
    else:
        word = "серий"
    return f"{count} {word}"


#: How many history rows to ask each *arr service for per poll. Comfortably
#: more than a single interval can produce, so nothing is missed between
#: polls; the seen-set keeps re-reads silent.
_HISTORY_PAGE_SIZE = 50

#: Cap on remembered import ids — bounded so a long-running process can't grow
#: the set without limit. Well above `_HISTORY_PAGE_SIZE` so an id is never
#: forgotten while it is still being returned by the history endpoint.
_SEEN_IMPORTS_CAP = 500


class LibraryWatcher:
    """Polls Radarr/Sonarr's import history and slskd's transfers, announcing outcomes."""

    def __init__(
        self,
        radarr: Any,
        sonarr: Any,
        *,
        notify: Optional[Callable[[str, str], Awaitable[None]]] = None,
        get_slskd: Optional[Callable[[], Awaitable[Any]]] = None,
        db: Optional["Database"] = None,
        clock: Callable[[], datetime] = _utcnow,
        music_settle_seconds: float = 300,
        music_stall_seconds: float = 3600,
    ):
        self.radarr = radarr
        self.sonarr = sonarr
        self.notify = notify
        self._get_slskd = get_slskd
        #: Persisted notification state; music announcements need it (without
        #: a database the slskd side is skipped entirely).
        self._db = db
        self._clock = clock
        self._music_settle_seconds = music_settle_seconds
        self._music_stall_seconds = music_stall_seconds
        #: (service_label, record id) already reported (insertion-ordered, bounded)
        self._seen_imports: dict[tuple[str, Any], None] = {}
        #: Services whose history has been read successfully at least once in
        #: this process. *arr's history journal already contains everything
        #: imported before this process started; `_fetch_imports()` reports a
        #: fresh watcher's first read verbatim by design (the raw detection
        #: primitive), while `poll()` — what `bot.main._library_watch` drives
        #: — treats each service's first *successful* read as a seed (see
        #: `_poll_imports`). Per service, not global: a service that was down
        #: on the first cycle must not be counted as seeded (NOTIF-04).
        self._primed_services: set[str] = set()

    # --------------------------------------------------------------- *arr
    async def _fetch_imports(
        self,
    ) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, Optional[datetime]]]:
        """One history cycle across both services (raw detection, no priming).

        Returns unseen records as ``(service, record)`` sorted by their own
        `date` field (a mixed movie+episode burst reads in the order it
        happened), plus — for every service whose history call *succeeded* —
        the newest record date it returned (None when its history was empty).
        Never raises — an outage on one service must not stop the other.
        """
        landed: list[tuple[str, dict[str, Any]]] = []
        answered: dict[str, Optional[datetime]] = {}
        for label, client in (("radarr", self.radarr), ("sonarr", self.sonarr)):
            if _breaker_open(client):
                continue  # NOTIF-08: known down; the breaker logged it already
            try:
                records = await client.get_history(
                    event_type="downloadFolderImported", page_size=_HISTORY_PAGE_SIZE,
                )
            except Exception as e:
                _log_poll_failure("library_watch_imports_failed", e, service=label)
                continue

            newest: Optional[datetime] = None
            for record in records:
                when = _record_date(record)
                if when is not None and (newest is None or when > newest):
                    newest = when
                record_id = record.get("id")
                if record_id is None:
                    continue
                key = (label, record_id)
                if key in self._seen_imports:
                    continue
                self._remember(key)
                landed.append((label, record))
            answered[label] = newest

        landed.sort(key=lambda pair: pair[1].get("date") or "")
        return landed, answered

    def _remember(self, key: tuple[str, Any]) -> None:
        self._seen_imports[key] = None
        while len(self._seen_imports) > _SEEN_IMPORTS_CAP:
            self._seen_imports.pop(next(iter(self._seen_imports)))

    async def poll(self) -> None:
        """One full cycle: announce newly-landed *arr imports, then slskd
        transfers. Never raises — a backend outage must not kill the loop."""
        await self._poll_imports()
        await self._poll_transfers()

    async def _poll_imports(self) -> None:
        """Announce new *arr imports, one message per download.

        Audit 2026-09-23:
        - NOTIF-04: priming is per service and happens only after that
          service's history call succeeded — a Radarr asleep during the
          first cycle no longer turns its whole history into "news" on wake.
        - With a database, the newest seen import date per service is kept in
          `notify_kv['import_watermark:<service>']`; a restarted watcher
          announces exactly what landed while it was down, nothing older.
          Without one (first ever run, or no db) the first successful read
          is a silent seed.
        - NOTIF-07: records of one poll are grouped by `downloadId` (fallback
          `sourceTitle`), so a season pack is one message, not ten.
        """
        landed, answered = await self._fetch_imports()
        to_announce: list[dict[str, Any]] = []
        for label, newest in answered.items():
            watermark = await self._import_watermark(label)
            records = [record for service, record in landed if service == label]
            if label not in self._primed_services:
                self._primed_services.add(label)
                if watermark is None:
                    records = []  # silent seed
                else:
                    records = [r for r in records if (_record_date(r) or watermark) > watermark]
            to_announce.extend(records)
            if newest is not None and (watermark is None or newest > watermark):
                await self._set_import_watermark(label, newest)

        to_announce.sort(key=lambda r: r.get("date") or "")
        groups: dict[str, list[dict[str, Any]]] = {}
        for record in to_announce:
            key = str(record.get("downloadId") or record.get("sourceTitle") or record.get("id"))
            groups.setdefault(key, []).append(record)
        for records in groups.values():
            title = html.escape(str(records[0].get("sourceTitle") or "Unknown"))
            if len(records) > 1:
                text = f"✅ <b>{title}</b> — в библиотеке ({_episodes(len(records))})."
            else:
                text = f"✅ <b>{title}</b> — в библиотеке."
            await self._announce(text, "library")

    async def _import_watermark(self, label: str) -> Optional[datetime]:
        if self._db is None:
            return None
        raw = await self._db.kv_get(f"{_IMPORT_WATERMARK_PREFIX}{label}")
        return _parse_iso(raw) if raw else None

    async def _set_import_watermark(self, label: str, when: datetime) -> None:
        if self._db is not None:
            await self._db.kv_set(f"{_IMPORT_WATERMARK_PREFIX}{label}", when.isoformat())

    # -------------------------------------------------------------- slskd
    async def _poll_transfers(self) -> None:
        """Record slskd transfer changes and announce settled albums.

        Audit 2026-09-23 (NOTIF-01/02/03/05): one summary per batch instead of
        one message per file. State lives in SQLite (`music_transfer`,
        `music_batch`, `notify_kv['slskd_watermark']`), so a restart neither
        replays history nor loses a half-finished album.

        - A transfer first seen already finished counts only if it ended
          after the watermark (the start of the last successful poll). The
          very first run sets the watermark to "now", so pre-existing history
          stays quiet.
        - Keys that vanish from slskd are simply not updated: slskd prunes its
          own list and a restart empties it wholesale (BUG-02).
        - A failed poll leaves the watermark untouched.
        """
        if self._get_slskd is None or self._db is None:
            return
        poll_started = self._clock()
        try:
            slskd = await self._get_slskd()
            if slskd is None or _breaker_open(slskd):
                return
            transfers = await slskd.get_transfers(include_completed=True)
        except Exception as e:
            _log_poll_failure("library_watch_transfers_failed", e)
            return

        db = self._db
        raw_watermark = await db.kv_get(_SLSKD_WATERMARK_KEY)
        watermark = _parse_iso(raw_watermark) if raw_watermark else poll_started

        known = await db.music_transfer_states()
        changes: list[tuple[str, str, str, str, str]] = []
        for transfer in transfers:
            tid = music_notify.transfer_id(transfer)
            state = transfer.state or ""
            previous = known.get(tid)
            if previous == state:
                continue
            if previous is None and music_notify.is_terminal(state):
                ended = transfer.ended_at
                if ended is None or ended <= watermark:
                    continue  # history, not news
            known[tid] = state
            changes.append((
                tid,
                music_notify.batch_key(transfer),
                music_notify.track_key(transfer),
                state,
                music_notify.album_title(transfer.directory),
            ))
        await db.music_record(changes, poll_started)

        await self._announce_ready_batches(poll_started)
        await db.kv_set(_SLSKD_WATERMARK_KEY, poll_started.isoformat())

    async def _announce_ready_batches(self, now: datetime) -> None:
        """Send one message for every batch that settled (or stalled)."""
        assert self._db is not None
        ready: list[str] = []
        summaries: list[music_notify.BatchSummary] = []
        for batch in await self._db.music_pending_batches():
            summary = music_notify.summarize(batch["title"], batch["tracks"])
            quiet = (now - batch["last_change_at"]).total_seconds()
            settled = summary.active == 0 and quiet >= self._music_settle_seconds
            stalled = quiet >= self._music_stall_seconds
            if not (settled or stalled):
                continue
            ready.append(batch["batch_key"])
            if not summary.all_cancelled:
                summaries.append(summary)

        # Marked before sending: a Telegram blip costs one summary, never a
        # repeat loop.
        await self._db.music_mark_notified(ready, now)
        text = music_notify.format_message(summaries)
        if text:
            await self._announce(text, "music")

    async def _announce(self, text: str, category: NotifyCategory) -> None:
        """Deliver one notification; a failure is logged, never raised.

        ``category`` ("music" | "library") lets the delivery side honour each
        user's preferences (`UserPreferences.notify_music` / `notify_library`,
        audit 2026-09-23 NOTIF-06); ``notify`` is called as
        ``notify(text, category)``.

        State is updated by the caller regardless, so a Telegram blip costs one
        missed message rather than a stuck watcher that repeats forever.
        """
        if self.notify is None:
            return
        try:
            await self.notify(text, category)
        except Exception as e:
            logger.warning("library_notify_failed", error=str(e))
