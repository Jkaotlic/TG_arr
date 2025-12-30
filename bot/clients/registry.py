"""Client registry for connection pooling and reuse."""

from typing import Optional

from bot.config import get_settings

# Singleton instances
_prowlarr: Optional["ProwlarrClient"] = None
_radarr: Optional["RadarrClient"] = None
_sonarr: Optional["SonarrClient"] = None
_qbittorrent: Optional["QBittorrentClient"] = None
_emby: Optional["EmbyClient"] = None
_tmdb: Optional["TMDbClient"] = None


def get_prowlarr() -> "ProwlarrClient":
    """Get or create Prowlarr client singleton."""
    global _prowlarr
    from bot.clients.prowlarr import ProwlarrClient

    settings = get_settings()
    if _prowlarr is None:
        _prowlarr = ProwlarrClient(settings.prowlarr_url, settings.prowlarr_api_key)
    return _prowlarr


def get_radarr() -> "RadarrClient":
    """Get or create Radarr client singleton."""
    global _radarr
    from bot.clients.radarr import RadarrClient

    settings = get_settings()
    if _radarr is None:
        _radarr = RadarrClient(settings.radarr_url, settings.radarr_api_key)
    return _radarr


def get_sonarr() -> "SonarrClient":
    """Get or create Sonarr client singleton."""
    global _sonarr
    from bot.clients.sonarr import SonarrClient

    settings = get_settings()
    if _sonarr is None:
        _sonarr = SonarrClient(settings.sonarr_url, settings.sonarr_api_key)
    return _sonarr


def get_qbittorrent() -> Optional["QBittorrentClient"]:
    """Get or create qBittorrent client singleton (if configured)."""
    global _qbittorrent
    from bot.clients.qbittorrent import QBittorrentClient

    settings = get_settings()
    if not settings.qbittorrent_enabled:
        return None
    if _qbittorrent is None:
        _qbittorrent = QBittorrentClient(
            settings.qbittorrent_url,
            settings.qbittorrent_username,
            settings.qbittorrent_password,
            timeout=settings.qbittorrent_timeout,
        )
    return _qbittorrent


def get_emby() -> Optional["EmbyClient"]:
    """Get or create Emby client singleton (if configured)."""
    global _emby
    from bot.clients.emby import EmbyClient

    settings = get_settings()
    if not settings.emby_enabled:
        return None
    if _emby is None:
        _emby = EmbyClient(
            settings.emby_url,
            settings.emby_api_key,
            timeout=settings.emby_timeout,
        )
    return _emby


def get_tmdb() -> Optional["TMDbClient"]:
    """Get or create TMDb client singleton (if configured)."""
    global _tmdb
    from bot.clients.tmdb import TMDbClient

    settings = get_settings()
    if not settings.tmdb_enabled:
        return None
    if _tmdb is None:
        _tmdb = TMDbClient(settings.tmdb_api_key)
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
