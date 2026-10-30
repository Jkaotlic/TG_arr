"""One living status message per bot-initiated grab (improvements round 2, E1).

The grab confirmation the user already has is edited in place as the
download moves, instead of new messages piling up:

    ⏳ <title> / Релиз / Жду загрузку…
    ⏳ <title> / Релиз / 📥 43% · 12.3 МБ/с · осталось ~8 мин
    ⏳ <title> / Релиз / ✅ Скачано, импортирую…
    ✅ <title> — в библиотеке / Релиз            (+ «▶️ Смотреть в Emby», E2)

Linking a grab to its torrent (live facts 2026-09-24): Radarr/Sonarr's
``grabbed`` history records carry ``data.guid`` — the very guid the bot pushed
— and ``downloadId``, the torrent hash. That is the exact link; the queue
(matched by release title) is only the fallback. A direct qBittorrent add
(``tgarr-direct``) is linked by tag + torrent name. Unlinked after
``link_timeout`` → the message says so and tracking stops; the regular
library notice then still reaches the user.

Suppression is per chat: the grabber's chat is excluded from the qBit
"download complete" message and from the «в библиотеке» notice for that
``download_id`` (``owner_chats`` / ``claim_import``); other users still get
theirs. Grabs made outside the bot (RSS, Jellyseerr) have no row, so nothing
changes for them.

Edits are throttled (a progress edit only when the 10 % bucket changes AND at
least ``throttle`` seconds passed; state changes go out at once). Edit
outcomes: "message is not modified" is success, a deleted/inaccessible message
stops tracking silently (``lost``), flood control skips the rest of the tick
with nothing persisted, so the same edit is retried next tick.

State lives in the ``live_status`` table (schema v7), so a restart resumes
editing the same message. Maintenance drops rows untouched for 7 days.

The same table also carries plain «в библиотеке» notices (``kind="notice"``)
that wait for their Emby button (E2) — see ``register_notice``.
"""

import html
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal, Optional
from urllib.parse import urlparse

import structlog

from bot.clients.qbittorrent import DIRECT_TAG
from bot.logging_utils import describe_error
from bot.models import TorrentInfo, TorrentState
from bot.ui.formatters.torrent import format_speed_ru

if TYPE_CHECKING:
    from bot.db import Database

logger = structlog.get_logger()

EditOutcome = Literal["ok", "lost", "retry"]
#: ``editor(chat_id, message_id, text, reply_markup) -> outcome``
Editor = Callable[[int, int, str, Any], Awaitable[EditOutcome]]
#: ``emby_link(provider_id) -> url | None``
EmbyLink = Callable[[str], Awaitable[Optional[str]]]
#: ``make_markup(url) -> reply_markup``
MarkupFactory = Callable[[str], Any]

WAITING = "waiting"
DOWNLOADING = "downloading"
STALLED = "stalled"
COMPLETED = "completed"
EMBY = "emby"  # in the library; still looking for the Emby item
IMPORTED = "imported"
FINISHED = "finished"  # direct qBit download done (no library import)
MISSING = "missing"
NOT_IMPORTED = "not_imported"  # downloaded, but no import for a day
UNTRACKED = "untracked"
LOST = "lost"

ACTIVE_STATES = (WAITING, DOWNLOADING, STALLED, COMPLETED, EMBY)
#: States in which a library import is (or was) this row's to announce.
_CLAIMABLE = (WAITING, DOWNLOADING, STALLED, COMPLETED, EMBY, IMPORTED, MISSING, NOT_IMPORTED)

KIND_NOTICE = "notice"

LINE_WAITING = "Жду загрузку…"
_LINE_COMPLETED = "✅ Скачано, импортирую…"
_LINE_MISSING = "❌ Загрузка пропала из очереди"
_LINE_NOT_IMPORTED = "⚠️ Не импортировано — проверь *arr"
_LINE_UNTRACKED = "Статус загрузки не отслеживается — сообщу, когда будет в библиотеке."

#: PERF-03: запас к `created_at` при чтении /history/since. *arr пишет `grabbed` раньше,
#: чем бот создаёт строку, и его часы могут расходиться с нашими.
_HISTORY_MARGIN = timedelta(seconds=300)

#: qBittorrent reports "unknown" ETA as 8640000 s (100 days).
_ETA_UNKNOWN = 8_640_000


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ------------------------------------------------------------------ render
def _eta_text(seconds: Optional[int]) -> Optional[str]:
    if seconds is None or seconds < 0 or seconds >= _ETA_UNKNOWN:
        return None
    if seconds < 60:
        return "меньше минуты"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"~{minutes} мин"
    hours, rest = divmod(minutes, 60)
    return f"~{hours} ч {rest} мин" if rest else f"~{hours} ч"


