"""Tests for Lidarr and Deezer clients + music-aware SearchService/AddService behaviour."""

from unittest.mock import AsyncMock, patch

import pytest

from bot.clients.deezer import DeezerClient
from bot.clients.lidarr import LidarrClient
from bot.models import ArtistInfo, ContentType, MovieInfo, SeriesInfo


class TestLidarrClient:
    """Tests for Lidarr API client."""

    @pytest.fixture
    def lidarr(self):
        return LidarrClient("http://lidarr:8686", "test_key")

    def test_parse_artist(self, lidarr):
        raw = {
            "id": 42,
            "foreignArtistId": "mb-uuid-1",
            "artistName": "Metallica",
            "sortName": "metallica",
            "disambiguation": "US band",
            "artistType": "Group",
            "status": "active",
            "genres": ["Metal", "Thrash"],
            "images": [
                {"coverType": "poster", "remoteUrl": "http://img/poster.jpg"},
                {"coverType": "fanart", "remoteUrl": "http://img/fanart.jpg"},
            ],
            "ratings": {"value": 8.9},
            "statistics": {"albumCount": 10, "trackCount": 120},
            "qualityProfileId": 1,
            "metadataProfileId": 2,
            "rootFolderPath": "G:\\Music\\Library",
        }
        artist = lidarr._parse_artist(raw)
        assert artist is not None
        assert artist.mb_id == "mb-uuid-1"
        assert artist.name == "Metallica"
        assert artist.album_count == 10
        assert artist.track_count == 120
        assert artist.lidarr_id == 42
        assert artist.root_folder_path == "G:\\Music\\Library"
        assert artist.poster_url == "http://img/poster.jpg"
        assert artist.ratings == {"default": 8.9}

    def test_parse_artist_skips_invalid(self, lidarr):
        # No mb_id → must return None
        assert lidarr._parse_artist({"artistName": "X"}) is None
        # No name → must return None
        assert lidarr._parse_artist({"foreignArtistId": "mb-1"}) is None

    async def test_lookup_artist_http(self, lidarr):
        with patch.object(lidarr, "get", new=AsyncMock(return_value=[
            {"foreignArtistId": "mb-1", "artistName": "Artist A"},
            {"foreignArtistId": "mb-2", "artistName": "Artist B"},
        ])):
            artists = await lidarr.lookup_artist("test")
            assert len(artists) == 2
            assert artists[0].name == "Artist A"

    async def test_lookup_artist_empty(self, lidarr):
        with patch.object(lidarr, "get", new=AsyncMock(return_value=None)):
            artists = await lidarr.lookup_artist("nothing")
            assert artists == []

    async def test_add_artist_payload(self, lidarr):
        captured: dict = {}

        async def fake_post(endpoint, json_data=None, **kwargs):
            captured["endpoint"] = endpoint
            captured["payload"] = json_data
            return {"id": 100, "foreignArtistId": "mb-x", "artistName": "Artist X"}

        artist = ArtistInfo(mb_id="mb-x", name="Artist X")

        with patch.object(lidarr, "post", new=AsyncMock(side_effect=fake_post)):
            added = await lidarr.add_artist(
                artist=artist,
                quality_profile_id=3,
                metadata_profile_id=1,
                root_folder_path="/music",
                monitor="all",
                search_for_missing=True,
            )

        assert added.lidarr_id == 100
        assert captured["endpoint"] == "/api/v1/artist"
        payload = captured["payload"]
        assert payload["foreignArtistId"] == "mb-x"
        assert payload["artistName"] == "Artist X"
        assert payload["qualityProfileId"] == 3
        assert payload["metadataProfileId"] == 1
        assert payload["rootFolderPath"] == "/music"
        assert payload["addOptions"]["monitor"] == "all"
        assert payload["addOptions"]["searchForMissingAlbums"] is True

    async def test_check_connection_v1_endpoint(self, lidarr):
        """LOGIC-10c: LidarrClient no longer overrides check_connection — the
        inherited BaseAPIClient default already hits "/api/v1/system/status",
        the same endpoint the (now removed) override used verbatim."""
        with patch.object(lidarr, "get", new=AsyncMock(return_value={"version": "1.0.0.0"})) as g:
            ok, ver, elapsed = await lidarr.check_connection()
        assert ok is True
        assert ver == "1.0.0.0"
        # Must hit v1, not v3
        g.assert_called_with("/api/v1/system/status")

    async def test_push_release_unwraps_list_response(self, lidarr):
        """BUG-01: POST /release/push returns List<ReleaseResource> — the
        client must unwrap it to the first dict, not collapse it to {}."""
        with patch.object(
            lidarr,
            "_post_no_retry",
            new=AsyncMock(return_value=[{"approved": True, "rejections": []}]),
        ):
            result = await lidarr.push_release(
                title="Test.Release.FLAC",
                download_url="http://example.com/file.torrent",
            )
        assert result == {"approved": True, "rejections": []}

    async def test_push_release_empty_list_returns_empty_dict(self, lidarr):
        """An empty list response must not raise — falls back to {}."""
        with patch.object(lidarr, "_post_no_retry", new=AsyncMock(return_value=[])):
            result = await lidarr.push_release(
                title="Test.Release.FLAC",
                download_url="http://example.com/file.torrent",
            )
        assert result == {}

    def test_parse_album_removed(self, lidarr):
        """DEAD-09: _parse_album had no production callers — removed."""
        assert not hasattr(lidarr, "_parse_album")

    def test_grab_release_removed(self, lidarr):
        """BUG-05: no client keeps a direct grab_release — Prowlarr's
        guid/indexerId are meaningless to *arr's own /release cache."""
        assert not hasattr(lidarr, "grab_release")


