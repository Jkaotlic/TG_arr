"""R4 C3-dead-clients: pin dead-code removals and surviving behaviour.

Covers DEAD-02 (ProwlarrClient.grab_release), DEAD-03/04
(LidarrClient.lookup_album / search_album + Formatters.format_album_info),
DEAD-05 (SonarrClient.lookup_series_by_tvdb) and DEAD-06
(DeezerClient.get_trending_albums). The deleted symbols must be gone, while
the live siblings (_parse_album / AlbumInfo, the calendar/lookup paths) keep
working unchanged.
"""

from bot.clients.deezer import DeezerClient
from bot.clients.lidarr import LidarrClient
from bot.clients.prowlarr import ProwlarrClient
from bot.clients.sonarr import SonarrClient
from bot.ui.formatters import Formatters


class TestDeadSymbolsRemoved:
    """The orphaned methods/helpers must no longer exist anywhere."""

    def test_prowlarr_grab_release_removed(self):
        assert not hasattr(ProwlarrClient, "grab_release")

    def test_sonarr_lookup_series_by_tvdb_removed(self):
        assert not hasattr(SonarrClient, "lookup_series_by_tvdb")

    def test_deezer_get_trending_albums_removed(self):
        assert not hasattr(DeezerClient, "get_trending_albums")

    def test_lidarr_lookup_album_removed(self):
        assert not hasattr(LidarrClient, "lookup_album")

    def test_lidarr_search_album_removed(self):
        assert not hasattr(LidarrClient, "search_album")

    def test_formatters_format_album_info_removed(self):
        assert not hasattr(Formatters, "format_album_info")


class TestSurvivingSymbolsIntact:
    """Live siblings of the deleted code must keep working."""

    def test_lidarr_parse_album_still_works(self):
        lidarr = LidarrClient("http://lidarr:8686", "k")
        raw = {
            "id": 7,
            "foreignAlbumId": "mb-album-1",
            "title": "Master of Puppets",
            "releaseDate": "1986-03-03T00:00:00Z",
            "albumType": "Album",
            "artist": {"artistName": "Metallica", "foreignArtistId": "mb-uuid-1"},
            "statistics": {"trackCount": 8, "trackFileCount": 8},
            "duration": 3200000,
        }
        album = lidarr._parse_album(raw)
        assert album is not None
        assert album.title == "Master of Puppets"
        assert album.year == 1986

    def test_sonarr_lookup_series_still_present(self):
        # The remaining title-based lookup is untouched.
        assert hasattr(SonarrClient, "lookup_series")
        assert hasattr(SonarrClient, "get_series_by_tvdb")

    def test_deezer_get_trending_artists_still_present(self):
        assert hasattr(DeezerClient, "get_trending_artists")

    def test_lidarr_lookup_artist_still_present(self):
        assert hasattr(LidarrClient, "lookup_artist")
        assert hasattr(LidarrClient, "search_artist")
