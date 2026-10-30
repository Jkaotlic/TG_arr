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

import asyncio
import html
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal, Optional

import structlog

from bot.clients.base import CircuitBreaker, ServiceConnectionError
from bot.db import MUSIC_TOMBSTONE_DAYS
from bot.logging_utils import describe_error
from bot.services import music_notify

if TYPE_CHECKING:
    from bot.db import Database
    from bot.services.live_status import LiveStatusService

logger = structlog.get_logger()

#: Notification categories, matched against per-user preferences on delivery.
NotifyCategory = Literal["music", "library"]

#: `notify_kv` key: start time of the last successful slskd poll (ISO, UTC).
_SLSKD_WATERMARK_KEY = "slskd_watermark"

#: Tolerated clock skew between the slskd host and the bot (seconds). Used as
#: the margin below the watermark for first-seen finished transfers, and as
#: slack in the "unobserved gap" test.
_CLOCK_SKEW_MARGIN_S = 120


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
    log_fn(event, error=describe_error(error), error_type=type(error).__name__, **fields)


def _episodes(count: int) -> str:
    """``2 серии`` / ``5 серий`` / ``21 серия``."""
    if count % 10 == 1 and count % 100 != 11:
        word = "серия"
    elif 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        word = "серии"
    else:
        word = "серий"
    return f"{count} {word}"


def provider_id(record: dict[str, Any]) -> Optional[str]:
    """``tmdb.<id>`` / ``tvdb.<id>`` from a history record read with
    ``include_details`` (E2: finding the item in Emby); None without them."""
    movie = record.get("movie")
    if isinstance(movie, dict) and movie.get("tmdbId"):
        return f"tmdb.{movie['tmdbId']}"
    series = record.get("series")
    if isinstance(series, dict) and series.get("tvdbId"):
        return f"tvdb.{series['tvdbId']}"
    return None


#: How many history rows to ask each *arr service for per poll. Comfortably
#: more than a single interval can produce, so nothing is missed between
#: polls; the seen-set keeps re-reads silent.
_HISTORY_PAGE_SIZE = 50

#: Cap on remembered import ids — bounded so a long-running process can't grow
#: the set without limit. Well above `_HISTORY_PAGE_SIZE` so an id is never
#: forgotten while it is still being returned by the history endpoint.
_SEEN_IMPORTS_CAP = 500

#: LOGIC-21: только Sonarr импортирует один downloadId по частям (пак сезона).
#: Фильм Radarr одним файлом, и ждать ради него минуту незачем.
_HELD_SERVICES = frozenset({"sonarr"})

#: Потолок удержания: столько опросов после первого файла. Без него пак, который
#: растёт на каждом опросе, не был бы объявлен никогда.
_HOLD_MAX_POLLS = 3