class TestAlbumInfoRemoved:
    """DEAD-09: AlbumInfo (and its ContentInfo union slot) had no producer
    left after _parse_album's removal — no album-grab flow exists yet."""

    def test_album_info_not_importable(self):
        import bot.models as models_module

        assert not hasattr(models_module, "AlbumInfo")


class TestDeezerClient:
    """Tests for Deezer public API client."""

    @pytest.fixture
    def deezer(self):
        return DeezerClient()

    def test_no_api_key_header(self, deezer):
        headers = deezer._get_headers()
        assert "X-Api-Key" not in headers
        assert "Accept" in headers

    async def test_get_trending_artists(self, deezer):
        response = {
            "data": [
                {"id": 1, "name": "Drake", "nb_fan": 45_000_000, "picture_big": "http://img/1.jpg"},
                {"id": 2, "name": "Taylor Swift", "nb_fan": 50_000_000},
            ]
        }
        with patch.object(deezer, "get", new=AsyncMock(return_value=response)):
            artists = await deezer.get_trending_artists(limit=10)
        assert len(artists) == 2
        assert artists[0]["name"] == "Drake"
        assert artists[0]["fans"] == 45_000_000

    async def test_get_trending_artists_network_failure(self, deezer):
        with patch.object(deezer, "get", new=AsyncMock(side_effect=RuntimeError("net"))):
            artists = await deezer.get_trending_artists()
        assert artists == []


# Removed with the Scryer migration (2026-07-28): TestProwlarrMusicDetection
# checked Prowlarr's audio-category mapping. The bot no longer talks to
# Prowlarr — Scryer routes indexers and returns typed releases.


