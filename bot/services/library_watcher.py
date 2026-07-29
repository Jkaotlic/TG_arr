""""It's in the library now" notifications.

Before this, the only completion notice came from qBittorrent — "the torrent
finished", which is not the same as "Scryer imported it and you can watch it".
Music got nothing at all: slskd is a separate download client that qBittorrent
knows nothing about.

Scryer has no generic webhook (its only notification channel type is
`mediabrowser`, for Emby), so this polls both backends and reports the
transitions it sees. State lives in memory: after a restart the first poll
only records what is in flight, so the user is never spammed with history.
"""

import html
from collections.abc import Awaitable, Callable
from typing import Any, Optional

import structlog

logger = structlog.get_logger()

#: `importStatus` values that mean the file is in the library.
_IMPORTED = {"IMPORTED", "COMPLETED", "SUCCESS"}

#: …and the ones that mean it isn't, and won't be without help.
_IMPORT_FAILED = {"FAILED", "REJECTED", "ERROR"}


class LibraryWatcher:
    """Polls Scryer imports and slskd transfers, announcing what completed."""

    def __init__(
        self,
        notify: Callable[[str], Awaitable[None]],
        get_scryer: Optional[Callable[[], Awaitable[Any]]] = None,
        get_slskd: Optional[Callable[[], Awaitable[Any]]] = None,
    ):
        self.notify = notify
        self._get_scryer = get_scryer
        self._get_slskd = get_slskd
        #: queue item id -> last seen import status ("" while downloading)
        self._imports: dict[str, str] = {}
        #: slskd "user/file" -> last seen state
        self._transfers: dict[str, str] = {}
        self._seeded_imports = False
        self._seeded_transfers = False

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
            items = await scryer.get_download_queue()
        except Exception as e:
            logger.warning("library_watch_imports_failed", error=str(e))
            return

        seen: dict[str, str] = {}
        for item in items:
            status = (item.import_status or "").upper()
            seen[item.id] = status
            previous = self._imports.get(item.id)

            # Unknown item that is *already* finished: this is the first poll
            # after a restart seeing history, not a fresh event.
            if previous is None:
                continue
            if previous == status:
                continue

            if status in _IMPORTED:
                await self._announce(
                    f"✅ <b>{html.escape(item.title_name)}</b> — в библиотеке."
                )
            elif status in _IMPORT_FAILED:
                reason = item.attention_reason or item.import_status or "причина неизвестна"
                await self._announce(
                    f"⚠️ <b>{html.escape(item.title_name)}</b> — импорт не удался: "
                    f"{html.escape(str(reason))[:150]}"
                )

        self._imports = seen
        self._seeded_imports = True

    # -------------------------------------------------------------- slskd
    async def _poll_transfers(self) -> None:
        if self._get_slskd is None:
            return
        try:
            slskd = await self._get_slskd()
            if slskd is None:
                return
            transfers = await slskd.get_active_transfers()
        except Exception as e:
            logger.warning("library_watch_transfers_failed", error=str(e))
            return

        seen = {f"{t.username}/{t.filename}": t for t in transfers}

        for key, transfer in seen.items():
            previous = self._transfers.get(key)
            if previous is None or previous == transfer.state:
                continue
            if transfer.is_errored:
                await self._announce(
                    f"⚠️ 🎵 <b>{html.escape(transfer.filename)}</b> — "
                    f"не удалось скачать ({html.escape(transfer.state)})"
                )

        # slskd drops finished transfers from the active list, so a key that
        # disappeared without an error state is a successful download.
        for key, previous_state in self._transfers.items():
            if key in seen:
                continue
            filename = key.split("/", 1)[-1]
            if "Errored" in previous_state or "Cancelled" in previous_state:
                continue
            await self._announce(f"🎵 <b>{html.escape(filename)}</b> — скачано.")

        self._transfers = {key: t.state for key, t in seen.items()}
        self._seeded_transfers = True

    async def _announce(self, text: str) -> None:
        """Deliver one notification; a failure is logged, never raised.

        State is updated by the caller regardless, so a Telegram blip costs one
        missed message rather than a stuck watcher that repeats forever.
        """
        try:
            await self.notify(text)
        except Exception as e:
            logger.warning("library_notify_failed", error=str(e))
