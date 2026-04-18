"""Deezer public API client for music trending/discovery (no API key required)."""

import time
from typing import Any, Optional

import structlog

from bot.clients.base import BaseAPIClient

logger = structlog.get_logger()


class DeezerClient(BaseAPIClient):
    """Client for Deezer public API — no auth, used for trending music data."""

    BASE_URL = "https://api.deezer.com"

    def __init__(self) -> None:
        super().__init__(self.BASE_URL, api_key="", service_name="Deezer")

    def _get_headers(self) -> dict[str, str]:
        """Deezer public API doesn't require auth headers."""
        return {
            "Accept": "application/json",
            "User-Agent": "TG_arr-bot/1.0",
        }

    async def get_trending_artists(self, limit: int = 20) -> list[dict[str, Any]]:
        """Get top trending artists from Deezer chart."""
        try:
            result = await self.get(f"/chart/0/artists", params={"limit": limit})
        except Exception as e:
            logger.warning("Deezer trending artists failed", error=str(e))
            return []

        if not isinstance(result, dict):
            return []

        items = result.get("data") or []
        artists: list[dict[str, Any]] = []
        for item in items:
            artists.append({
                "name": item.get("name", "Unknown"),
                "deezer_id": item.get("id"),
                "picture_url": item.get("picture_xl") or item.get("picture_big") or item.get("picture"),
                "fans": item.get("nb_fan"),
                "link": item.get("link"),
            })
        return artists

    async def get_trending_albums(self, limit: int = 20) -> list[dict[str, Any]]:
        """Get top trending albums from Deezer chart."""
        try:
            result = await self.get(f"/chart/0/albums", params={"limit": limit})
        except Exception as e:
            logger.warning("Deezer trending albums failed", error=str(e))
            return []

        if not isinstance(result, dict):
            return []

        items = result.get("data") or []
        albums: list[dict[str, Any]] = []
        for item in items:
            artist_data = item.get("artist") or {}
            albums.append({
                "title": item.get("title", "Unknown"),
                "deezer_id": item.get("id"),
                "artist_name": artist_data.get("name"),
                "cover_url": item.get("cover_xl") or item.get("cover_big") or item.get("cover"),
                "release_date": item.get("release_date"),
                "link": item.get("link"),
            })
        return albums

    async def check_connection(self) -> tuple[bool, Optional[str], Optional[float]]:
        """Check if Deezer API is reachable."""
        start_time = time.monotonic()
        try:
            await self.get("/chart/0/artists", params={"limit": 1})
            elapsed = (time.monotonic() - start_time) * 1000
            return True, "public", round(elapsed, 2)
        except Exception as e:
            elapsed = (time.monotonic() - start_time) * 1000
            logger.warning("Deezer health check failed", error=str(e))
            return False, None, round(elapsed, 2)
