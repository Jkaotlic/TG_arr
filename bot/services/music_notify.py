"""Pure aggregation logic for music (slskd) download notifications.

Audit 2026-09-23, NOTIF-01/02/03/05: the watcher used to send one message per
transfer state change — 185 of 188 notifications in ten days were about
single files. The unit of news is the album, so transfers are grouped by
album folder, a track's outcome is the *best* state across all of its
transfers (a retry that succeeded wins over the earlier failure), and one
summary goes out once the album has settled.

2026-09-24 (live): the album is the *folder*, not slskd's ``batchId``. Live
slskd gives most files their own batchId (one directory of 17 files enqueued
together carried 17 ids), so batchId grouping made every track its own
"album" again. See `album_key`.

Everything here is side-effect free; persistence lives in `bot.db`
(`music_transfer` / `music_batch` / `notify_kv`), orchestration in
`bot.services.library_watcher`.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Iterable, Literal, Optional

from bot.models import SlskdTransfer

Outcome = Literal["succeeded", "active", "failed", "cancelled"]

#: Best first. A track that succeeded once is downloaded, whatever else
#: happened to it; a track still queued is not a failure yet.
_OUTCOME_RANK: dict[str, int] = {"succeeded": 3, "active": 2, "failed": 1, "cancelled": 0}

#: Telegram's hard limit is 4096; leave room for HTML entities.
MESSAGE_CAP = 4000

#: How many failed track names a summary line lists before "и ещё N".
_FAILED_NAMES_SHOWN = 3

#: Per-field clipping (before escaping) so one line can never blow the cap
#: and truncation never cuts through an HTML tag.
_TITLE_CAP = 200
_NAME_CAP = 80

_DISC_DIR = re.compile(r"^(?:cd|disc|disk|dvd|диск)\s*(\d+)$", re.IGNORECASE)
_TRAILING_TAGS = re.compile(r"(\s*\[[^\]]*\])+\s*$")


def split_path(path: str) -> list[str]:
    """Soulseek paths are Windows-style regardless of the peer's OS."""
    return [p for p in re.split(r"[\\/]", path or "") if p]


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def classify(state: str) -> Outcome:
    """Map a raw slskd state ("Completed, Rejected", "Queued, Remotely", ...)."""
    state = state or ""
    if "Succeeded" in state:
        return "succeeded"
    if "Cancelled" in state:
        return "cancelled"
    if "Completed" in state:
        # Errored / Rejected / TimedOut / Aborted — the transfer is over and
        # the file did not arrive.
        return "failed"
    return "active"


def is_terminal(state: str) -> bool:
    return classify(state) != "active"


def track_outcome(states: Iterable[str]) -> Outcome:
    """Best outcome across all transfers of one track."""
    best: Outcome = "cancelled"
    for state in states:
        outcome = classify(state)
        if _OUTCOME_RANK[outcome] > _OUTCOME_RANK[best]:
            best = outcome
    return best


def transfer_id(t: SlskdTransfer) -> str:
    return t.id or f"{t.username}/{t.full_filename or t.filename}"


#: Prefix of album-folder batch keys. Schema-v5 keys (raw slskd batchIds or
#: ``user/dir``) never carry it, which is how ``_migrate_to_v6`` tells them apart.
ALBUM_KEY_PREFIX = "album:"


def _album_parts(directory: str) -> tuple[list[str], Optional[str]]:
    """Split a directory into the album path and its disc subfolder, if any.

    The disc comes back normalised (``CD 2`` / ``Disc2`` -> ``disc2``).
    """
    parts = split_path(directory)
    if len(parts) > 1:
        match = _DISC_DIR.match(parts[-1])
        if match:
            return parts[:-1], f"disc{int(match.group(1))}"
    return parts, None


def _album_name(parts: list[str]) -> str:
    if not parts:
        return ""
    return _TRAILING_TAGS.sub("", parts[-1]).strip() or parts[-1]


def album_key(directory: str) -> str:
    """Normalised album folder: the unit a music summary is about.

    Only the album folder's own name counts (peers share one album from
    different roots), minus a disc subfolder and trailing ``[tags]`` — the
    same reading `album_title` uses — case-folded, whitespace collapsed. The
    peer's username is deliberately not part of it: Lidarr switching peers
    mid-album must still give one summary.
    """
    parts, _disc = _album_parts(directory)
    return ALBUM_KEY_PREFIX + " ".join(_album_name(parts).casefold().split())


