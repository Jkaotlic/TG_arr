"""Tests for Lidarr and Deezer clients + music-aware SearchService/AddService behaviour."""

from unittest.mock import AsyncMock, patch

import pytest

from bot.clients.deezer import DeezerClient
from bot.clients.lidarr import LidarrClient
from bot.clients.prowlarr import MUSIC_CATEGORIES, ProwlarrClient
from bot.models import ArtistInfo, ContentType, SearchResult


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

    def test_parse_album(self, lidarr):
        raw = {
            "id": 7,
            "foreignAlbumId": "mb-album-1",
            "title": "Master of Puppets",
            "releaseDate": "1986-03-03T00:00:00Z",
            "albumType": "Album",
            "genres": ["Thrash Metal"],
            "artist": {"artistName": "Metallica", "foreignArtistId": "mb-uuid-1"},
            "statistics": {"trackCount": 8, "trackFileCount": 8},
            "duration": 3200000,
        }
        album = lidarr._parse_album(raw)
        assert album is not None
        assert album.title == "Master of Puppets"
        assert album.artist_name == "Metallica"
        assert album.year == 1986
        assert album.track_count == 8
        assert album.has_file is True
        assert album.duration_ms == 3200000

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
        with patch.object(lidarr, "get", new=AsyncMock(return_value={"version": "1.0.0.0"})) as g:
            ok, ver, elapsed = await lidarr.check_connection()
        assert ok is True
        assert ver == "1.0.0.0"
        # Must hit v1, not v3
        g.assert_called_with("/api/v1/system/status")


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


class TestProwlarrMusicDetection:
    """Prowlarr must detect music content type from audio categories."""

    def test_music_categories_detected(self):
        client = ProwlarrClient("http://prowlarr", "key")
        raw = {
            "guid": "x",
            "title": "Artist - Album [2024] [FLAC]",
            "categories": [{"id": 3040, "name": "Audio/Lossless"}],
            "downloadUrl": "http://example.com/file",
        }
        result = client._normalize_result(raw)
        assert result is not None
        assert result.detected_type == ContentType.MUSIC

    def test_audio_root_category(self):
        client = ProwlarrClient("http://prowlarr", "key")
        raw = {
            "guid": "y",
            "title": "Artist - Single [MP3]",
            "categories": [{"id": 3000}, {"id": 3010}],
        }
        result = client._normalize_result(raw)
        assert result is not None
        assert result.detected_type == ContentType.MUSIC

    def test_music_categories_list_exhaustive(self):
        # Sanity check that our hard-coded list covers the common audio buckets.
        for cat in (3000, 3010, 3020, 3030, 3040, 3050, 3060):
            assert cat in MUSIC_CATEGORIES


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

        url = "https://t.example/file?passkey=XYZ&token=ABC&file=nice.mkv"
        masked = _mask_url(url)
        assert "XYZ" not in masked
        assert "ABC" not in masked
        assert "file=nice.mkv" in masked

    def test_magnet_untouched(self):
        from bot.services.add_service import _mask_url

        url = "magnet:?xt=urn:btih:abc123&tr=udp://tracker"
        masked = _mask_url(url)
        assert masked.startswith("magnet:?xt=urn:btih:")

    def test_empty_url(self):
        from bot.services.add_service import _mask_url

        assert _mask_url("") == ""


class TestDownloadUrlValidation:
    """SEC-01/SEC-11: async SSRF validation with getaddrinfo."""

    async def test_rejects_private_ip_literal(self):
        from bot.services.add_service import _validate_download_url

        assert await _validate_download_url("http://192.168.31.95/x") is False
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
    """SearchService.detect_content_type returns MUSIC when Lidarr finds an artist."""

    async def test_music_detected_when_artist_matches(self):
        from bot.services.scoring import ScoringService
        from bot.services.search_service import SearchService

        prowlarr = AsyncMock()
        radarr = AsyncMock()
        radarr.lookup_movie = AsyncMock(return_value=[])
        sonarr = AsyncMock()
        sonarr.lookup_series = AsyncMock(return_value=[])
        lidarr = AsyncMock()
        lidarr.lookup_artist = AsyncMock(return_value=[
            ArtistInfo(mb_id="mb-1", name="Metallica"),
        ])

        svc = SearchService(prowlarr, radarr, sonarr, ScoringService(), lidarr=lidarr)
        ct = await svc.detect_content_type("Metallica")
        assert ct == ContentType.MUSIC

    async def test_unknown_when_no_lidarr(self):
        from bot.services.scoring import ScoringService
        from bot.services.search_service import SearchService

        prowlarr = AsyncMock()
        radarr = AsyncMock()
        radarr.lookup_movie = AsyncMock(return_value=[])
        sonarr = AsyncMock()
        sonarr.lookup_series = AsyncMock(return_value=[])

        svc = SearchService(prowlarr, radarr, sonarr, ScoringService(), lidarr=None)
        ct = await svc.detect_content_type("NoSuchArtist")
        assert ct == ContentType.UNKNOWN


class TestAddServiceMusic:
    """AddService.add_artist and grab_music_release wire through to Lidarr."""

    async def test_add_artist_no_lidarr_returns_error(self):
        from bot.services.add_service import AddService

        svc = AddService(AsyncMock(), AsyncMock(), AsyncMock(), lidarr=None)
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
        svc = AddService(AsyncMock(), AsyncMock(), AsyncMock(), lidarr=lidarr)
        added, action = await svc.add_artist(
            artist=ArtistInfo(mb_id="m-1", name="X"),
            quality_profile_id=1,
            metadata_profile_id=1,
            root_folder_path="/m",
        )
        assert added is existing
        assert action.success is True
        lidarr.add_artist.assert_not_called()

    async def test_grab_music_release_no_lidarr(self):
        from bot.services.add_service import AddService

        svc = AddService(AsyncMock(), AsyncMock(), AsyncMock(), lidarr=None)
        release = SearchResult(guid="g", title="t")
        ok, action, msg = await svc.grab_music_release(
            artist=ArtistInfo(mb_id="m-1", name="X"),
            release=release,
            quality_profile_id=1,
            metadata_profile_id=1,
            root_folder_path="/m",
        )
        assert ok is False
        assert "Lidarr" in msg