def progress_line(torrent: TorrentInfo) -> str:
    """``📥 43% · 12.3 МБ/с · осталось ~8 мин`` (speed/ETA only when known)."""
    parts = [f"📥 {int(torrent.progress * 100)}%"]
    if torrent.download_speed > 0:
        parts.append(format_speed_ru(torrent.download_speed))
    eta = _eta_text(torrent.eta)
    if eta:
        parts.append(f"осталось {eta}")
    return " · ".join(parts)


def _release_line(release: Optional[str]) -> str:
    return f"Релиз: <i>{html.escape(release)}</i>" if release else ""


def initial_text(title: str, release: Optional[str], *, status: str = LINE_WAITING) -> str:
    """The confirmation a grab turns into: ``⏳ <title>\\nРелиз…\\nЖду загрузку…``."""
    lines = [f"⏳ <b>{html.escape(title)}</b>", _release_line(release), status]
    return "\n".join(line for line in lines if line)


def render(row: dict[str, Any]) -> str:
    """The message text for a row's current state."""
    if row["kind"] == KIND_NOTICE:
        return str(row.get("detail") or "")
    title = html.escape(row["title"])
    release = _release_line(row.get("release"))
    state = row["state"]
    if state in (IMPORTED, EMBY):
        head, status = f"✅ <b>{title}</b> — в библиотеке", ""
    elif state == FINISHED:
        head, status = f"✅ <b>{title}</b> — скачано", ""
    elif state == MISSING:
        head, status = f"❌ <b>{title}</b>", _LINE_MISSING
    elif state == STALLED:
        head, status = f"⚠️ <b>{title}</b>", str(row.get("detail") or "⚠️ Застряло")
    elif state == NOT_IMPORTED:
        head, status = f"⚠️ <b>{title}</b>", _LINE_NOT_IMPORTED
    else:
        head, status = f"⏳ <b>{title}</b>", str(row.get("detail") or LINE_WAITING)
    return "\n".join(line for line in (head, release, status) if line)


_PRIVATE_SUFFIXES = (".lan", ".local", ".home", ".localhost", ".internal")