def album_track_key(directory: str, filename: str) -> str:
    """Track identity inside its album: ``disc2\\01 - x.flac`` or ``01 - x.flac``.

    Independent of the peer's root path, so the same file fetched from another
    peer is the same track; CD1/CD2 tracks with equal names stay apart.
    """
    _parts, disc = _album_parts(directory)
    return f"{disc}\\{filename}" if disc else filename


def batch_key(t: SlskdTransfer) -> str:
    """The album folder — never ``t.batch_id``: live slskd sets one per file."""
    return album_key(t.directory)


def track_key(t: SlskdTransfer) -> str:
    return album_track_key(t.directory, t.filename)


def track_name(key: str) -> str:
    parts = split_path(key)
    return parts[-1] if parts else key


def album_title(directory: str) -> str:
    """``music\\Artist - Album (2023) [FLAC]`` -> ``Artist — Album (2023)``.

    Takes the last path component (or its parent for ``CD1``-style disc
    folders), drops trailing ``[tags]`` and turns the first `` - `` into an
    em dash.
    """
    parts, _disc = _album_parts(directory)
    if not parts:
        return "Музыка"
    return _album_name(parts).replace(" - ", " — ", 1)


@dataclass
class BatchSummary:
    """What one summary line says about one batch."""

    title: str
    succeeded: int
    failed_names: list[str] = field(default_factory=list)
    active: int = 0

    @property
    def total(self) -> int:
        return self.succeeded + len(self.failed_names) + self.active

    @property
    def all_cancelled(self) -> bool:
        return self.total == 0


def summarize(title: str, tracks: Iterable[tuple[str, str]]) -> BatchSummary:
    """Build a summary from ``(track_key, state)`` rows of one batch.

    Cancelled tracks are left out of the total: the user (or Lidarr) called
    them off, so they are neither downloaded nor failed.
    """
    by_track: dict[str, list[str]] = {}
    shown_as: dict[str, str] = {}
    for key, state in tracks:
        # Peers may spell the same file name in a different case.
        folded = key.casefold()
        by_track.setdefault(folded, []).append(state)
        shown_as.setdefault(folded, key)
    succeeded = 0
    active = 0
    failed: list[str] = []
    for folded in sorted(by_track):
        key = shown_as[folded]
        outcome = track_outcome(by_track[folded])
        if outcome == "succeeded":
            succeeded += 1
        elif outcome == "active":
            active += 1
        elif outcome == "failed":
            failed.append(track_name(key))
    return BatchSummary(title=title, succeeded=succeeded, failed_names=failed, active=active)


def format_batch_line(summary: BatchSummary) -> str:
    """``<b>Artist — Album</b>: 10/12 скачано, 2 не удалось (a, b)``."""
    title = html.escape(_clip(summary.title, _TITLE_CAP))
    text = f"<b>{title}</b>: {summary.succeeded}/{summary.total} скачано"
    failed = summary.failed_names
    if failed:
        shown = ", ".join(html.escape(_clip(n, _NAME_CAP)) for n in failed[:_FAILED_NAMES_SHOWN])
        rest = len(failed) - _FAILED_NAMES_SHOWN
        if rest > 0:
            shown += f" и ещё {rest}"
        text += f", {len(failed)} не удалось ({shown})"
    if summary.active:
        text += f", ещё {summary.active} в очереди"
    return text


def format_message(summaries: list[BatchSummary]) -> Optional[str]:
    """One message for every batch that became ready in a poll cycle."""
    summaries = [s for s in summaries if not s.all_cancelled]
    if not summaries:
        return None
    if len(summaries) == 1:
        # Clipped title and names keep a single line far below the cap.
        return f"🎵 {format_batch_line(summaries[0])}"

    lines = ["🎵 Музыка:"]
    length = len(lines[0])
    for index, summary in enumerate(summaries):
        line = f"• {format_batch_line(summary)}"
        remaining = len(summaries) - index
        tail = f"\n… и ещё {remaining}"
        if length + 1 + len(line) + len(tail) > MESSAGE_CAP:
            lines.append(tail.lstrip("\n"))
            break
        lines.append(line)
        length += 1 + len(line)
    return "\n".join(lines)