class TestUrlMasking:
    """SEC-04: query-string masking for download URLs in logs."""

    def test_mask_apikey(self):
        from bot.services.add_service import _mask_url

        url = "https://tracker.example/download.torrent?apikey=SUPER_SECRET&id=42"
        masked = _mask_url(url)
        assert "SUPER_SECRET" not in masked
        assert "apikey=***" in masked

    def test_mask_multiple_secrets(self):
        from bot.services.add_service import _mask_url

        url = "https://t.example/dl?passkey=XYZ&token=ABC&name=nice.mkv"
        masked = _mask_url(url)
        assert "XYZ" not in masked
        assert "ABC" not in masked
        assert "name=nice.mkv" in masked

    def test_magnet_untouched(self):
        from bot.services.add_service import _mask_url

        url = "magnet:?xt=urn:btih:abc123&tr=udp://tracker"
        masked = _mask_url(url)
        assert masked.startswith("magnet:?xt=urn:btih:")

    def test_empty_url(self):
        from bot.services.add_service import _mask_url

        assert _mask_url("") == ""

    def test_mask_link_file_r_rss_params(self):
        """SEC-03: Prowlarr's own download proxy nests the ORIGINAL tracker
        URL (itself carrying a passkey) inside `link`/`file`/`r`/`rss` query
        params — these must be masked too, not just `apikey`."""
        from bot.services.add_service import _mask_url

        url = (
            "https://prowlarr.local/2/download"
            "?apikey=PROWLARR_KEY"
            "&link=https%3A%2F%2Ftracker%2Fdl%2F1%2FTRACKERPASSKEY"
            "&file=release.torrent"
            "&r=true"
            "&rss=1"
        )
        masked = _mask_url(url, max_len=500)
        assert "PROWLARR_KEY" not in masked
        assert "TRACKERPASSKEY" not in masked
        assert "link=***" in masked
        assert "file=***" in masked
        assert "r=***" in masked
        assert "rss=***" in masked

    def test_mask_long_path_segment_passkey(self):
        """SEC-03: a passkey embedded as a path segment (common format:
        /download/<id>/<passkey>/name.torrent) must be masked even though
        it's not in the query string."""
        from bot.services.add_service import _mask_url

        url = "https://tracker.example/download/123/abcdef0123456789abcdef01/name.torrent"
        masked = _mask_url(url, max_len=200)
        assert "abcdef0123456789abcdef01" not in masked
        assert "/download/123/***/name.torrent" in masked

    def test_short_path_segments_not_masked(self):
        """Short, ordinary path segments (ids, filenames) must survive."""
        from bot.services.add_service import _mask_url

        url = "https://tracker.example/download/123/name.torrent"
        masked = _mask_url(url, max_len=200)
        assert masked == "https://tracker.example/download/123/name.torrent"


class TestDownloadUrlValidation:
    """SEC-01/SEC-11: async SSRF validation with getaddrinfo."""

    async def test_rejects_private_ip_literal(self):
        from bot.services.add_service import _validate_download_url

        assert await _validate_download_url("http://192.168.0.95/x") is False
        assert await _validate_download_url("http://127.0.0.1/x") is False
        assert await _validate_download_url("http://10.0.0.5/x") is False

    async def test_rejects_unknown_scheme(self):
        from bot.services.add_service import _validate_download_url

        assert await _validate_download_url("ftp://example.com/") is False
        assert await _validate_download_url("file:///etc/passwd") is False

    async def test_rejects_magnet_without_btih(self):
        from bot.services.add_service import _validate_download_url

        assert await _validate_download_url("magnet:?xt=urn:ed2k:abc") is False

    async def test_accepts_valid_magnet(self):
        from bot.services.add_service import _validate_download_url

        assert await _validate_download_url("magnet:?xt=urn:btih:aabbccdd") is True

    async def test_rejects_hostname_that_resolves_to_private(self):
        import socket

        from bot.services.add_service import _validate_download_url

        def fake_getaddrinfo(host, port, *args, **kwargs):
            return [(socket.AF_INET, 0, 0, "", ("10.0.0.1", 0))]

        with patch("bot.services.add_service.socket.getaddrinfo", side_effect=fake_getaddrinfo):
            assert await _validate_download_url("http://evil.example/") is False


