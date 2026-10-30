"""Morning digest (improvements round 2, E3).

One message per local day at ``DIGEST_HOUR`` (``TIMEZONE``) — or, if the bot
was down then, as soon as it is up again the same day before 23:00:

    🌅 Сводка за сутки
    📚 В библиотеку: N          (up to 5 titles, «и ещё M»)
    🎵 Музыка: N альбомов       (album summaries sent in the last 24 h)
    ⚠️ Застряло: …              (qBit stalled/error >24 h, slskd «Queued,
                                 Remotely» >24 h, *arr queue warnings)
    📅 Сегодня выходит: …       (Sonarr calendar, local today)

Only non-empty sections are shown; nothing to say → no message at all. The
day is recorded in ``notify_kv['digest_last_date']`` *before* sending, so a
Telegram blip costs one digest, never a repeat. Each user's muted categories
(``notify_library`` / ``notify_music``) drop their sections, and
``digest_enabled`` («🌅 Утренняя сводка» in /settings) opts out entirely.

LOGIC-07 (аудит 2026-10-30): сервис, упавший к часу сводки, не равен тихому дню.
Пока источник не ответил и не наступил полдень по ``TIMEZONE``, день не отмечается
и проверка повторяется. С полудня уходит то, что есть, со строкой о недоступных
источниках, и день отмечается. Ошибка БД или кода в ``collect()`` повторяется всегда.
"""

import html
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from typing import TYPE_CHECKING, Any, Optional

import structlog

from bot.logging_utils import describe_error
from bot.models import TorrentState, UserPreferences

if TYPE_CHECKING:
    from bot.db import Database

logger = structlog.get_logger()

#: ``notify_kv`` key: the local date (ISO) whose digest was already handled.
DIGEST_KV_KEY = "digest_last_date"

#: Catch-up stops at this local hour: a digest at midnight is noise.
_LAST_HOUR = 23
#: LOGIC-07: до этого местного часа сводка ждёт упавшие источники.
_SOURCE_RETRY_UNTIL_HOUR = 12
_MAX_TITLES = 5
_WINDOW = timedelta(hours=24)
_SLSKD_REMOTE_QUEUE = "Queued, Remotely"


@dataclass
class DigestData:
    library: list[str] = field(default_factory=list)
    music_albums: int = 0
    #: ``(category, line)`` — "library" | "music", filtered per user.
    stuck: list[tuple[str, str]] = field(default_factory=list)
    airing: list[str] = field(default_factory=list)
    #: ``(category, source)`` — источники, не ответившие при сборе, по порядку.
    failed: list[tuple[str, str]] = field(default_factory=list)

    def source_failed(self, category: str, source: str, error: Exception) -> None:
        if (category, source) in self.failed:
            return  # одна строка на источник за проход, сколько бы его вызовов ни упало
        logger.warning("digest_source_failed", source=source, error=describe_error(error))
        self.failed.append((category, source))


