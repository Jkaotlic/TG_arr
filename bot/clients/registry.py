"""Client registry for connection pooling and reuse."""

import asyncio
from typing import TYPE_CHECKING, Optional

from bot.config import get_settings

if TYPE_CHECKING:
    from bot.clients.deezer import DeezerClient
    from bot.clients.emby import EmbyClient
    from bot.clients.emby_sync_hook import EmbySyncHookClient
    from bot.clients.lidarr import LidarrClient
    from bot.clients.navidrome import NavidromeClient
    from bot.clients.prowlarr import ProwlarrClient
    from bot.clients.qbittorrent import QBittorrentClient
    from bot.clients.radarr import RadarrClient
    from bot.clients.slskd import SlskdClient
    from bot.clients.sonarr import SonarrClient
    from bot.clients.tmdb import TMDbClient
    from bot.clients.torrserver import TorrServerClient
    from bot.services.torrserver_service import TorrServerService

# Per-client locks to prevent race conditions in singleton creation
_radarr_lock = asyncio.Lock()
_sonarr_lock = asyncio.Lock()
_prowlarr_lock = asyncio.Lock()
_lidarr_lock = asyncio.Lock()
_slskd_lock = asyncio.Lock()
_navidrome_lock = asyncio.Lock()
_qbittorrent_lock = asyncio.Lock()
_emby_lock = asyncio.Lock()
_tmdb_lock = asyncio.Lock()
_deezer_lock = asyncio.Lock()
_torrserver_lock = asyncio.Lock()
_emby_sync_hook_lock = asyncio.Lock()

# Singleton instances
_radarr: Optional["RadarrClient"] = None
_sonarr: Optional["SonarrClient"] = None
_prowlarr: Optional["ProwlarrClient"] = None
_lidarr: Optional["LidarrClient"] = None
_slskd: Optional["SlskdClient"] = None
_navidrome: Optional["NavidromeClient"] = None
_qbittorrent: Optional["QBittorrentClient"] = None
_emby: Optional["EmbyClient"] = None
_tmdb: Optional["TMDbClient"] = None
_deezer: Optional["DeezerClient"] = None
_torrserver: Optional["TorrServerClient"] = None
_emby_sync_hook: Optional["EmbySyncHookClient"] = None


async def get_radarr() -> "RadarrClient":
    """Get or create the Radarr client singleton."""
    global _radarr
    async with _radarr_lock:
        if _radarr is None:
            from bot.clients.radarr import RadarrClient

            settings = get_settings()
            _radarr = RadarrClient(settings.radarr_url, settings.radarr_api_key)
    return _radarr


async def get_sonarr() -> "SonarrClient":
    """Get or create the Sonarr client singleton."""
    global _sonarr
    async with _sonarr_lock:
        if _sonarr is None:
            from bot.clients.sonarr import SonarrClient

            settings = get_settings()
            _sonarr = SonarrClient(settings.sonarr_url, settings.sonarr_api_key)
    return _sonarr


async def get_prowlarr() -> "ProwlarrClient":
    """Get or create the Prowlarr client singleton."""
    global _prowlarr
    async with _prowlarr_lock:
        if _prowlarr is None:
            from bot.clients.prowlarr import ProwlarrClient

            settings = get_settings()
            _prowlarr = ProwlarrClient(settings.prowlarr_url, settings.prowlarr_api_key)
    return _prowlarr


async def get_scryer() -> None:
    """Temporary bridge — DELETE IN TASK 15.

    Nine modules (handlers/{calendar,music,settings,status,titles,trending},
    handlers/search/services.py, main.py) still import this name at module level.
    Removing it outright turned their import errors into pytest collection errors,
    which aborted the entire suite.

    It deliberately does not return a client: every caller is converted in
    Tasks 8-14, and this raises so a missed one fails loudly instead of silently
    talking to a backend that no longer exists.
    """
    raise RuntimeError(
        "Scryer was removed in the 2026-08-10 rollback. This caller still has "
        "to be converted to the *arr clients — see the rollback plan, Tasks 8-14."
    )


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


async def get_torrserver() -> Optional["TorrServerClient"]:
    """Get or create the TorrServer client singleton (if configured)."""
    global _torrserver
    settings = get_settings()
    if not settings.torrserver_enabled:
        return None
    async with _torrserver_lock:
        if _torrserver is None:
            from bot.clients.torrserver import TorrServerClient

            _torrserver = TorrServerClient(
                settings.torrserver_url,
                settings.torrserver_username,
                settings.torrserver_password,
                timeout=settings.torrserver_timeout,
                search_timeout=settings.torrserver_search_timeout,
            )
    return _torrserver


async def get_emby_sync_hook() -> Optional["EmbySyncHookClient"]:
    """Get or create the Emby sync hook client singleton (if configured)."""
    global _emby_sync_hook
    settings = get_settings()
    if not settings.emby_sync_hook_enabled:
        return None
    async with _emby_sync_hook_lock:
        if _emby_sync_hook is None:
            from bot.clients.emby_sync_hook import EmbySyncHookClient

            _emby_sync_hook = EmbySyncHookClient(
                settings.emby_sync_hook_url,
                settings.emby_sync_hook_token,
                timeout=settings.emby_sync_hook_timeout,
            )
    return _emby_sync_hook


async def get_torrserver_service() -> Optional["TorrServerService"]:
    """Compose the TorrServer client and (optional) sync hook into the service.

    Cheap to build and stateless, so it is assembled per call rather than kept
    as another singleton — the expensive parts (the HTTP clients) are cached.
    """
    client = await get_torrserver()
    if client is None:
        return None
    from bot.services.torrserver_service import TorrServerService

    settings = get_settings()
    return TorrServerService(
        client,
        await get_emby_sync_hook(),
        metadata_timeout=settings.torrserver_metadata_timeout,
    )


async def close_all() -> None:
    """Close all client connections. Call on shutdown."""
    global _radarr, _sonarr, _prowlarr, _lidarr, _slskd, _navidrome, _qbittorrent, _emby, _tmdb, _deezer
    global _torrserver, _emby_sync_hook

    if _radarr:
        await _radarr.close()
        _radarr = None
    if _sonarr:
        await _sonarr.close()
        _sonarr = None
    if _prowlarr:
        await _prowlarr.close()
        _prowlarr = None
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
    if _torrserver:
        await _torrserver.close()
        _torrserver = None
    if _emby_sync_hook:
        await _emby_sync_hook.close()
        _emby_sync_hook = None
