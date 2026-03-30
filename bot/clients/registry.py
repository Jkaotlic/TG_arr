"""Client registry for connection pooling and reuse."""

import asyncio
from typing import Optional

from bot.config import get_settings

# Module-level lock to prevent race conditions in singleton creation
_lock = asyncio.Lock()

# Singleton instances
_prowlarr: Optional["ProwlarrClient"] = None
_radarr: Optional["RadarrClient"] = None
_sonarr: Optional["SonarrClient"] = None
_qbittorrent: Optional["QBittorrentClient"] = None
_emby: Optional["EmbyClient"] = None
_tmdb: Optional["TMDbClient"] = None


async def get_prowlarr() -> "ProwlarrClient":
    """Get or create Prowlarr client singleton."""
    global _prowlarr
    async with _lock:
        if _prowlarr is None:
            from bot.clients.prowlarr import ProwlarrClient

            settings = get_settings()
            _prowlarr = ProwlarrClient(settings.prowlarr_url, settings.prowlarr_api_key)
    return _prowlarr


async def get_radarr() -> "RadarrClient":
    """Get or create Radarr client singleton."""
    global _radarr
    async with _lock:
        if _radarr is None:
            from bot.clients.radarr import RadarrClient

            settings = get_settings()
            _radarr = RadarrClient(settings.radarr_url, settings.radarr_api_key)
    return _radarr


async def get_sonarr() -> "SonarrClient":
    """Get or create Sonarr client singleton."""
    global _sonarr
    async with _lock:
        if _sonarr is None:
            from bot.clients.sonarr import SonarrClient

            settings = get_settings()
            _sonarr = SonarrClient(settings.sonarr_url, settings.sonarr_api_key)
    return _sonarr


async def get_qbittorrent() -> Optional["QBittorrentClient"]:
    """Get or create qBittorrent client singleton (if configured)."""
    global _qbittorrent
    settings = get_settings()
    if not settings.qbittorrent_enabled:
        return None
    async with _lock:
        if _qbittorrent is None:
            from bot.clients.qbittorrent import QBittorrentClient

            _qbittorrent = QBittorrentClient(
                settings.qbittorrent_url,
                settings.qbittorrent_username,
                settings.qbittorrent_password,
                timeout=settings.qbittorrent_timeout,
            )
    return _qbittorrent


async def get_emby() -> Optional["EmbyClient"]:
    """Get or create Emby client singleton (if configured)."""
    global _emby
    settings = get_settings()
    if not settings.emby_enabled:
        return None
    async with _lock:
        if _emby is None:
            from bot.clients.emby import EmbyClient

            _emby = EmbyClient(
                settings.emby_url,
                settings.emby_api_key,
                timeout=settings.emby_timeout,
            )
    return _emby


async def get_tmdb() -> Optional["TMDbClient"]:
    """Get or create TMDb client singleton (if configured)."""
    global _tmdb
    settings = get_settings()
    if not settings.tmdb_enabled:
        return None
    async with _lock:
        if _tmdb is None:
            from bot.clients.tmdb import TMDbClient

            _tmdb = TMDbClient(
                settings.tmdb_api_key,
                language=settings.tmdb_language,
                proxy_url=settings.tmdb_proxy_url,
            )
    return _tmdb


async def close_all() -> None:
    """Close all client connections. Call on shutdown."""
    global _prowlarr, _radarr, _sonarr, _qbittorrent, _emby, _tmdb

    if _prowlarr:
        await _prowlarr.close()
        _prowlarr = None
    if _radarr:
        await _radarr.close()
        _radarr = None
    if _sonarr:
        await _sonarr.close()
        _sonarr = None
    if _qbittorrent:
        await _qbittorrent.close()
        _qbittorrent = None
    if _emby:
        await _emby.close()
        _emby = None
    if _tmdb:
        await _tmdb.close()
        _tmdb = None
