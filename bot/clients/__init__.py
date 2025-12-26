"""HTTP API clients for external services."""

from bot.clients.emby import EmbyClient
from bot.clients.prowlarr import ProwlarrClient
from bot.clients.qbittorrent import QBittorrentClient
from bot.clients.radarr import RadarrClient
from bot.clients.sonarr import SonarrClient

__all__ = ["EmbyClient", "ProwlarrClient", "RadarrClient", "SonarrClient", "QBittorrentClient"]
