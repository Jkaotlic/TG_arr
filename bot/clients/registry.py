"""Client registry for connection pooling and reuse."""

import asyncio
from typing import TYPE_CHECKING, Optional

from bot.config import get_settings

if TYPE_CHECKING:
    from bot.clients.deezer import DeezerClient
    from bot.clients.emby import EmbyClient
    from bot.clients.lidarr import LidarrClient
    from bot.clients.navidrome import NavidromeClient
    from bot.clients.qbittorrent import QBittorrentClient
    from bot.clients.scryer import ScryerClient
    from bot.clients.slskd import SlskdClient
    from bot.clients.tmdb import TMDbClient

# Per-client locks to prevent race conditions in singleton creation
_scryer_lock = asyncio.Lock()
_lidarr_lock = asyncio.Lock()
_slskd_lock = asyncio.Lock()
_navidrome_lock = asyncio.Lock()
_qbittorrent_lock = asyncio.Lock()
_emby_lock = asyncio.Lock()
_tmdb_lock = asyncio.Lock()
_deezer_lock = asyncio.Lock()

# Singleton instances
_scryer: Optional["ScryerClient"] = None
_lidarr: Optional["LidarrClient"] = None
_slskd: Optional["SlskdClient"] = None
_navidrome: Optional["NavidromeClient"] = None
_qbittorrent: Optional["QBittorrentClient"] = None
_emby: Optional["EmbyClient"] = None
_tmdb: Optional["TMDbClient"] = None
_deezer: Optional["DeezerClient"] = None


async def get_scryer() -> "ScryerClient":
    """Get or create the Scryer client singleton.

    One instance per process matters more here than for the old *arr clients:
    the JWT (24h TTL) is cached on the instance, so a second client would mean
    a second login on every cold start.
    """
    global _scryer
    async with _scryer_lock:
        if _scryer is None:
            from bot.clients.scryer import ScryerClient

            settings = get_settings()
            _scryer = ScryerClient(
                settings.scryer_url,
                settings.scryer_username,
                settings.scryer_password,
            )
    return _scryer


async def get_slskd() -> Optional["SlskdClient"]:
    """Get or create slskd client singleton (if configured)."""
    global _slskd
    settings = get_settings()
    if not settings.slskd_enabled:
        return None
    async with _slskd_lock:
        if _slskd is None:
            from bot.clients.slskd import SlskdClient

            _slskd = SlskdClient(
                settings.slskd_url,
                settings.slskd_api_key,
                timeout=settings.slskd_timeout,
                search_timeout=settings.slskd_search_timeout,
            )
    return _slskd


async def get_navidrome() -> Optional["NavidromeClient"]:
    """Get or create Navidrome client singleton (if configured)."""
    global _navidrome
    settings = get_settings()
    if not settings.navidrome_enabled:
        return None
    async with _navidrome_lock:
        if _navidrome is None:
            from bot.clients.navidrome import NavidromeClient

            _navidrome = NavidromeClient(
                settings.navidrome_url,
                settings.navidrome_username,
                settings.navidrome_password,
                timeout=settings.navidrome_timeout,
            )
    return _navidrome


async def get_lidarr() -> Optional["LidarrClient"]:
    """Get or create Lidarr client singleton (if configured)."""
    global _lidarr
    settings = get_settings()
    if not settings.lidarr_enabled:
        return None
    async with _lidarr_lock:
        if _lidarr is None:
            from bot.clients.lidarr import LidarrClient

            _lidarr = LidarrClient(settings.lidarr_url, settings.lidarr_api_key)
    return _lidarr


async def get_deezer() -> Optional["DeezerClient"]:
    """Get or create Deezer client singleton (if enabled)."""
    global _deezer
    settings = get_settings()
    if not settings.deezer_enabled:
        return None
    async with _deezer_lock:
        if _deezer is None:
            from bot.clients.deezer import DeezerClient

            _deezer = DeezerClient()
    return _deezer


async def get_qbittorrent() -> Optional["QBittorrentClient"]:
    """Get or create qBittorrent client singleton (if configured)."""
    global _qbittorrent
    settings = get_settings()
    if not settings.qbittorrent_enabled:
        return None
    async with _qbittorrent_lock:
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
    async with _emby_lock:
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
    async with _tmdb_lock:
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
    global _scryer, _lidarr, _slskd, _navidrome, _qbittorrent, _emby, _tmdb, _deezer

    if _scryer:
        await _scryer.close()
        _scryer = None
    if _slskd:
        await _slskd.close()
        _slskd = None
    if _navidrome:
        await _navidrome.close()
        _navidrome = None
    if _lidarr:
        await _lidarr.close()
        _lidarr = None
    if _qbittorrent:
        await _qbittorrent.close()
        _qbittorrent = None
    if _emby:
        await _emby.close()
        _emby = None
    if _tmdb:
        await _tmdb.close()
        _tmdb = None
    if _deezer:
        await _deezer.close()
        _deezer = None
