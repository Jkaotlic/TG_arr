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
from typing import Any, Optional

import structlog

logger = structlog.get_logger()

#: How many history rows to ask each *arr service for per poll. Comfortably
#: more than a single interval can produce, so nothing is missed between
#: polls; the seen-set keeps re-reads silent.
_HISTORY_PAGE_SIZE = 50

#: Cap on remembered import ids — bounded so a long-running process can't grow
#: the set without limit. Well above `_HISTORY_PAGE_SIZE` so an id is never
#: forgotten while it is still being returned by the history endpoint.
_SEEN_IMPORTS_CAP = 500

#: slskd transfer states that mean "this transfer is over". slskd reports e.g.
#: "Completed, Succeeded" / "Completed, Cancelled" / "Completed, Errored".
_TRANSFER_SUCCEEDED = "Succeeded"
_TRANSFER_CANCELLED = "Cancelled"
_TRANSFER_ERRORED = "Errored"


class LibraryWatcher:
    """Polls Radarr/Sonarr's import history and slskd's transfers, announcing outcomes."""

    def __init__(
        self,
        radarr: Any,
        sonarr: Any,
        *,
        notify: Optional[Callable[[str], Awaitable[None]]] = None,
        get_slskd: Optional[Callable[[], Awaitable[Any]]] = None,
    ):
        self.radarr = radarr
        self.sonarr = sonarr
        self.notify = notify
        self._get_slskd = get_slskd
        #: (service_label, record id) already reported (insertion-ordered, bounded)
        self._seen_imports: dict[tuple[str, Any], None] = {}
        #: slskd "user/file" -> last seen state
        self._transfers: dict[str, str] = {}
        #: Whether `poll()` has completed a cycle yet. *arr's history journal
        #: already contains everything imported before this process started —
        #: unlike slskd's transfer list (which only reports on a *change* of
        #: state, so a first sight is inherently quiet), `poll_once()` reports
        #: a fresh watcher's very first read verbatim by design (see its own
        #: docstring and tests/test_library_watcher.py's
        #: test_library_watcher_reports_imported_media). Left unguarded here,
        #: every process (re)start would announce up to
        #: `_HISTORY_PAGE_SIZE` already-known imports per service as if they
        #: had just landed. `poll()` — what `bot.main._library_watch` actually
        #: drives — seeds `_seen_imports` on its first cycle without
        #: announcing anything, then reports normally from the second cycle on.
        self._imports_primed = False

    # --------------------------------------------------------------- *arr
    async def poll_once(self) -> list[dict[str, Any]]:
        """One history cycle across both services.

        Returns newly-imported records not seen on a previous call, sorted by
        their own `date` field so a mixed movie+episode burst is returned in
        the order it actually happened. Never raises — an outage on one
        service must not stop the other from being checked.
        """
        landed: list[dict[str, Any]] = []
        for label, client in (("radarr", self.radarr), ("sonarr", self.sonarr)):
            try:
                records = await client.get_history(
                    event_type="downloadFolderImported", page_size=_HISTORY_PAGE_SIZE,
                )
            except Exception as e:
                logger.warning("library_watch_imports_failed", service=label, error=str(e))
                continue

            for record in records:
                record_id = record.get("id")
                if record_id is None:
                    continue
                key = (label, record_id)
                if key in self._seen_imports:
                    continue
                self._remember(key)
                landed.append(record)

        landed.sort(key=lambda r: r.get("date") or "")
        return landed

    def _remember(self, key: tuple[str, Any]) -> None:
        self._seen_imports[key] = None
        while len(self._seen_imports) > _SEEN_IMPORTS_CAP:
            self._seen_imports.pop(next(iter(self._seen_imports)))

    async def poll(self) -> None:
        """One full cycle: announce newly-landed *arr imports, then slskd
        transfers. Never raises — a backend outage must not kill the loop."""
        landed = await self.poll_once()
        if self._imports_primed:
            for record in landed:
                title = html.escape(str(record.get("sourceTitle") or "Unknown"))
                await self._announce(f"✅ <b>{title}</b> — в библиотеке.")
        else:
            # First cycle after (re)start: `poll_once()` already recorded
            # every currently-known import in `_seen_imports` above, so this
            # is a silent seed, not a missed announcement — genuinely new
            # imports from here on are still reported normally.
            self._imports_primed = True
        await self._poll_transfers()

    # -------------------------------------------------------------- slskd
    async def _poll_transfers(self) -> None:
        if self._get_slskd is None:
            return
        try:
            slskd = await self._get_slskd()
            if slskd is None:
                return
            transfers = await slskd.get_transfers(include_completed=True)
        except Exception as e:
            logger.warning("library_watch_transfers_failed", error=str(e))
            return

        seen: dict[str, str] = {}
        for transfer in transfers:
            key = f"{transfer.username}/{transfer.filename}"
            state = transfer.state or ""
            seen[key] = state
            previous = self._transfers.get(key)

            # Unknown key, or an unchanged state, is not an event. A key first
            # seen already finished is history (restart), not news.
            if previous is None or previous == state:
                continue

            name = html.escape(transfer.filename)
            if _TRANSFER_SUCCEEDED in state:
                await self._announce(f"🎵 <b>{name}</b> — скачано.")
            elif _TRANSFER_ERRORED in state:
                await self._announce(
                    f"⚠️ 🎵 <b>{name}</b> — не удалось скачать ({html.escape(state)})"
                )
            elif _TRANSFER_CANCELLED in state:
                await self._announce(f"🚫 🎵 <b>{name}</b> — загрузка отменена.")

        # Keys that vanished are dropped silently: slskd prunes its own history,
        # and a restart empties the list wholesale — neither is evidence of a
        # completed download (BUG-02).
        self._transfers = seen

    async def _announce(self, text: str) -> None:
        """Deliver one notification; a failure is logged, never raised.

        State is updated by the caller regardless, so a Telegram blip costs one
        missed message rather than a stuck watcher that repeats forever.
        """
        if self.notify is None:
            return
        try:
            await self.notify(text)
        except Exception as e:
            logger.warning("library_notify_failed", error=str(e))
