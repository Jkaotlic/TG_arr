"""«🔁 Докачать» under a music summary (M1, improvements round 2).

slskd diagnosis 2026-09-24: Lidarr already retries a failed slskd release by
itself — it blocklists the release and grabs another peer (75 of 77 albums
since 03.09 were imported, 59% on the first try). A button on every failure
would mostly duplicate that. So a summary line gets the button ONLY when:

- its album folder maps to a Lidarr album (Lidarr's `grabbed` history of the
  slskd client: the album key of the release title, else normalised artist +
  album title) — no match, no button;
- that album is still incomplete in Lidarr
  (``statistics.trackFileCount < statistics.trackCount``);
- nothing for it sits in Lidarr's queue (Lidarr's own retry may already be
  there by the time the summary settles).

Any Lidarr error reads as "no button": the summary itself always goes out.

The button carries a 6-char token (`bot.ui.list_token.new_list_token`); the
album id lives in ``notify_kv['music_retry:<token>']``. A tap sends Lidarr's
``AlbumSearch``; taps within `RETRY_COOLDOWN_S` (double tap, or another
recipient of the same summary) answer "already searching" without a second
command.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal, Optional

import structlog

from bot.logging_utils import describe_error
from bot.services import music_notify
from bot.ui.list_token import new_list_token

if TYPE_CHECKING:
    from bot.clients.lidarr import LidarrClient
    from bot.db import Database

logger = structlog.get_logger()

#: ``notify_kv`` key prefix of issued retry tokens.
KV_PREFIX = "music_retry:"
#: At most this many retry buttons under one summary message.
MAX_BUTTONS = 3
#: A button older than this reads as stale (and its token is pruned).
TOKEN_TTL = timedelta(days=7)
#: Taps within this window after a started search do not search again.
RETRY_COOLDOWN_S = 600
#: How many recent `grabbed` records the album mapping looks through.
GRAB_HISTORY_PAGE = 100

_LABEL_TITLE_CAP = 40
_TRAILING_YEAR = re.compile(r"\s*\((?:19|20)\d{2}\)\s*$")

RetryResult = Literal["started", "already", "stale", "unavailable", "failed"]

#: Tokens whose tap is being processed right now (double-tap guard).
_in_flight: set[str] = set()
_in_flight_lock = asyncio.Lock()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class RetryOffer:
    """One button: its callback token and its text."""

    token: str
    label: str


def _norm(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.casefold())


def _is_slskd_grab(record: dict[str, Any]) -> bool:
    data = record.get("data")
    client = data.get("downloadClient") if isinstance(data, dict) else None
    return not client or "slskd" in str(client).casefold()


_BRACKET_GROUP = re.compile(r"\[[^\]]*\]")
_SLASHES = re.compile(r"[\\/]")


def _release_title(source: str) -> str:
    """A grab's release title as an album folder name.

    Live Tubifarry title (2026-09-24): ``Noize MC - Выход в город (2021)
    [FLAC 16bit/44.1kHz] [👤 peer ] [⚡ 2.64MB/s ] [WEB]``. `album_key` reads
    slash and backslash as path separators, so every ``[...]`` group is dropped
    and the remaining slashes (``AC/DC``) become spaces.
    """
    title = _SLASHES.sub(" ", _BRACKET_GROUP.sub(" ", source))
    return " ".join(title.split())


def match_album(batch_key: str, grabs: Sequence[dict[str, Any]]) -> Optional[int]:
    """The Lidarr album id of a music batch, None when nothing matches.

    ``grabs`` are Lidarr `grabbed` history records (newest first) read with
    ``includeArtist``/``includeAlbum``; only the slskd client's count.
    """
    if not batch_key.startswith(music_notify.ALBUM_KEY_PREFIX):
        return None
    label = batch_key[len(music_notify.ALBUM_KEY_PREFIX):]
    if label.startswith("loose:"):
        return None
    wanted = {_norm(label), _norm(_TRAILING_YEAR.sub("", label))} - {""}
    candidates = [g for g in grabs if isinstance(g, dict) and _is_slskd_grab(g)]
    for record in candidates:
        album_id = record.get("albumId")
        if isinstance(album_id, int) and album_id > 0:
            source = _release_title(str(record.get("sourceTitle") or ""))
            if source and music_notify.album_key(source) == batch_key:
                return album_id
    for record in candidates:
        album_id = record.get("albumId")
        if not (isinstance(album_id, int) and album_id > 0):
            continue
        artist = record.get("artist") if isinstance(record.get("artist"), dict) else {}
        album = record.get("album") if isinstance(record.get("album"), dict) else {}
        name, title = str(artist.get("artistName") or ""), str(album.get("title") or "")
        if name and title and _norm(f"{name} {title}") in wanted:
            return album_id
    return None


def _incomplete(album: Any) -> bool:
    stats = album.get("statistics") if isinstance(album, dict) else None
    if not isinstance(stats, dict):
        return False
    try:
        return int(stats.get("trackFileCount") or 0) < int(stats.get("trackCount") or 0)
    except (TypeError, ValueError):
        return False


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


class MusicRetryService:
    """Issues retry buttons for settled music summaries and acts on taps."""

    def __init__(
        self,
        db: "Database",
        get_lidarr: Callable[[], Awaitable[Optional["LidarrClient"]]],
        *,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._db = db
        self._get_lidarr = get_lidarr
        self._clock = clock

    async def offers(
        self, batches: Sequence[tuple[str, music_notify.BatchSummary]],
    ) -> list[RetryOffer]:
        """Buttons for the summary lines that deserve one (see module doc)."""
        wanted = [(key, s) for key, s in batches if s.failed_names and not s.all_cancelled]
        if not wanted:
            return []
        try:
            lidarr: Optional["LidarrClient"] = await self._get_lidarr()
            if lidarr is None:
                return []
            grabs = await lidarr.get_grab_history(page_size=GRAB_HISTORY_PAGE)
        except Exception as e:
            logger.info("music_retry_lookup_failed", error=describe_error(e))
            return []

        offers: list[RetryOffer] = []
        seen: set[int] = set()
        for key, summary in wanted:
            if len(offers) >= MAX_BUTTONS:
                break
            album_id = match_album(key, grabs)
            if album_id is None or album_id in seen:
                continue
            seen.add(album_id)
            try:
                if not _incomplete(await lidarr.get_album(album_id)):
                    continue
                if await lidarr.album_in_queue(album_id):
                    continue
            except Exception as e:
                logger.info("music_retry_check_failed", album_id=album_id, error=describe_error(e))
                continue
            offers.append(await self._issue(album_id, summary.title))
        if offers:
            await self._prune()
        return offers

    async def _issue(self, album_id: int, title: str) -> RetryOffer:
        token = new_list_token()
        entry = {"album_id": album_id, "title": title, "issued_at": self._clock().isoformat()}
        await self._db.kv_set(KV_PREFIX + token, json.dumps(entry, ensure_ascii=False))
        return RetryOffer(token=token, label=f"🔁 Докачать: {_clip(title, _LABEL_TITLE_CAP)}")

    async def _prune(self) -> None:
        """Drop tokens older than `TOKEN_TTL` (the table must not grow forever)."""
        cutoff = self._clock() - TOKEN_TTL
        stale = []
        for key, raw in (await self._db.kv_items(KV_PREFIX)).items():
            issued = _issued_at(raw)
            if issued is None or issued < cutoff:
                stale.append(key)
        await self._db.kv_delete(stale)

    async def retry(self, token: str) -> RetryResult:
        """Handle one tap on a retry button."""
        async with _in_flight_lock:
            if token in _in_flight:
                return "already"
            _in_flight.add(token)
        try:
            return await self._retry(token)
        finally:
            _in_flight.discard(token)

    async def _retry(self, token: str) -> RetryResult:
        now = self._clock()
        raw = await self._db.kv_get(KV_PREFIX + token)
        issued = _issued_at(raw)
        if raw is None or issued is None or now - issued > TOKEN_TTL:
            return "stale"
        try:
            entry = json.loads(raw)
            album_id = int(entry["album_id"])
            last = entry.get("retried_at")
            last_at = datetime.fromisoformat(last) if last else None
        except (ValueError, KeyError, TypeError, AttributeError):
            # M-3: a corrupt row must not leave the tap unanswered.
            logger.warning("music_retry_corrupt_entry", token=token)
            return "stale"
        if last_at and (now - last_at).total_seconds() < RETRY_COOLDOWN_S:
            return "already"
        lidarr = await self._get_lidarr()
        if lidarr is None:
            return "unavailable"
        try:
            await lidarr.search_album(album_id)
        except Exception as e:
            logger.warning("music_retry_search_failed", album_id=album_id, error=describe_error(e))
            return "failed"
        entry["retried_at"] = now.isoformat()
        await self._db.kv_set(KV_PREFIX + token, json.dumps(entry, ensure_ascii=False))
        logger.info("music_retry_started", album_id=album_id)
        return "started"


def _issued_at(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(json.loads(raw)["issued_at"])
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
