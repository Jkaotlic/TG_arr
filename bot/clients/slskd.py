"""slskd (Soulseek) client.

slskd is the download client Lidarr hands music work to, and it is also useful
directly: Soulseek indexes *tracks and albums by filename*, so it answers
"find me this one album" or "find me this track" — queries Lidarr's
artist-centric model can't express.

API notes (slskd 0.24.x, `/api/v0`):

- auth is a static `X-API-KEY` header;
- a search is asynchronous: POST creates it, then the caller polls
  `GET /searches/{id}` until `isComplete`. Responses are grouped per remote
  *user*, each carrying a list of files;
- downloading means enqueueing files from one specific user:
  `POST /transfers/downloads/{username}` with `[{filename, size}]`.
"""

import asyncio
import re
import time
import uuid
from typing import Any, Optional

import structlog

from bot.clients.base import BaseAPIClient
from bot.models import (
    ArtistInfo,
    SlskdFile,
    SlskdSearchResult,
    SlskdTransfer,
    format_bytes,
)

logger = structlog.get_logger()

#: Audio extensions worth surfacing, best format first.
_AUDIO_EXTENSIONS = (".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".aac", ".wma")

#: Quality ranking for sorting: lossless first, then bitrate-ish ordering.
_FORMAT_RANK = {"flac": 100, "wav": 95, "alac": 90, "m4a": 60, "aac": 55, "ogg": 50, "opus": 50, "mp3": 40, "wma": 20}

#: How often to poll a running search.
_POLL_INTERVAL_S = 2.0


def _extension_of(filename: str) -> str:
    """Lowercase extension without the dot; slskd's own `extension` field is
    frequently empty, so it is derived from the filename instead."""
    match = re.search(r"\.([A-Za-z0-9]{1,5})$", filename or "")
    return match.group(1).lower() if match else ""


def _basename(path: str) -> str:
    """Soulseek paths are Windows-style regardless of the peer's OS."""
    return re.split(r"[\\/]", path or "")[-1]


def _dirname(path: str) -> str:
    parts = re.split(r"[\\/]", path or "")
    return parts[-2] if len(parts) > 1 else ""


class SlskdClient(BaseAPIClient):
    """Async client for the slskd (Soulseek) HTTP API."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: Optional[float] = None,
        search_timeout: float = 25.0,
    ):
        super().__init__(base_url, api_key, "slskd")
        self._timeout_override = timeout
        self.search_timeout = search_timeout

    def _get_headers(self) -> dict[str, str]:
        """slskd authenticates with X-API-KEY, not the *arr X-Api-Key spelling."""
        return {
            "X-API-KEY": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "TG_arr-bot/1.0",
        }

    def _get_http_timeout(self) -> float:
        if self._timeout_override is not None:
            return self._timeout_override
        return super()._get_http_timeout()

    async def check_connection(self) -> tuple[bool, Optional[str], Optional[float]]:
        """Health probe: (ok, version, ms). `ok` requires a live Soulseek login —
        a running slskd that is disconnected from the network can't download."""
        start = time.monotonic()
        try:
            result = await self.get("/api/v0/application")
            elapsed = round((time.monotonic() - start) * 1000, 2)
            if not isinstance(result, dict):
                return False, None, elapsed
            version = (result.get("version") or {}).get("current")
            server = result.get("server") or {}
            return bool(server.get("isLoggedIn")), version, elapsed
        except Exception as e:
            elapsed = round((time.monotonic() - start) * 1000, 2)
            logger.warning("health_check_failed", service=self.service_name, error=str(e))
            return False, None, elapsed

    # --------------------------------------------------------------- search
    async def search(
        self,
        query: str,
        *,
        limit: int = 25,
        min_bitrate_free_only: bool = False,
        timeout: Optional[float] = None,
    ) -> list[SlskdSearchResult]:
        """Run a Soulseek search and return the best candidates.

        The search is asynchronous on slskd's side, so this polls until it
        completes or `timeout` (default `search_timeout`) elapses — a Soulseek
        search never really "finishes" quickly, it accumulates responses, so
        cutting it off at a deadline and using what arrived is the norm.
        """
        search_id = str(uuid.uuid4())
        deadline = time.monotonic() + (timeout if timeout is not None else self.search_timeout)

        await self.post(
            "/api/v0/searches",
            json_data={"id": search_id, "searchText": query},
        )

        payload: dict[str, Any] = {}
        while True:
            # Sleep first (the search has just been created and has nothing
            # yet), then poll, then check the deadline — in that order, so a
            # short `search_timeout` still yields ONE poll instead of
            # returning empty-handed without ever having asked.
            remaining = deadline - time.monotonic()
            await asyncio.sleep(max(min(_POLL_INTERVAL_S, remaining), 0.05))
            try:
                response = await self.get(
                    f"/api/v0/searches/{search_id}", params={"includeResponses": "true"}
                )
                payload = response if isinstance(response, dict) else {}
            except Exception as e:
                logger.warning("slskd_search_poll_failed", error=str(e))
                break
            if isinstance(payload, dict) and payload.get("isComplete"):
                break
            if time.monotonic() >= deadline:
                break

        responses = (payload or {}).get("responses") or []
        results = self._flatten_responses(responses, min_free_slot_only=min_bitrate_free_only)
        logger.info(
            "slskd_search_completed",
            query=query[:80],
            responses=len(responses),
            candidates=len(results),
            state=(payload or {}).get("state"),
        )
        return results[:limit]

    def _flatten_responses(
        self, responses: list[dict[str, Any]], *, min_free_slot_only: bool = False
    ) -> list[SlskdSearchResult]:
        """Group each peer's audio files into one album-ish candidate.

        Soulseek returns files, not albums, so files from one user sharing the
        same parent directory are collapsed into a single result — that is the
        unit a user actually wants to download.
        """
        grouped: dict[tuple[str, str], SlskdSearchResult] = {}
        for response in responses:
            username = response.get("username") or "?"
            if min_free_slot_only and not response.get("hasFreeUploadSlot"):
                continue
            for raw in response.get("files") or []:
                filename = raw.get("filename") or ""
                extension = _extension_of(filename) or (raw.get("extension") or "").lower().lstrip(".")
                if f".{extension}" not in _AUDIO_EXTENSIONS:
                    continue
                folder = _dirname(filename)
                key = (username, folder)
                entry = grouped.get(key)
                if entry is None:
                    entry = SlskdSearchResult(
                        username=username,
                        folder=folder,
                        has_free_slot=bool(response.get("hasFreeUploadSlot")),
                        queue_length=response.get("queueLength") or 0,
                        upload_speed=response.get("uploadSpeed") or 0,
                    )
                    grouped[key] = entry
                entry.files.append(
                    SlskdFile(
                        filename=filename,
                        name=_basename(filename),
                        size=raw.get("size") or 0,
                        extension=extension,
                        bitrate=raw.get("bitRate"),
                        bit_depth=raw.get("bitDepth"),
                        sample_rate=raw.get("sampleRate"),
                        length_seconds=raw.get("length"),
                    )
                )

        results = list(grouped.values())
        results.sort(
            key=lambda r: (
                -_FORMAT_RANK.get(r.dominant_format, 0),
                not r.has_free_slot,
                -r.track_count,
                r.queue_length,
                -r.upload_speed,
            )
        )
        return results

    async def lookup_artists(self, query: str, limit: int = 5) -> list[ArtistInfo]:
        """Best-effort artist lookup used only for content-type detection.

        Soulseek has no artist entity — names are inferred from the shared
        folder names, so this is deliberately shallow: it answers "does this
        look like music?", not "which artist is this exactly".
        """
        results = await self.search(query, limit=limit * 4, timeout=min(self.search_timeout, 12.0))
        seen: dict[str, ArtistInfo] = {}
        for result in results:
            name = result.guessed_artist
            if not name or name.casefold() in seen:
                continue
            seen[name.casefold()] = ArtistInfo(mb_id=f"slskd:{name}", name=name)
            if len(seen) >= limit:
                break
        return list(seen.values())

    # ------------------------------------------------------------- download
    async def enqueue(self, username: str, files: list[SlskdFile]) -> bool:
        """Queue files from one peer. Returns True when slskd accepted them."""
        if not files:
            return False
        payload = [{"filename": f.filename, "size": f.size} for f in files]
        try:
            await self.post(f"/api/v0/transfers/downloads/{username}", json_data=payload)
        except Exception as e:
            logger.error("slskd_enqueue_failed", username=username, count=len(files), error=str(e))
            return False
        logger.info(
            "slskd_enqueued",
            username=username,
            count=len(files),
            size=format_bytes(sum(f.size for f in files)),
        )
        return True

    async def get_downloads(self) -> list[dict[str, Any]]:
        """Raw download state, one entry per remote user."""
        result = await self.get("/api/v0/transfers/downloads")
        return result if isinstance(result, list) else []

    async def get_active_transfers(self) -> list[SlskdTransfer]:
        """Flatten slskd's per-user download tree into a list of transfers."""
        transfers: list[SlskdTransfer] = []
        for user_entry in await self.get_downloads():
            username = user_entry.get("username") or "?"
            for directory in user_entry.get("directories") or []:
                for file_entry in directory.get("files") or []:
                    state = file_entry.get("state") or "Unknown"
                    # slskd keeps completed transfers in the list; only report
                    # what is still in flight or waiting.
                    if "Completed" in state and "Errored" not in state:
                        continue
                    size = file_entry.get("size") or 0
                    transferred = file_entry.get("bytesTransferred") or 0
                    transfers.append(
                        SlskdTransfer(
                            username=username,
                            filename=_basename(file_entry.get("filename") or ""),
                            state=state,
                            size=size,
                            transferred=transferred,
                            average_speed=file_entry.get("averageSpeed") or 0,
                        )
                    )
        return transfers
