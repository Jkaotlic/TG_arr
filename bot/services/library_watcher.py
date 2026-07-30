""""It's in the library now" notifications.

Before this, the only completion notice came from qBittorrent — "the torrent
finished", which is not the same as "Scryer imported it and you can watch it".
Music got nothing at all: slskd is a separate download client that qBittorrent
knows nothing about.

Scryer has no generic webhook (its only notification channel type is
`mediabrowser`, for Emby — re-verified by introspection on 2026-07-30), so this
polls. What it polls matters:

- **Imports come from the journal** (`importHistory`), not from the active
  `downloadQueue`. A finished import leaves the queue, so diffing the queue only
  catches an import if a poll lands inside that window — on the live instance it
  never did, and every real import went unannounced (audit 2026-07-30, BUG-01).
- **Music comes from terminal transfer states**, not from a key disappearing.
  "It vanished from the active list" also describes a cancelled transfer, and a
  slskd restart makes *everything* vanish at once (BUG-02).

State lives in memory: the first poll after a restart only records what it sees,
so history is never replayed at the user.
"""

import html
from collections.abc import Awaitable, Callable
from typing import Any, Optional

import structlog

logger = structlog.get_logger()

#: How many journal rows to ask for per poll. Comfortably more than a single
#: interval can produce, so nothing is missed between polls; the seen-set keeps
#: re-reads silent.
_HISTORY_LIMIT = 25

#: Cap on remembered import ids — bounded so a long-running process can't grow
#: the set without limit. Well above `_HISTORY_LIMIT` so an id is never
#: forgotten while it is still being returned by the journal.
_SEEN_IMPORTS_CAP = 500

#: slskd transfer states that mean "this transfer is over". slskd reports e.g.
#: "Completed, Succeeded" / "Completed, Cancelled" / "Completed, Errored".
_TRANSFER_SUCCEEDED = "Succeeded"
_TRANSFER_CANCELLED = "Cancelled"
_TRANSFER_ERRORED = "Errored"


class LibraryWatcher:
    """Polls Scryer's import journal and slskd's transfers, announcing outcomes."""

    def __init__(
        self,
        notify: Callable[[str], Awaitable[None]],
        get_scryer: Optional[Callable[[], Awaitable[Any]]] = None,
        get_slskd: Optional[Callable[[], Awaitable[Any]]] = None,
    ):
        self.notify = notify
        self._get_scryer = get_scryer
        self._get_slskd = get_slskd
        #: import-record ids already reported (insertion-ordered, bounded)
        self._seen_imports: dict[str, None] = {}
        #: slskd "user/file" -> last seen state
        self._transfers: dict[str, str] = {}
        self._seeded_imports = False

    async def poll(self) -> None:
        """One cycle. Never raises: a backend outage must not kill the loop."""
        await self._poll_imports()
        await self._poll_transfers()

    # ------------------------------------------------------------- Scryer
    async def _poll_imports(self) -> None:
        if self._get_scryer is None:
            return
        try:
            scryer = await self._get_scryer()
            if scryer is None:
                return
            records = await scryer.get_import_history(limit=_HISTORY_LIMIT)
        except Exception as e:
            logger.warning("library_watch_imports_failed", error=str(e))
            return

        # First poll of this process: the journal is history, not news.
        if not self._seeded_imports:
            for record in records:
                self._remember(record.id)
            self._seeded_imports = True
            logger.info("library_watch_seeded", imports=len(records))
            return

        # Oldest first, so a burst is announced in the order it happened.
        for record in reversed(records):
            if record.id in self._seen_imports:
                continue
            if not record.is_finished:
                # Still importing — leave it unrecorded so the next poll, which
                # will see its final decision, reports it.
                continue

            self._remember(record.id)
            title = html.escape(record.source_title)
            if record.is_imported:
                await self._announce(f"✅ <b>{title}</b> — в библиотеке.")
            elif record.is_failed:
                await self._announce(
                    f"⚠️ <b>{title}</b> — импорт не удался: "
                    f"{html.escape(record.failure_reason)[:200]}"
                )
            # SKIPPED (e.g. ALREADY_IMPORTED) is neither news nor a problem.

    def _remember(self, record_id: str) -> None:
        self._seen_imports[record_id] = None
        while len(self._seen_imports) > _SEEN_IMPORTS_CAP:
            self._seen_imports.pop(next(iter(self._seen_imports)))

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
        try:
            await self.notify(text)
        except Exception as e:
            logger.warning("library_notify_failed", error=str(e))
