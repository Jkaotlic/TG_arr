"""Navidrome client — read-only "is this already in the library?" probe.

Navidrome speaks the Subsonic API. Authentication is the Subsonic token
scheme: send `u` (username), `s` (a random salt) and `t` = md5(password+salt),
so the password itself never travels in the query string.

This client exists for exactly one job: tell the user "you already have this
album" *before* they queue a duplicate download. It never writes.
"""

import hashlib
import secrets
import time
from typing import Any, Optional

import structlog

from bot.clients.base import BaseAPIClient
from bot.models import NavidromeAlbum

logger = structlog.get_logger()

#: Subsonic protocol version and client identifier sent with every call.
_API_VERSION = "1.16.1"
_CLIENT_NAME = "TG_arr"


class NavidromeClient(BaseAPIClient):
    """Minimal read-only Subsonic client for Navidrome."""

    #: Library contents change only after a scan (`ScanSchedule` is 15m by
    #: default), so a short cache is enough to keep repeat lookups cheap.
    _SEARCH_CACHE_TTL = 120.0

    def __init__(self, base_url: str, username: str, password: str, timeout: Optional[float] = None):
        super().__init__(base_url, "", "navidrome")
        self.username = username
        self._password = password
        self._timeout_override = timeout

    def _get_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "User-Agent": "TG_arr-bot/1.0",
        }

    def _get_http_timeout(self) -> float:
        if self._timeout_override is not None:
            return self._timeout_override
        return super()._get_http_timeout()

    def _auth_params(self) -> dict[str, str]:
        """Subsonic token auth: t = md5(password + salt), fresh salt per call.

        md5 is not a security choice here — it is what the Subsonic protocol
        mandates. The point of the scheme is that the plaintext password is not
        in the URL (and therefore not in any proxy/access log).
        """
        salt = secrets.token_hex(8)
        token = hashlib.md5(f"{self._password}{salt}".encode()).hexdigest()  # noqa: S324 -- protocol-mandated
        return {
            "u": self.username,
            "t": token,
            "s": salt,
            "v": _API_VERSION,
            "c": _CLIENT_NAME,
            "f": "json",
        }

    async def _subsonic(self, endpoint: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Call a Subsonic endpoint and unwrap `subsonic-response`.

        Subsonic reports failures *inside* a 200 response (same trap as
        Scryer's GraphQL errors), so the status field is checked explicitly.
        """
        query = self._auth_params()
        query.update(params or {})
        raw = await self.get(f"/rest/{endpoint}", params=query)
        body = raw.get("subsonic-response") if isinstance(raw, dict) else None
        if not isinstance(body, dict):
            return {}
        if body.get("status") != "ok":
            error = body.get("error") or {}
            # Do not log the message verbatim at ERROR: a wrong-credentials
            # reply is expected config drift, not an incident.
            logger.warning(
                "navidrome_api_error",
                endpoint=endpoint,
                code=error.get("code"),
                message=str(error.get("message", ""))[:120],
            )
            return {}
        return body

    async def check_connection(self) -> tuple[bool, Optional[str], Optional[float]]:
        """Health probe: (ok, version, ms)."""
        start = time.monotonic()
        try:
            body = await self._subsonic("ping")
            elapsed = round((time.monotonic() - start) * 1000, 2)
            return bool(body), body.get("serverVersion") or body.get("version"), elapsed
        except Exception as e:
            elapsed = round((time.monotonic() - start) * 1000, 2)
            logger.warning("health_check_failed", service=self.service_name, error=str(e))
            return False, None, elapsed

    async def search_albums(self, query: str, limit: int = 10) -> list[NavidromeAlbum]:
        """Albums matching `query` that are already in the library."""

        async def _fetch() -> list[NavidromeAlbum]:
            body = await self._subsonic(
                "search3",
                {"query": query, "albumCount": limit, "artistCount": 0, "songCount": 0},
            )
            albums = ((body.get("searchResult3") or {}).get("album")) or []
            return [
                NavidromeAlbum(
                    id=str(a.get("id", "")),
                    name=a.get("name") or a.get("album") or "?",
                    artist=a.get("artist") or "",
                    year=a.get("year"),
                    song_count=a.get("songCount") or 0,
                )
                for a in albums
            ]

        return await self._ttl_cached(f"albums:{query.casefold()}", self._SEARCH_CACHE_TTL, _fetch)

    async def has_album(self, artist: str, album: str) -> bool:
        """Whether an album by this artist is already on disk.

        Matching is fuzzy on purpose — tagging differs between releases, so an
        exact string compare would report "not in library" for albums the user
        demonstrably has. Both names must merely be contained in the candidate.
        """
        wanted_album = (album or "").casefold().strip()
        wanted_artist = (artist or "").casefold().strip()
        if not wanted_album:
            return False
        for candidate in await self.search_albums(f"{artist} {album}".strip(), limit=20):
            name = (candidate.name or "").casefold()
            by = (candidate.artist or "").casefold()
            if wanted_album in name and (not wanted_artist or wanted_artist in by):
                return True
        return False

    async def has_artist(self, artist: str) -> bool:
        """Whether the library has anything by this artist."""
        wanted = (artist or "").casefold().strip()
        if not wanted:
            return False
        body = await self._subsonic(
            "search3", {"query": artist, "artistCount": 10, "albumCount": 0, "songCount": 0}
        )
        artists = ((body.get("searchResult3") or {}).get("artist")) or []
        return any(wanted in (a.get("name") or "").casefold() for a in artists)
