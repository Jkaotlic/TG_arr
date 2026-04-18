"""Lidarr API client."""

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import structlog

from bot.clients.base import APIError, BaseAPIClient
from bot.models import AlbumInfo, ArtistInfo, MetadataProfile, QualityProfile, RootFolder

logger = structlog.get_logger()


class LidarrClient(BaseAPIClient):
    """Client for Lidarr API (music)."""

    def __init__(self, base_url: str, api_key: str):
        super().__init__(base_url, api_key, "Lidarr")

    async def lookup_artist(self, query: str) -> list[ArtistInfo]:
        """Search for artists by name via MusicBrainz (through Lidarr)."""
        log = logger.bind(query=query)
        log.info("Looking up artist in Lidarr")

        results = await self.get("/api/v1/artist/lookup", params={"term": query})
        if not isinstance(results, list):
            return []

        artists = []
        for item in results:
            try:
                artist = self._parse_artist(item)
                if artist:
                    artists.append(artist)
            except Exception as e:
                log.warning("Failed to parse artist", error=str(e))

        log.info("Artist lookup completed", artist_count=len(artists))
        return artists

    async def lookup_album(self, query: str) -> list[AlbumInfo]:
        """Search for albums by title via MusicBrainz (through Lidarr)."""
        log = logger.bind(query=query)
        log.info("Looking up album in Lidarr")

        results = await self.get("/api/v1/album/lookup", params={"term": query})
        if not isinstance(results, list):
            return []

        albums = []
        for item in results:
            try:
                album = self._parse_album(item)
                if album:
                    albums.append(album)
            except Exception as e:
                log.warning("Failed to parse album", error=str(e))

        log.info("Album lookup completed", album_count=len(albums))
        return albums

    async def get_artist_by_mbid(self, mb_id: str) -> Optional[ArtistInfo]:
        """Get artist from library by MusicBrainz ID."""
        results = await self.get("/api/v1/artist", params={"mbId": mb_id})
        if isinstance(results, list) and results:
            return self._parse_artist(results[0])
        return None

    async def get_all_artists(self) -> list[ArtistInfo]:
        """Get all artists currently in the Lidarr library."""
        results = await self.get("/api/v1/artist")
        if not isinstance(results, list):
            return []
        return [a for a in (self._parse_artist(item) for item in results) if a]

    async def add_artist(
        self,
        artist: ArtistInfo,
        quality_profile_id: int,
        metadata_profile_id: int,
        root_folder_path: str,
        monitored: bool = True,
        monitor: str = "all",
        search_for_missing: bool = True,
        tags: Optional[list[int]] = None,
    ) -> ArtistInfo:
        """
        Add an artist to Lidarr.

        Args:
            artist: ArtistInfo with at least mb_id and name
            quality_profile_id: Quality profile ID
            metadata_profile_id: Metadata profile ID (controls which release types)
            root_folder_path: Root folder path
            monitored: Whether the artist is monitored
            monitor: Which albums to monitor: all, future, missing, existing, first, latest, none
            search_for_missing: Kick off search for missing albums after add
            tags: Optional tag IDs

        Returns:
            ArtistInfo of the added artist
        """
        log = logger.bind(name=artist.name, mb_id=artist.mb_id)
        log.info("Adding artist to Lidarr")

        payload: dict[str, Any] = {
            "artistName": artist.name,
            "foreignArtistId": artist.mb_id,
            "qualityProfileId": quality_profile_id,
            "metadataProfileId": metadata_profile_id,
            "rootFolderPath": root_folder_path,
            "monitored": monitored,
            "monitorNewItems": "all",
            "addOptions": {
                "monitor": monitor,
                "searchForMissingAlbums": search_for_missing,
            },
        }

        if tags:
            payload["tags"] = tags

        result = await self.post("/api/v1/artist", json_data=payload)

        if isinstance(result, dict):
            added = self._parse_artist(result)
            if added:
                log.info("Artist added successfully", lidarr_id=added.lidarr_id)
                return added

        raise APIError("Не удалось добавить артиста в Lidarr")

    async def grab_release(self, guid: str, indexer_id: int) -> dict[str, Any]:
        """Grab a specific release (non-idempotent)."""
        payload = {"guid": guid, "indexerId": indexer_id}
        result = await self._post_no_retry("/api/v1/release", json_data=payload)
        return result if isinstance(result, dict) else {}

    async def push_release(
        self,
        title: str,
        download_url: str,
        protocol: str = "torrent",
        publish_date: Optional[str] = None,
    ) -> dict[str, Any]:
        """Push a release to Lidarr for processing."""
        payload: dict[str, Any] = {
            "title": title,
            "downloadUrl": download_url,
            "protocol": protocol.capitalize(),
        }
        if publish_date:
            payload["publishDate"] = publish_date

        result = await self._post_no_retry("/api/v1/release/push", json_data=payload)
        return result if isinstance(result, dict) else {}

    async def search_artist(self, artist_id: int) -> dict[str, Any]:
        """Trigger a search for all albums of an artist."""
        payload = {"name": "ArtistSearch", "artistId": artist_id}
        result = await self.post("/api/v1/command", json_data=payload)
        return result if isinstance(result, dict) else {}

    async def search_album(self, album_id: int) -> dict[str, Any]:
        """Trigger a search for a specific album."""
        payload = {"name": "AlbumSearch", "albumIds": [album_id]}
        result = await self.post("/api/v1/command", json_data=payload)
        return result if isinstance(result, dict) else {}

    async def get_calendar(self, days: int = 7) -> list[dict[str, Any]]:
        """Get upcoming album releases from Lidarr calendar."""
        now = datetime.now(timezone.utc)
        start = now.strftime("%Y-%m-%d")
        end = (now + timedelta(days=days)).strftime("%Y-%m-%d")

        params = {
            "start": start,
            "end": end,
            "includeArtist": "true",
        }
        results = await self.get("/api/v1/calendar", params=params)
        if not isinstance(results, list):
            return []

        albums: list[dict[str, Any]] = []
        for item in results:
            try:
                release_date = item.get("releaseDate", "") or item.get("airDate", "")
                artist_data = item.get("artist") or {}
                albums.append({
                    "artist_name": artist_data.get("artistName", "Unknown"),
                    "title": item.get("title", ""),
                    "album_type": item.get("albumType", ""),
                    "release_date": release_date,
                    "has_file": item.get("hasFile", False),
                    "overview": item.get("overview", "") or "",
                    "genres": item.get("genres", []) or [],
                })
            except Exception as e:
                logger.warning("Failed to parse calendar album", error=str(e))

        return albums

    async def get_quality_profiles(self) -> list[QualityProfile]:
        """Get all quality profiles."""
        results = await self.get("/api/v1/qualityprofile")
        profiles = []
        if isinstance(results, list):
            for item in results:
                try:
                    profiles.append(QualityProfile(id=item["id"], name=item["name"]))
                except (KeyError, TypeError) as e:
                    logger.warning("Skipping malformed profile", error=str(e))
        return profiles

    async def get_metadata_profiles(self) -> list[MetadataProfile]:
        """Get all metadata profiles (Lidarr-specific)."""
        results = await self.get("/api/v1/metadataprofile")
        profiles = []
        if isinstance(results, list):
            for item in results:
                try:
                    profiles.append(MetadataProfile(id=item["id"], name=item["name"]))
                except (KeyError, TypeError) as e:
                    logger.warning("Skipping malformed metadata profile", error=str(e))
        return profiles

    async def get_root_folders(self) -> list[RootFolder]:
        """Get all root folders."""
        results = await self.get("/api/v1/rootfolder")
        folders = []
        if isinstance(results, list):
            for item in results:
                try:
                    folders.append(RootFolder(
                        id=item["id"],
                        path=item["path"],
                        free_space=item.get("freeSpace"),
                    ))
                except (KeyError, TypeError) as e:
                    logger.warning("Skipping malformed root folder", error=str(e))
        return folders

    def _parse_artist(self, item: dict[str, Any]) -> Optional[ArtistInfo]:
        """Parse Lidarr artist response to ArtistInfo."""
        mb_id = item.get("foreignArtistId") or item.get("mbId") or ""
        if not mb_id:
            return None

        name = item.get("artistName") or item.get("sortName") or ""
        if not name:
            return None

        poster_url = None
        fanart_url = None
        for image in item.get("images", []) or []:
            cover_type = (image.get("coverType") or "").lower()
            url = image.get("remoteUrl") or image.get("url")
            if cover_type == "poster" and not poster_url:
                poster_url = url
            elif cover_type == "fanart" and not fanart_url:
                fanart_url = url

        ratings: dict[str, Any] = {}
        rating_data = item.get("ratings")
        if isinstance(rating_data, dict) and "value" in rating_data:
            ratings["default"] = rating_data["value"]

        stats = item.get("statistics") or {}
        album_count = int(stats.get("albumCount", 0) or 0)
        track_count = int(stats.get("trackCount", 0) or 0)

        return ArtistInfo(
            mb_id=mb_id,
            name=name,
            sort_name=item.get("sortName"),
            overview=item.get("overview"),
            disambiguation=item.get("disambiguation"),
            artist_type=item.get("artistType"),
            status=item.get("status"),
            genres=item.get("genres", []) or [],
            poster_url=poster_url,
            fanart_url=fanart_url,
            ratings=ratings,
            album_count=album_count,
            track_count=track_count,
            lidarr_id=item.get("id"),
            quality_profile_id=item.get("qualityProfileId"),
            metadata_profile_id=item.get("metadataProfileId"),
            root_folder_path=item.get("rootFolderPath") or item.get("path"),
        )

    def _parse_album(self, item: dict[str, Any]) -> Optional[AlbumInfo]:
        """Parse Lidarr album response to AlbumInfo."""
        mb_id = item.get("foreignAlbumId") or item.get("mbId") or ""
        if not mb_id:
            return None

        title = item.get("title") or ""
        if not title:
            return None

        poster_url = None
        for image in item.get("images", []) or []:
            cover_type = (image.get("coverType") or "").lower()
            url = image.get("remoteUrl") or image.get("url")
            if cover_type in ("cover", "poster") and not poster_url:
                poster_url = url

        release_date = None
        year = None
        rd = item.get("releaseDate")
        if isinstance(rd, str) and rd:
            try:
                release_date = datetime.fromisoformat(rd.replace("Z", "+00:00"))
                year = release_date.year
            except ValueError:
                pass

        artist_data = item.get("artist") or {}
        artist_name = artist_data.get("artistName") or item.get("artistName")
        artist_mb_id = artist_data.get("foreignArtistId") or item.get("foreignArtistId")

        ratings: dict[str, Any] = {}
        rating_data = item.get("ratings")
        if isinstance(rating_data, dict) and "value" in rating_data:
            ratings["default"] = rating_data["value"]

        stats = item.get("statistics") or {}
        track_count = int(stats.get("trackCount", 0) or 0)
        duration_ms = int(item.get("duration", 0) or 0)

        return AlbumInfo(
            mb_id=mb_id,
            artist_mb_id=artist_mb_id,
            title=title,
            artist_name=artist_name,
            disambiguation=item.get("disambiguation"),
            album_type=item.get("albumType"),
            release_date=release_date,
            year=year,
            overview=item.get("overview"),
            genres=item.get("genres", []) or [],
            poster_url=poster_url,
            ratings=ratings,
            track_count=track_count,
            duration_ms=duration_ms,
            lidarr_id=item.get("id"),
            has_file=bool(stats.get("trackFileCount", 0) > 0) if stats else False,
        )

    async def check_connection(self) -> tuple[bool, str | None, float | None]:
        """Check if Lidarr is available."""
        start_time = time.monotonic()
        try:
            result = await self.get("/api/v1/system/status")
            elapsed = (time.monotonic() - start_time) * 1000
            version = result.get("version") if isinstance(result, dict) else None
            return True, version, round(elapsed, 2)
        except Exception as e:
            elapsed = (time.monotonic() - start_time) * 1000
            logger.warning("Lidarr health check failed", error=str(e))
            return False, None, round(elapsed, 2)