@dataclass
class _HeldImport:
    """Файлы одного downloadId, ждущие конца импорта."""

    first_poll: int
    records: list[dict[str, Any]] = field(default_factory=list)


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
        poll_interval_seconds: float = 60,
        live: Optional["LiveStatusService"] = None,
        music_retry: Optional[
            Callable[[list[tuple[str, music_notify.BatchSummary]]], Awaitable[list[Any]]]
        ] = None,
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
        #: A gap between successful slskd polls longer than this means time
        #: went unobserved (see `_poll_transfers`).
        self._unobserved_gap_s = 2 * poll_interval_seconds + _CLOCK_SKEW_MARGIN_S
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
        #: E1 (improvements round 2): `LiveStatusService` — an import that
        #: finishes a bot grab's living message is not announced again to
        #: that chat (`claim_import` returns the chats to skip).
        self._live = live
        #: M1 (improvements round 2): ``MusicRetryService.offers`` — retry
        #: buttons for a music summary, passed on as ``retry=``.
        self._music_retry = music_retry
        #: LOGIC-21: (service, downloadId) -> файлы, ждущие конца импорта. Только в
        #: памяти: идентификаторы запомнены и отметка уже впереди, так что после
        #: падения эти файлы не объявляются (и не повторяются). При штатной
        #: остановке их объявляет `flush_held`.
        self._held: dict[tuple[str, str], _HeldImport] = {}
        self._poll_number = 0
        #: Группы, готовые к объявлению и ещё не отправленные, и отправляемая сейчас.
        #: Отмена посреди рассылки не должна терять первые и повторять вторую.
        self._ready: list[tuple[str, list[dict[str, Any]]]] = []
        self._in_flight: Optional["asyncio.Task[None]"] = None

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
        Remembers nothing (BUG-19): that is `_accept_imports`' job, once the
        watermark is written.
        """
        landed: list[tuple[str, dict[str, Any]]] = []
        answered: dict[str, Optional[datetime]] = {}
        for label, client in (("radarr", self.radarr), ("sonarr", self.sonarr)):
            if _breaker_open(client):
                continue  # NOTIF-08: known down; the breaker logged it already
            try:
                # Log review 2026-10-10: with a watermark only newer records are
                # read (`/history/since`); the paged read stays for the first
                # ever run (silent seed) and for watchers without a database.
                watermark = await self._import_watermark(label)
                if watermark is not None:
                    records = await client.get_history_since(
                        watermark, event_type="downloadFolderImported", include_details=True,
                    )
                else:
                    records = await client.get_history(
                        event_type="downloadFolderImported", page_size=_HISTORY_PAGE_SIZE,
                        # E2: the title's provider ids, for the Emby button.
                        include_details=True,
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
                if (label, record_id) in self._seen_imports:
                    continue
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
        transfers. Never raises — a backend outage must not kill the loop.

        BUG-19: то же про ошибки БД. Шаги независимы, и сбой первого не отменяет
        второй. Что не записано, перечитается на следующем опросе.
        """
        for step, name in ((self._poll_imports, "imports"), (self._poll_transfers, "transfers")):
            try:
                await step()
            except Exception as e:
                logger.warning(
                    "library_watch_step_failed", step=name,
                    error=describe_error(e), error_type=type(e).__name__,
                )

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

        Audit 2026-10-30:
        - LOGIC-21: пак, импортируемый дольше одного опроса, остаётся одним
          сообщением (`_collect_groups`).
        - LOGIC-08: порядок намеренный: отметка, потом отправка. Падение между ними
          стоит одного сообщения, а не повтора всей пачки после рестарта; от сбоев
          Telegram защищают повторы в `send_telegram`.
        """
        landed, answered = await self._fetch_imports()
        accepted: list[tuple[str, dict[str, Any]]] = []
        for label, newest in answered.items():
            records = [record for service, record in landed if service == label]
            try:
                fresh = await self._accept_imports(label, records, newest)
            except Exception as e:
                # BUG-19: ничего не запомнено, значит следующий опрос прочитает их снова.
                logger.warning(
                    "library_watch_state_failed", service=label,
                    error=describe_error(e), error_type=type(e).__name__,
                )
                continue
            accepted.extend((label, record) for record in fresh)

        self._ready.extend(self._collect_groups(accepted, answered))
        await self._send_ready()

    async def _send_ready(self) -> None:
        """Объявить готовые группы по очереди.

        Отправка идёт отдельной задачей под `shield`: отмена цикла посреди рассылки
        не обрывает её на половине получателей, поэтому `flush_held` её дожидается
        и не шлёт заново. Сбой одной группы оставляет остальные до следующего опроса.
        """
        while self._ready:
            label, records = self._ready.pop(0)
            self._in_flight = asyncio.ensure_future(self._announce_group(label, records))
            await asyncio.shield(self._in_flight)
            self._in_flight = None

    async def _accept_imports(
        self, label: str, records: list[dict[str, Any]], newest: Optional[datetime],
    ) -> list[dict[str, Any]]:
        """Записать состояние и вернуть записи, которые стоит объявить.

        BUG-19: идентификаторы запоминаются последними. Раньше они запоминались при
        чтении, и ошибка БД между чтением и отправкой теряла импорт навсегда.
        """
        watermark = await self._import_watermark(label)
        fresh = records
        if label not in self._primed_services:
            if watermark is None:
                fresh = []  # silent seed
            else:
                fresh = [r for r in records if (_record_date(r) or watermark) > watermark]
        if newest is not None and (watermark is None or newest > watermark):
            await self._set_import_watermark(label, newest)
        self._primed_services.add(label)
        for record in records:
            self._remember((label, record["id"]))
        return fresh

    def _collect_groups(
        self,
        accepted: list[tuple[str, dict[str, Any]]],
        answered: dict[str, Optional[datetime]],
    ) -> list[tuple[str, list[dict[str, Any]]]]:
        """Группы, готовые к объявлению на этом опросе (LOGIC-21).

        Группа Sonarr с downloadId ждёт опроса без новых файлов, но не дольше
        `_HOLD_MAX_POLLS`. Сервис, который не ответил, о тишине ничего не говорит.
        Остальное группируется в пределах опроса и объявляется сразу.
        """
        self._poll_number += 1
        accepted = sorted(accepted, key=lambda pair: pair[1].get("date") or "")
        ready: dict[tuple[str, str], list[dict[str, Any]]] = {}
        grew: set[tuple[str, str]] = set()
        for label, record in accepted:
            download_id = record.get("downloadId")
            if label in _HELD_SERVICES and download_id:
                key = (label, str(download_id))
                held = self._held.setdefault(key, _HeldImport(first_poll=self._poll_number))
                held.records.append(record)
                grew.add(key)
            else:
                key = (label, str(download_id or record.get("sourceTitle") or record.get("id")))
                ready.setdefault(key, []).append(record)
        for key, held in list(self._held.items()):
            quiet = key not in grew and key[0] in answered
            if quiet or self._poll_number - held.first_poll >= _HOLD_MAX_POLLS:
                ready[key] = held.records
                del self._held[key]
        groups = [(key[0], records) for key, records in ready.items()]
        groups.sort(key=lambda group: group[1][0].get("date") or "")
        return groups

    async def flush_held(self) -> None:
        """Объявить всё удержанное: штатная остановка, следующего опроса не будет."""
        self._ready.extend((key[0], held.records) for key, held in self._held.items())
        self._held.clear()
        try:
            if self._in_flight is not None:
                await asyncio.wait([self._in_flight])
                self._in_flight = None
            await self._send_ready()
        except asyncio.CancelledError:
            # Время на остановку вышло: отправку под `shield` никто больше не ждёт.
            if self._in_flight is not None:
                self._in_flight.cancel()
            raise

    async def _announce_group(self, label: str, records: list[dict[str, Any]]) -> None:
        title = html.escape(str(records[0].get("sourceTitle") or "Unknown"))
        if len(records) > 1:
            text = f"✅ <b>{title}</b> — в библиотеке ({_episodes(len(records))})."
        else:
            text = f"✅ <b>{title}</b> — в библиотеке."
        extra: dict[str, Any] = {}
        download_id = records[0].get("downloadId")
        if self._live is not None and download_id:
            exclude = await self._live.claim_import(label, str(download_id), records)
            if exclude:
                extra["exclude"] = frozenset(exclude)
        provider = provider_id(records[0])
        if provider:
            extra["provider"] = provider
        await self._announce(text, "library", **extra)

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
        first_run = raw_watermark is None
        watermark = poll_started if raw_watermark is None else _parse_iso(raw_watermark)
        # Review round 1: slskd's host clock may run a little behind the
        # bot's, so a transfer that finished between two polls can carry an
        # `endedAt` just before the watermark. Transfers already recorded are
        # matched by id, so the margin can't make anything count twice.
        cutoff = watermark - timedelta(seconds=_CLOCK_SKEW_MARGIN_S)
        tombstone_age = poll_started - timedelta(days=MUSIC_TOMBSTONE_DAYS)

        # Review round 1: time nobody observed (slskd unreachable, or the bot
        # itself down) is not "quiet time". If the last successful poll is
        # much older than one interval, restart the settle/stall timers of
        # the pending albums instead of declaring them stalled on wake.
        if not first_run and (poll_started - watermark).total_seconds() > self._unobserved_gap_s:
            await db.music_touch_pending(poll_started)

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
                if ended is None or ended <= cutoff:
                    continue  # history, not news
            elif (
                previous is None
                and not first_run
                and transfer.requested_at is not None
                and transfer.requested_at <= tombstone_age
            ):
                # DB-02: запрошен раньше предела жизни надгробия, а базе неизвестен,
                # значит чистка убрала его надгробие, пока slskd его держит. Не новость:
                # иначе застрявшая очередь объявлялась бы заново. Сравнение с возрастом,
                # а не с отметкой: часы slskd могут отставать на минуты, и новая
                # закачка не должна из-за этого выпадать. Когда такой файл докачается,
                # он пройдёт по правилу выше как обычная новость.
                continue
            known[tid] = state
            changes.append((
                tid,
                music_notify.batch_key(transfer),
                music_notify.track_key(transfer),
                state,
                music_notify.album_title(transfer.directory),
            ))
        await db.music_record(changes, poll_started)
        if first_run:
            # Review round 1: the very first run seeds whatever slskd already
            # holds (e.g. remote queues that never move) as announced —
            # otherwise the stall rule would report them an hour later.
            # Все неотмеченные, а не только изменения этого опроса: если отметка
            # упала в прошлый раз, изменений уже нет, а засеять их всё равно надо.
            pending = await db.music_pending_batches()
            await db.music_mark_notified(sorted({b["batch_key"] for b in pending}), poll_started)

        await self._announce_ready_batches(poll_started)
        await db.kv_set(_SLSKD_WATERMARK_KEY, poll_started.isoformat())

    async def _announce_ready_batches(self, now: datetime) -> None:
        """Send one message for every batch that settled (or stalled).

        LOGIC-20: батч, объявленный с незавершёнными треками, получает ещё одну,
        итоговую сводку, когда очередь дойдёт до нуля и успокоится. Засеянные батчи
        молчат: у них нет `partial_announced`.
        """
        assert self._db is not None
        ready: list[str] = []
        partial: list[str] = []
        summaries: list[music_notify.BatchSummary] = []
        keyed: list[tuple[str, music_notify.BatchSummary]] = []
        for batch in await self._db.music_pending_batches():
            summary = music_notify.summarize(batch["title"], batch["tracks"])
            quiet = (now - batch["last_change_at"]).total_seconds()
            settled = summary.active == 0 and quiet >= self._music_settle_seconds
            stalled = quiet >= self._music_stall_seconds
            if not (settled or stalled):
                continue
            ready.append(batch["batch_key"])
            if summary.active:
                partial.append(batch["batch_key"])
            if not summary.all_cancelled:
                summaries.append(summary)
                keyed.append((batch["batch_key"], summary))
        for batch in await self._db.music_partial_batches():
            summary = music_notify.summarize(batch["title"], batch["tracks"])
            quiet = (now - batch["last_change_at"]).total_seconds()
            if summary.active or quiet < self._music_settle_seconds:
                continue
            ready.append(batch["batch_key"])
            if not summary.all_cancelled:
                summaries.append(summary)
                keyed.append((batch["batch_key"], summary))

        # Marked before sending: a Telegram blip costs one summary, never a
        # repeat loop.
        await self._db.music_mark_notified(ready, now, partial=partial)
        text = music_notify.format_message(summaries)
        if text:
            extra: dict[str, Any] = {}
            if self._music_retry is not None:
                try:
                    offers = await self._music_retry(keyed)
                except Exception as e:
                    logger.warning("music_retry_offers_failed", error=describe_error(e))
                    offers = []
                if offers:
                    extra["retry"] = offers
            await self._announce(text, "music", **extra)

    async def _announce(self, text: str, category: NotifyCategory, **extra: Any) -> None:
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
            # ``extra`` (``exclude`` chats, Emby ``provider`` id) is passed only
            # when set, so a plain ``notify(text, category)`` keeps working.
            await self.notify(text, category, **extra)
        except Exception as e:
            logger.warning("library_notify_failed", error=describe_error(e))