def _albums(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        word = "альбом"
    elif 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        word = "альбома"
    else:
        word = "альбомов"
    return f"{count} {word}"


def _wanted(category: str, prefs: UserPreferences) -> bool:
    return (category == "music" and prefs.notify_music != "off") or (
        category == "library" and prefs.notify_library
    )


def render_digest(data: DigestData, prefs: UserPreferences) -> Optional[str]:
    """The digest for one user, or None when there is nothing for them."""
    sections: list[str] = []
    if prefs.notify_library and data.library:
        lines = [f"📚 В библиотеку: {len(data.library)}"]
        lines += [f"• {html.escape(t)}" for t in data.library[:_MAX_TITLES]]
        if len(data.library) > _MAX_TITLES:
            lines.append(f"• и ещё {len(data.library) - _MAX_TITLES}")
        sections.append("\n".join(lines))
    if prefs.notify_music != "off" and data.music_albums:
        sections.append(f"🎵 Музыка: {_albums(data.music_albums)}")
    stuck = [line for category, line in data.stuck if _wanted(category, prefs)]
    if stuck:
        sections.append("\n".join(["⚠️ Застряло:"] + [f"• {html.escape(s)}" for s in stuck]))
    if prefs.notify_library and data.airing:
        sections.append("\n".join(["📅 Сегодня выходит:"] + [f"• {html.escape(a)}" for a in data.airing]))
    unavailable = [source for category, source in data.failed if _wanted(category, prefs)]
    if unavailable:
        sections.append(f"⚠️ Недоступны: {html.escape(', '.join(unavailable))}")
    if not sections:
        return None
    return "🌅 <b>Сводка за сутки</b>\n\n" + "\n\n".join(sections)


def _parse(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DigestService:
    """Collects the day's news and sends it once per local day."""

    def __init__(
        self,
        db: "Database",
        *,
        radarr: Any = None,
        sonarr: Any = None,
        qbittorrent: Any = None,
        get_slskd: Optional[Callable[[], Awaitable[Any]]] = None,
        recipients: Callable[[], Awaitable[list[tuple[int, UserPreferences]]]],
        send: Callable[[int, str], Awaitable[None]],
        clock: Callable[[], datetime] = _utcnow,
        tz: tzinfo = timezone.utc,
        hour: int = 9,
    ):
        self._db = db
        self._radarr = radarr
        self._sonarr = sonarr
        self._qbit = qbittorrent
        self._get_slskd = get_slskd
        self._recipients = recipients
        self._send = send
        self._clock = clock
        self._tz = tz
        self._hour = hour

    # ------------------------------------------------------------ schedule
    def _due_hour(self, local: datetime) -> bool:
        return local.hour >= self._hour and (local.hour < _LAST_HOUR or local.hour == self._hour)

    async def maybe_send(self) -> int:
        """Send today's digest if it is due and not sent yet; returns how many
        users got it. Never raises."""
        local = self._clock().astimezone(self._tz)
        today = local.date().isoformat()
        try:
            if await self._db.kv_get(DIGEST_KV_KEY) == today or not self._due_hour(local):
                return 0
            data = await self.collect()
            if data.failed and local.hour < _SOURCE_RETRY_UNTIL_HOUR:
                logger.info("digest_waits_for_sources", sources=[source for _, source in data.failed])
                return 0
            users = await self._recipients()
            # Marked only once there is something to send (a failed collect is
            # retried next tick), but before sending — a partial send must not
            # repeat for the users who already got it.
            await self._db.kv_set(DIGEST_KV_KEY, today)
        except Exception as e:
            logger.warning("digest_failed", error=describe_error(e))
            return 0
        delivered = 0
        for uid, prefs in users:
            if not prefs.digest_enabled:
                continue
            text = render_digest(data, prefs)
            if text is None:
                continue
            try:
                await self._send(uid, text)
                delivered += 1
            except Exception as e:
                logger.warning("digest_send_failed", user_id=uid, error=describe_error(e))
        logger.info("digest_done", date=today, delivered=delivered)
        return delivered

    # ------------------------------------------------------------ collect
    async def collect(self) -> DigestData:
        now = self._clock()
        since = now - _WINDOW
        data = DigestData()
        data.library = await self._library(since, data)
        # Своя база, не внешний источник: ошибка отсюда идёт в повтор `maybe_send`.
        data.music_albums = await self._db.music_albums_notified_since(since)
        data.stuck = await self._stuck(now, data)
        data.airing = await self._airing(now, data)
        return data

    async def _library(self, since: datetime, data: DigestData) -> list[str]:
        titles: list[str] = []
        for name, client in (("Radarr", self._radarr), ("Sonarr", self._sonarr)):
            if client is None:
                continue
            try:
                # PERF-02: как у LibraryWatcher, лёгкий /history/since. Граница здесь
                # с точностью до секунды, поэтому проверка `when < since` остаётся.
                records = await client.get_history_since(
                    since, event_type="downloadFolderImported", include_details=True,
                )
            except Exception as e:
                data.source_failed("library", name, e)
                continue
            for record in sorted(records, key=lambda r: str(r.get("date") or "")):
                when = _parse(record.get("date"))
                if when is None or when < since:
                    continue
                title = None
                for key in ("movie", "series"):
                    block = record.get(key)
                    if isinstance(block, dict) and block.get("title"):
                        title = str(block["title"])
                        break
                title = title or str(record.get("sourceTitle") or "").strip()
                if title and title not in titles:
                    titles.append(title)
        return titles

    async def _stuck(self, now: datetime, data: DigestData) -> list[tuple[str, str]]:
        lines: list[tuple[str, str]] = []
        old = now - _WINDOW
        if self._qbit is not None:
            try:
                torrents = await self._qbit.get_torrents()
                count = sum(
                    1 for t in torrents
                    if t.state in (TorrentState.STALLED, TorrentState.ERROR)
                    and t.progress < 1.0
                    and t.added_on is not None and t.added_on <= old
                )
                if count:
                    lines.append(("library", f"торренты без движения больше суток: {count}"))
            except Exception as e:
                data.source_failed("library", "qBittorrent", e)
        if self._get_slskd is not None:
            try:
                slskd = await self._get_slskd()
                if slskd is not None:
                    transfers = await slskd.get_transfers()
                    count = sum(
                        1 for t in transfers
                        if t.state == _SLSKD_REMOTE_QUEUE
                        and t.requested_at is not None and t.requested_at <= old
                    )
                    if count:
                        lines.append(("music", f"музыка в очереди у пиров больше суток: {count}"))
            except Exception as e:
                data.source_failed("music", "slskd", e)
        for name, client in (("Radarr", self._radarr), ("Sonarr", self._sonarr)):
            if client is None:
                continue
            try:
                queue = await client.get_queue()
            except Exception as e:
                data.source_failed("library", name, e)
                continue
            count = sum(
                1 for r in queue
                if str(r.get("trackedDownloadStatus") or "").lower() == "warning"
                or str(r.get("status") or "").lower() == "warning"
            )
            if count:
                lines.append(("library", f"{name}: {count} с предупреждением"))
        return lines

    async def _airing(self, now: datetime, data: DigestData) -> list[str]:
        if self._sonarr is None:
            return []
        local = now.astimezone(self._tz)
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        try:
            # Batch E fix 1: from LOCAL midnight — a UTC-date start missed
            # the 00:00–03:00 MSK episodes.
            episodes = await self._sonarr.get_calendar(
                days=1, start=midnight.astimezone(timezone.utc),
            )
        except Exception as e:
            data.source_failed("library", "Sonarr", e)
            return []
        today = local.date()
        found: list[tuple[datetime, str]] = []
        for ep in episodes:
            when = _parse(ep.get("air_date"))
            if when is None or when.astimezone(self._tz).date() != today:
                continue
            label = f"{ep.get('series_title') or '?'} — S{int(ep.get('season') or 0):02d}E{int(ep.get('episode') or 0):02d}"
            found.append((when, label))
        return [label for _, label in sorted(found)]