class TestSearchServiceMusicDetection:
    """SearchService.detect_with_confidence returns MUSIC when the music
    backend finds an artist and Scryer's metadata search does not win.

    Migration 2026-07-28: the video side is one `searchMetadataMulti` call
    instead of parallel Radarr/Sonarr lookups; the music side still comes from
    Lidarr (falling back to slskd).
    """

    @staticmethod
    def _scryer(movies=None, series=None, anime=None):
        client = AsyncMock()
        client.search_metadata_multi = AsyncMock(return_value={
            ContentType.MOVIE: movies or [],
            ContentType.SERIES: series or [],
            ContentType.ANIME: anime or [],
        })
        return client

    async def test_music_detected_when_artist_matches(self):
        from bot.services.scoring import ScoringService
        from bot.services.search_service import SearchService

        lidarr = AsyncMock()
        lidarr.lookup_artist = AsyncMock(return_value=[ArtistInfo(mb_id="mb-1", name="Metallica")])

        svc = SearchService(self._scryer(), ScoringService(), lidarr=lidarr)
        ct = (await svc.detect_with_confidence("Metallica")).content_type
        assert ct == ContentType.MUSIC

    async def test_close_artist_and_screen_match_asks_user_to_choose(self):
        """A close artist/screen-title match must remain a user choice."""
        from bot.services.scoring import ScoringService
        from bot.services.search_service import SearchService

        lidarr = AsyncMock()
        lidarr.lookup_artist = AsyncMock(return_value=[ArtistInfo(mb_id="mb-1", name="The Weeknd")])

        svc = SearchService(
            self._scryer(
                movies=[MovieInfo(tmdb_id=1, title="The Weeknd - Double Fantasy", year=2025)],
                series=[SeriesInfo(tvdb_id=1, title="The Weekend")],
            ),
            ScoringService(),
            lidarr=lidarr,
        )

        assert (await svc.detect_with_confidence("The Weeknd")).content_type == ContentType.UNKNOWN

    async def test_close_cross_type_match_asks_user_to_choose(self):
        """A narrow lead must remain a user choice, regardless of type."""
        from bot.services.scoring import ScoringService
        from bot.services.search_service import SearchService

        lidarr = AsyncMock()
        lidarr.lookup_artist = AsyncMock(return_value=[ArtistInfo(mb_id="mb-1", name="Metallicaa")])

        svc = SearchService(
            self._scryer(movies=[MovieInfo(tmdb_id=1, title="Metallikaa", year=2025)]),
            ScoringService(),
            lidarr=lidarr,
        )

        assert (await svc.detect_with_confidence("Metallicaa")).content_type == ContentType.UNKNOWN

    async def test_unknown_when_no_music_backend(self):
        from bot.services.scoring import ScoringService
        from bot.services.search_service import SearchService

        svc = SearchService(self._scryer(), ScoringService(), lidarr=None, slskd=None)
        ct = (await svc.detect_with_confidence("NoSuchArtist")).content_type
        assert ct == ContentType.UNKNOWN

    async def test_slskd_is_used_when_lidarr_is_absent(self):
        """slskd alone must still classify a query as music."""
        from bot.services.scoring import ScoringService
        from bot.services.search_service import SearchService

        slskd = AsyncMock()
        slskd.lookup_artists = AsyncMock(return_value=[ArtistInfo(mb_id="slskd:Metallica", name="Metallica")])

        svc = SearchService(self._scryer(), ScoringService(), lidarr=None, slskd=slskd)
        ct = (await svc.detect_with_confidence("Metallica")).content_type
        assert ct == ContentType.MUSIC
        slskd.lookup_artists.assert_awaited_once()

    async def test_slskd_is_the_fallback_when_lidarr_fails(self):
        from bot.services.scoring import ScoringService
        from bot.services.search_service import SearchService

        lidarr = AsyncMock()
        lidarr.lookup_artist = AsyncMock(side_effect=RuntimeError("Lidarr stopped"))
        slskd = AsyncMock()
        slskd.lookup_artists = AsyncMock(return_value=[ArtistInfo(mb_id="slskd:Metallica", name="Metallica")])

        svc = SearchService(self._scryer(), ScoringService(), lidarr=lidarr, slskd=slskd)
        ct = (await svc.detect_with_confidence("Metallica")).content_type
        assert ct == ContentType.MUSIC


class TestAddServiceMusic:
    """AddService.add_artist wires through to Lidarr."""

    async def test_add_artist_no_lidarr_returns_error(self):
        from bot.services.add_service import AddService

        svc = AddService(AsyncMock(), lidarr=None)
        artist = ArtistInfo(mb_id="m-1", name="X")
        added, action = await svc.add_artist(
            artist=artist, quality_profile_id=1, metadata_profile_id=1, root_folder_path="/m",
        )
        assert added is None
        assert action.success is False
        assert "Lidarr" in action.error_message

    async def test_add_artist_existing_returns_existing(self):
        from bot.services.add_service import AddService

        lidarr = AsyncMock()
        existing = ArtistInfo(mb_id="m-1", name="X", lidarr_id=42)
        lidarr.get_artist_by_mbid = AsyncMock(return_value=existing)
        svc = AddService(AsyncMock(), lidarr=lidarr)
        added, action = await svc.add_artist(
            artist=ArtistInfo(mb_id="m-1", name="X"),
            quality_profile_id=1,
            metadata_profile_id=1,
            root_folder_path="/m",
        )
        assert added is existing
        assert action.success is True
        lidarr.add_artist.assert_not_called()
