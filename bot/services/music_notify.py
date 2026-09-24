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

_DISC_DIR = re.compile(r"^(?:cd|disc|disk|dvd|диск)[\s._-]*(\d+)\b.*$", re.IGNORECASE)
_DASHES = re.compile(r"\s+[–—]\s+")
#: A trailing ``(..)``/``{..}`` group is a tag only if every word in it is a
#: format word (``(FLAC)``, ``{24bit}``, ``(WEB FLAC 24-96)``) — never a year.
_FORMAT_WORD = (
    r"(?:flac|mp3|aac|alac|ogg|opus|wav|ape|m4a|web|cd|vinyl|lossless|hi-?res|v0|v2"
    r"|\d{2,3}(?:k|kbps)?|\d{2}\s*-?\s*bits?|\d{2}(?:[.,]\d)?\s*khz|\d{2}-\d{2,3})"
)
_FORMAT_GROUP = rf"[(\{{]\s*{_FORMAT_WORD}(?:[\s,/+-]+{_FORMAT_WORD})*\s*[)\}}]"
_TRAILING_TAGS = re.compile(rf"(?:\s*(?:\[[^\]]*\]|{_FORMAT_GROUP}))+\s*$", re.IGNORECASE)
_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,5}$")
#: ``1-01 ...`` (disc-track) or ``01 ...`` / ``01. ...`` / ``01 - ...``.
_TRACK_NO = re.compile(r"^\s*(?:(\d)[-.](\d{2,3})|(\d{1,3}))(?=[\s._-]|$)")


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

#: Share roots, not albums: files lying directly in one are "loose".
#: ``@@xxxxx`` is how slskd shows a peer's share alias.
_ROOT_DIRS = frozenset({"music", "music_readonly", "share", "downloads", "complete"})

_ARTIST_SEP = " - "


def _is_root_dir(part: str) -> bool:
    folded = part.casefold()
    return folded in _ROOT_DIRS or folded.startswith("@@")


def _album_parts(directory: str) -> tuple[list[str], Optional[int]]:
    """Split a directory into the album path and its disc number, if any."""
    parts = split_path(directory)
    if len(parts) > 1:
        match = _DISC_DIR.match(parts[-1])
        if match:
            return parts[:-1], int(match.group(1))
    return parts, None


def _clean_name(part: str) -> str:
    """Dashes unified to `` - ``, trailing ``[..]`` and format ``(..)``/``{..}`` dropped."""
    name = _DASHES.sub(_ARTIST_SEP, part)
    name = _TRAILING_TAGS.sub("", name).strip()
    return name or part.strip()


def album_label(directory: str) -> Optional[str]:
    """``Artist - Album (2023)`` for an album folder, None for loose files.

    The album folder name, disc subfolder collapsed, tags dropped. A name that
    does not carry its artist (no `` - ``: "Greatest Hits", "Live") takes the
    parent folder as the artist, so generic names of different artists stay
    apart. A share root (or no folder at all) is not an album.
    """
    parts, _disc = _album_parts(directory)
    if not parts or _is_root_dir(parts[-1]):
        return None
    name = _clean_name(parts[-1])
    if _ARTIST_SEP not in name and len(parts) > 1 and not _is_root_dir(parts[-2]):
        name = f"{_clean_name(parts[-2])}{_ARTIST_SEP}{name}"
    return name


def album_key(directory: str, username: str = "") -> str:
    """Normalised album folder: the unit a music summary is about.

    Built from `album_label`, case-folded, whitespace collapsed. The peer's
    root path and username are deliberately not part of it: Lidarr switching
    peers mid-album must still give one summary. Loose files (share root, no
    folder) are no album: they are bucketed per peer and directory, so
    different peers' loose files never merge.
    """
    label = album_label(directory)
    if label is not None:
        return ALBUM_KEY_PREFIX + " ".join(label.casefold().split())
    where = "/".join(p.casefold() for p in split_path(directory))
    return f"{ALBUM_KEY_PREFIX}loose:{username.casefold()}:{where}"


def track_identity(key: str) -> str:
    """The part of a track key that says which track it is (see below)."""
    return re.split(r"[\\/]", key, maxsplit=1)[0].casefold()


def album_track_key(directory: str, filename: str) -> str:
    """``<identity>\\<file name>``: identity for counting, file name for display.

    The identity survives what differs between peers: extension and format
    (``.flac``/``.mp3``), punctuation (``01 -``/``01.``), layout (``CD1\\01``
    vs flat ``1-01``). With a leading track number it is disc + number
    (disc 1 when the layout is flat); otherwise the normalised file stem.
    """
    _parts, disc = _album_parts(directory)
    stem = _EXTENSION.sub("", filename)
    match = _TRACK_NO.match(stem)
    if match:
        prefix_disc, number = match.group(1), match.group(2) or match.group(3)
        disc_no = disc or (int(prefix_disc) if prefix_disc else 1)
        identity = f"d{disc_no}t{int(number):03d}"
    else:
        words = re.sub(r"[\W_]+", " ", stem.casefold()).strip()
        identity = f"d{disc or 1}:{words}"
    return f"{identity}\\{filename}"


def batch_key(t: SlskdTransfer) -> str:
    """The album folder — never ``t.batch_id``: live slskd sets one per file."""
    return album_key(t.directory, t.username)


def track_key(t: SlskdTransfer) -> str:
    return album_track_key(t.directory, t.filename)


def track_name(key: str) -> str:
    parts = split_path(key)
    return parts[-1] if parts else key


def album_title(directory: str) -> str:
    """``music\\Artist - Album (2023) [FLAC]`` -> ``Artist — Album (2023)``.

    `album_label` with the first `` - `` turned into an em dash; loose files
    read as "Музыка".
    """
    label = album_label(directory)
    if label is None:
        return "Музыка"
    return label.replace(_ARTIST_SEP, " — ", 1)


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
        # Grouped by track identity, not file name: peers differ in format,
        # punctuation and disc layout for the same track.
        identity = track_identity(key)
        by_track.setdefault(identity, []).append(state)
        shown_as.setdefault(identity, key)
    succeeded = 0
    active = 0
    failed: list[str] = []
    for identity in sorted(by_track):
        key = shown_as[identity]
        outcome = track_outcome(by_track[identity])
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