def button_base(public_url: Optional[str], internal_url: Optional[str]) -> Optional[str]:
    """Base URL for «▶️ Смотреть в Emby», or None (no button).

    Batch E fix 1: Telegram rejects URL buttons pointing at internal hosts
    (``http://emby:8096`` → BUTTON_URL_INVALID). EMBY_PUBLIC_URL is taken as
    given; the EMBY_URL fallback only when it looks public — http(s), a dotted
    host that isn't localhost/.lan/.local/.home.
    """
    if public_url:
        return public_url.rstrip("/")
    if not internal_url:
        return None
    parsed = urlparse(internal_url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or "." not in host:
        return None
    if host == "localhost" or host.endswith(_PRIVATE_SUFFIXES):
        return None
    return internal_url.rstrip("/")


def _stall_reason(torrent: TorrentInfo) -> str:
    if torrent.state == TorrentState.ERROR:
        return "ошибка qBittorrent"
    if torrent.seeds_total == 0 and torrent.seeds == 0:
        return "нет сидов"
    return "раздача не качается"


# ------------------------------------------------------------------ registration
async def register_grab(
    db: "Database",
    *,
    chat_id: int,
    message_id: int,
    kind: str,
    title: str,
    release: Optional[str],
    arr: str,
    arr_id: Optional[int],
    guid: Optional[str],
    provider_id: Optional[str] = None,
    detail: Optional[str] = None,
    now: Optional[datetime] = None,
) -> str:
    """Start tracking a grab confirmation (already edited to ``initial_text``).

    ``arr`` is "radarr" | "sonarr" | "qbit" (a direct add); ``guid`` is the
    release guid the bot pushed (None for *arr's own search). Returns the key.
    """
    stamp = (now or _utcnow()).isoformat()
    key = f"{int(chat_id)}:{int(message_id)}"
    await db.live_add({
        "key": key, "chat_id": int(chat_id), "message_id": int(message_id), "kind": kind,
        "title": title, "arr": arr, "arr_id": arr_id, "download_id": None, "state": WAITING,
        "progress": None, "created_at": stamp, "updated_at": stamp, "release": release,
        "guid": guid, "provider_id": provider_id, "detail": detail, "edited_at": stamp,
        "stalled_since": None, "emby_tries": 0,
    })
    return key


class LiveStatusService:
    """Advances every living message; driven by ``tick()`` from ``bot.main``."""

    def __init__(
        self,
        db: "Database",
        editor: Editor,
        *,
        radarr: Any = None,
        sonarr: Any = None,
        qbittorrent: Any = None,
        emby_link: Optional[EmbyLink] = None,
        make_markup: Optional[MarkupFactory] = None,
        clock: Callable[[], datetime] = _utcnow,
        link_timeout: float = 600,
        throttle: float = 60,
        stall_after: float = 7200,
        import_timeout: float = 86400,
        emby_attempts: int = 5,
        emby_retry_every: float = 60,
    ):
        self._db = db
        self._edit = editor
        self._arr = {"radarr": radarr, "sonarr": sonarr}
        self._qbit = qbittorrent
        self._emby_link = emby_link
        self._make_markup = make_markup
        self._clock = clock
        self._link_timeout = link_timeout
        self._throttle = throttle
        self._stall_after = stall_after
        self._import_timeout = import_timeout
        self._emby_attempts = emby_attempts
        self._emby_retry_every = emby_retry_every
        # per-tick caches (reset by tick())
        self._history: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._history_since: dict[str, datetime] = {}
        self._queues: dict[str, list[dict[str, Any]]] = {}
        self._emby_cache: dict[str, Optional[str]] = {}

    # ------------------------------------------------------------ queries
    async def owner_chats(self, torrent_hash: str) -> set[int]:
        """Chats whose living message follows this torrent (any case)."""
        wanted = (torrent_hash or "").upper()
        if not wanted:
            return set()
        rows = await self._db.live_rows()
        return {
            int(r["chat_id"]) for r in rows
            if r["kind"] != KIND_NOTICE and (r["download_id"] or "") == wanted and r["state"] != LOST
            and r["state"] != UNTRACKED
        }

    async def pending_direct_chats(self) -> set[int]:
        """Чаты, чьё сообщение о прямом добавлении ещё не нашло свой торрент.

        LOGIC-09: торрент, докачанный до привязки, `owner_chats` ещё не узнаёт.
        """
        rows = await self._db.live_rows((WAITING,))
        return {int(r["chat_id"]) for r in rows if r["kind"] != KIND_NOTICE and r["arr"] == "qbit"}

    # ------------------------------------------------------------ edits
    async def _apply(self, row: dict[str, Any], markup: Any = None, **fields: Any) -> EditOutcome:
        """Edit the message to the row's new state, then persist it.

        Nothing is persisted on ``retry``: the next tick tries again.

        Batch E fix 1: the row is re-read first — an import claimed by the
        library watcher in the meantime must not be overwritten by a stale
        tick edit («Скачано, импортирую…» after «в библиотеке»).
        """
        current = next((r for r in await self._db.live_rows() if r["key"] == row["key"]), None)
        if current is not None and current["state"] != row["state"]:
            row.update(current)
            return "ok"
        now = self._clock().isoformat()
        new_row = {**row, **fields}
        outcome = await self._edit(int(row["chat_id"]), int(row["message_id"]), render(new_row), markup)
        if outcome == "retry":
            return outcome
        if outcome == "lost":
            await self._db.live_update(row["key"], state=LOST, updated_at=now)
            row["state"] = LOST
            return outcome
        fields.update(edited_at=now, updated_at=now)
        await self._db.live_update(row["key"], **fields)
        row.update(fields)
        return outcome

    async def _save(self, row: dict[str, Any], **fields: Any) -> None:
        """Persist without editing."""
        await self._db.live_update(row["key"], **fields)
        row.update(fields)

    # ------------------------------------------------------------ tick
    async def tick(self) -> None:
        """Advance every active row once. Never raises."""
        try:
            rows = await self._db.live_rows(ACTIVE_STATES)
        except Exception as e:
            logger.warning("live_status_rows_failed", error=describe_error(e))
            return
        if not rows:
            return
        self._history, self._queues, self._emby_cache = {}, {}, {}
        self._history_since = self._oldest_rows(rows)
        # Batch E fix 1: flood control is per chat — skip that chat's other
        # rows this tick, keep the rest going.
        flooded: set[int] = set()
        for row in rows:
            if int(row["chat_id"]) in flooded:
                continue
            try:
                outcome = await self._advance(row)
            except Exception as e:
                logger.warning("live_status_advance_failed", key=row["key"], error=describe_error(e))
                continue
            if outcome == "retry":
                flooded.add(int(row["chat_id"]))

    def _oldest_rows(self, rows: list[dict[str, Any]]) -> dict[str, datetime]:
        """Самая старая строка на сервис: от неё читается история этого тика."""
        oldest: dict[str, datetime] = {}
        for row in rows:
            if row["kind"] == KIND_NOTICE or row["state"] == EMBY:
                continue  # история им не нужна, а старая строка удлинила бы чтение
            created = _parse(row["created_at"]) or self._clock()
            arr = row["arr"]
            if arr not in oldest or created < oldest[arr]:
                oldest[arr] = created
        return oldest

    async def _advance(self, row: dict[str, Any]) -> Optional[EditOutcome]:
        state = row["state"]
        if state == EMBY:
            return await self._retry_emby(row)
        if state == WAITING:
            linked = await self._link(row)
            if not linked:
                return await self._maybe_give_up(row)
        if row["state"] == COMPLETED:
            return await self._await_import(row)
        return await self._follow_torrent(row)

    async def _await_import(self, row: dict[str, Any]) -> Optional[EditOutcome]:
        """Downloaded: wait for the import; after ``import_timeout`` say so
        and stop polling (a late import still finishes the message)."""
        outcome = await self._check_imported(row)
        if outcome is not None:
            return outcome
        since = _parse(row.get("updated_at"))
        if since is not None and (self._clock() - since).total_seconds() >= self._import_timeout:
            return await self._apply(row, state=NOT_IMPORTED, detail=_LINE_NOT_IMPORTED)
        return None

    # ------------------------------------------------------------ linking
    async def _cached_history(self, arr: str, event_type: str) -> list[dict[str, Any]]:
        key = (arr, event_type)
        if key not in self._history:
            client = self._arr.get(arr)
            records: list[dict[str, Any]] = []
            if client is not None:
                # PERF-03: раз в 30 с лёгкий /history/since, а не страница /history.
                # Часовой пояс и граница те же, что у LibraryWatcher: UTC с точностью
                # до секунды, «не раньше». Сервис отдаёт старые записи первыми, а
                # привязка ждёт новые первыми.
                since = self._history_since.get(arr, self._clock()) - _HISTORY_MARGIN
                try:
                    records = await client.get_history_since(since, event_type=event_type)
                except Exception as e:
                    logger.debug("live_status_history_failed", arr=arr, error=describe_error(e))
            self._history[key] = (
                sorted(records, key=lambda r: str(r.get("date") or ""), reverse=True)
                if isinstance(records, list) else []
            )
        return self._history[key]

    async def _cached_queue(self, arr: str) -> Optional[list[dict[str, Any]]]:
        """The queue, or None when it could not be read (unknown ≠ empty)."""
        if arr not in self._queues:
            client = self._arr.get(arr)
            if client is None:
                return None
            try:
                self._queues[arr] = await client.get_queue()
            except Exception as e:
                logger.debug("live_status_queue_failed", arr=arr, error=describe_error(e))
                return None
        return self._queues[arr]

    async def _link(self, row: dict[str, Any]) -> bool:
        found = await self._link_direct(row) if row["arr"] == "qbit" else await self._link_arr(row)
        if found is None:
            return False
        download_id, source_title = found
        fields: dict[str, Any] = {
            "download_id": download_id.upper(), "state": DOWNLOADING,
            "updated_at": self._clock().isoformat(),
        }
        if not row.get("release") and source_title:
            # *arr's own search: now we know which release it took.
            fields["release"] = source_title
        await self._save(row, **fields)
        return True

    async def _link_arr(self, row: dict[str, Any]) -> Optional[tuple[str, Optional[str]]]:
        arr = row["arr"]
        id_field = "movieId" if arr == "radarr" else "seriesId"
        created = _parse(row["created_at"]) or self._clock()
        for record in await self._cached_history(arr, "grabbed"):
            download_id = record.get("downloadId")
            if not download_id:
                continue
            data = record.get("data") if isinstance(record.get("data"), dict) else {}
            if row.get("guid"):
                if data.get("guid") == row["guid"]:
                    return str(download_id), record.get("sourceTitle")
                continue
            # *arr's own search: no guid to match — the first grab of this
            # title after the request.
            when = _parse(record.get("date"))
            if (
                record.get(id_field) == row.get("arr_id")
                and when is not None
                and when >= created - timedelta(seconds=120)
            ):
                return str(download_id), record.get("sourceTitle")
        release = (row.get("release") or "").strip().casefold()
        if release:
            for record in await self._cached_queue(arr) or []:
                if str(record.get("title") or "").strip().casefold() == release and record.get("downloadId"):
                    return str(record["downloadId"]), None
        return None

    async def _link_direct(self, row: dict[str, Any]) -> Optional[tuple[str, Optional[str]]]:
        """A direct add: the ``tgarr-direct`` torrent named like the release;
        else (batch E fix 1: tracker titles often differ from the torrent's
        name) the newest tagged torrent added since the grab (−60 s) that no
        other row follows."""
        if self._qbit is None:
            return None
        release = (row.get("release") or "").strip().casefold()
        try:
            torrents = await self._qbit.get_torrents()
        except Exception as e:
            logger.debug("live_status_qbit_failed", error=describe_error(e))
            return None
        owned = {
            str(r["download_id"]).upper() for r in await self._db.live_rows()
            if r["download_id"] and r["key"] != row["key"]
        }
        tagged = [t for t in torrents if DIRECT_TAG in t.tags and t.hash.upper() not in owned]
        for torrent in tagged:
            if torrent.name.strip().casefold() == release:
                return str(torrent.hash), None
        created = _parse(row["created_at"]) or self._clock()
        recent = [
            t for t in tagged
            if t.added_on is not None and t.added_on >= created - timedelta(seconds=60)
        ]
        if recent:
            newest = max(recent, key=lambda t: t.added_on or created)
            return str(newest.hash), None
        return None

    async def _maybe_give_up(self, row: dict[str, Any]) -> Optional[EditOutcome]:
        created = _parse(row["created_at"]) or self._clock()
        if (self._clock() - created).total_seconds() < self._link_timeout:
            return None
        return await self._apply(row, state=UNTRACKED, detail=_LINE_UNTRACKED)

    # ------------------------------------------------------------ torrent
    async def _follow_torrent(self, row: dict[str, Any]) -> Optional[EditOutcome]:
        if self._qbit is None:
            return None
        try:
            torrent = await self._qbit.get_torrent(str(row["download_id"]).lower())
        except Exception as e:
            logger.debug("live_status_qbit_failed", error=describe_error(e))
            return None
        if torrent is None:
            return await self._torrent_gone(row)

        now = self._clock()
        if torrent.progress >= 1.0 or torrent.state in (TorrentState.COMPLETED, TorrentState.SEEDING):
            if row["arr"] == "qbit":
                return await self._apply(row, state=FINISHED, progress=100, detail=None)
            return await self._apply(row, state=COMPLETED, progress=100, detail=_LINE_COMPLETED)

        if torrent.state in (TorrentState.STALLED, TorrentState.ERROR):
            since = _parse(row.get("stalled_since"))
            if since is None:
                await self._save(row, stalled_since=now.isoformat())
            elif row["state"] != STALLED and (now - since).total_seconds() >= self._stall_after:
                return await self._apply(
                    row, state=STALLED, detail=f"⚠️ Застряло: {_stall_reason(torrent)}",
                )
            if row["state"] == STALLED:
                return None
        elif row.get("stalled_since"):
            await self._save(row, stalled_since=None)

        pct = int(torrent.progress * 100)
        if row["state"] == STALLED:  # moving again: say so at once
            return await self._apply(row, state=DOWNLOADING, progress=pct, detail=progress_line(torrent))
        shown = row.get("progress")
        edited = _parse(row.get("edited_at")) or _parse(row["created_at"]) or now
        if shown is not None and pct // 10 == int(shown) // 10:
            return None
        if (now - edited).total_seconds() < self._throttle:
            return None
        return await self._apply(row, progress=pct, detail=progress_line(torrent))

    async def _torrent_gone(self, row: dict[str, Any]) -> Optional[EditOutcome]:
        if row["arr"] == "qbit":
            return await self._apply(row, state=MISSING, detail=_LINE_MISSING)
        imported = await self._check_imported(row)
        if imported is not None:
            return imported
        queue = await self._cached_queue(row["arr"])
        if queue is None:
            return None  # can't tell: try again next tick
        wanted = str(row["download_id"]).upper()
        if any(str(r.get("downloadId") or "").upper() == wanted for r in queue):
            return None
        return await self._apply(row, state=MISSING, detail=_LINE_MISSING)

    async def _check_imported(self, row: dict[str, Any]) -> Optional[EditOutcome]:
        wanted = str(row.get("download_id") or "").upper()
        if not wanted:
            return None
        records = [
            r for r in await self._cached_history(row["arr"], "downloadFolderImported")
            if str(r.get("downloadId") or "").upper() == wanted
        ]
        if not records:
            return None
        return await self._mark_imported(row)

    # ------------------------------------------------------------ import
    async def claim_import(
        self, service: str, download_id: Optional[str], records: list[dict[str, Any]],
    ) -> set[int]:
        """An import landed (from ``LibraryWatcher``): finish the living
        messages that follow it and return their chats, which the regular
        «в библиотеке» notice must skip. Never raises."""
        wanted = (download_id or "").upper()
        if not wanted:
            return set()
        self._emby_cache = {}
        chats: set[int] = set()
        try:
            rows = await self._db.live_rows(_CLAIMABLE)
            for row in rows:
                if row["kind"] == KIND_NOTICE or row["arr"] != service:
                    continue
                if (row["download_id"] or "") != wanted:
                    continue
                if row["state"] not in (IMPORTED, EMBY):
                    outcome = await self._mark_imported(row)
                    if outcome == "lost":
                        continue
                chats.add(int(row["chat_id"]))
        except Exception as e:
            logger.warning("live_status_claim_failed", error=describe_error(e))
        return chats

    async def _mark_imported(self, row: dict[str, Any]) -> EditOutcome:
        url = await self._lookup_emby(row)
        if url is not None:
            return await self._apply(row, self._markup(url), state=IMPORTED, progress=100, detail=None)
        next_state = EMBY if self._emby_link is not None and row.get("provider_id") else IMPORTED
        return await self._apply(row, state=next_state, progress=100, detail=None, emby_tries=1)

    # ------------------------------------------------------------ Emby (E2)
    def _markup(self, url: str) -> Any:
        return self._make_markup(url) if self._make_markup is not None else None

    async def _lookup_emby(self, row: dict[str, Any]) -> Optional[str]:
        provider = row.get("provider_id")
        if self._emby_link is None or not provider:
            return None
        if provider not in self._emby_cache:
            try:
                self._emby_cache[provider] = await self._emby_link(provider)
            except Exception as e:
                logger.debug("live_status_emby_failed", error=describe_error(e))
                self._emby_cache[provider] = None
        return self._emby_cache[provider]

    async def _retry_emby(self, row: dict[str, Any]) -> Optional[EditOutcome]:
        now = self._clock()
        edited = _parse(row.get("edited_at")) or now
        if (now - edited).total_seconds() < self._emby_retry_every:
            return None
        url = await self._lookup_emby(row)
        tries = int(row.get("emby_tries") or 0) + 1
        if url is not None:
            return await self._apply(row, self._markup(url), state=IMPORTED, emby_tries=tries)
        if tries >= self._emby_attempts:
            await self._save(row, state=IMPORTED, emby_tries=tries, updated_at=now.isoformat())
        else:
            # Count the attempt; ``edited_at`` doubles as "last attempt".
            await self._save(row, emby_tries=tries, edited_at=now.isoformat())
        return None

    async def register_notice(
        self, chat_id: int, message_id: int, text: str, provider_id: str, service: str,
    ) -> None:
        """A plain «в библиотеке» notice sent without its Emby button (the
        item wasn't in Emby yet): keep looking for a few minutes and add the
        button in place (E2)."""
        stamp = self._clock().isoformat()
        await self._db.live_add({
            "key": f"{int(chat_id)}:{int(message_id)}", "chat_id": int(chat_id),
            "message_id": int(message_id), "kind": KIND_NOTICE, "title": "", "arr": service,
            "arr_id": None, "download_id": None, "state": EMBY, "progress": None,
            "created_at": stamp, "updated_at": stamp, "release": None, "guid": None,
            "provider_id": provider_id, "detail": text, "edited_at": stamp,
            "stalled_since": None, "emby_tries": 1,
        })

    @property
    def emby_enabled(self) -> bool:
        return self._emby_link is not None

    async def emby_url(self, provider_id: Optional[str]) -> Optional[str]:
        """One immediate lookup (for the regular library notice)."""
        if self._emby_link is None or not provider_id:
            return None
        try:
            return await self._emby_link(provider_id)
        except Exception as e:
            logger.debug("live_status_emby_failed", error=describe_error(e))
            return None

    def markup_for(self, url: Optional[str]) -> Any:
        return self._markup(url) if url else None
