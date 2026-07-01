"""Feature #7: /health dashboard (services + disk free + qBittorrent summary)."""

from bot.models import QBittorrentStatus, SystemStatus


def test_format_health_shows_services_disk_and_qbit():
    from bot.handlers.status import _format_health

    statuses = [
        SystemStatus(service="Radarr", available=True, version="5.0"),
        SystemStatus(service="Sonarr", available=False, error="down"),
    ]
    disks = [("/movies", 1_099_511_627_776), ("/tv", None)]
    qbit = QBittorrentStatus(active_downloads=2, download_speed=5_000_000, free_space=500_000_000_000)

    text = _format_health(statuses, disks, qbit)

    assert "Radarr" in text and "✅" in text        # up service
    assert "Sonarr" in text and "❌" in text        # down service
    assert "/movies" in text and "TB" in text        # disk free formatted
    assert "/tv" in text and "N/A" in text           # unknown free space
    assert "qBittorrent" in text and "2" in text     # active downloads


def test_format_health_without_qbit_omits_section():
    from bot.handlers.status import _format_health

    statuses = [SystemStatus(service="Radarr", available=True)]
    text = _format_health(statuses, [], None)
    assert "qBittorrent" not in text
    assert "Radarr" in text
