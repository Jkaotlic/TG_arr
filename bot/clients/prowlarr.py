"""Prowlarr API client."""

import re
from datetime import datetime
from typing import Any, Optional

import structlog

from bot.clients.base import BaseAPIClient
from bot.models import ContentType, QualityInfo, SearchResult

logger = structlog.get_logger()

# Category mappings for Prowlarr
MOVIE_CATEGORIES = [2000, 2010, 2020, 2030, 2040, 2045, 2050, 2060, 2070, 2080]
TV_CATEGORIES = [5000, 5010, 5020, 5030, 5040, 5045, 5050, 5060, 5070, 5080]


class ProwlarrClient(BaseAPIClient):
    """Client for Prowlarr API."""

    def __init__(self, base_url: str, api_key: str):
        super().__init__(base_url, api_key, "Prowlarr")

    async def search(
        self,
        query: str,
        content_type: ContentType = ContentType.UNKNOWN,
        categories: Optional[list[int]] = None,
        limit: int = 100,
    ) -> list[SearchResult]:
        """
        Search for releases across all indexers.

        Args:
            query: Search query
            content_type: Filter by movie or series
            categories: Specific category IDs to search
            limit: Maximum number of results

        Returns:
            List of normalized SearchResult objects
        """
        params: dict[str, Any] = {
            "query": query,
            "limit": limit,
            "type": "search",
        }

        # Add category filter based on content type
        if categories:
            params["categories"] = categories
        elif content_type == ContentType.MOVIE:
            params["categories"] = MOVIE_CATEGORIES
        elif content_type == ContentType.SERIES:
            params["categories"] = TV_CATEGORIES

        log = logger.bind(query=query, content_type=content_type.value)
        log.info("Searching Prowlarr")

        try:
            results = await self.get("/api/v1/search", params=params, timeout=60.0)

            if not isinstance(results, list):
                log.warning("Unexpected response type", response_type=type(results).__name__)
                return []

            normalized = []
            for item in results:
                try:
                    result = self._normalize_result(item)
                    if result:
                        normalized.append(result)
                except Exception as e:
                    log.warning("Failed to normalize result", error=str(e), item=item.get("title", "unknown"))

            log.info("Search completed", result_count=len(normalized))
            return normalized

        except Exception as e:
            log.error("Search failed", error=str(e))
            raise

    def _normalize_result(self, item: dict[str, Any]) -> Optional[SearchResult]:
        """Normalize Prowlarr search result to our model."""
        # Handle different field naming conventions in Prowlarr responses
        guid = item.get("guid") or item.get("downloadUrl") or item.get("infoUrl") or ""
        if not guid:
            return None

        title = item.get("title") or item.get("fileName") or ""
        if not title:
            return None

        # Parse size - can be in different fields
        size = 0
        if "size" in item:
            size = int(item["size"]) if item["size"] else 0
        elif "files" in item and isinstance(item["files"], list) and item["files"]:
            size = sum(f.get("size", 0) for f in item["files"])

        # Parse seeders/leechers
        seeders = None
        leechers = None
        if "seeders" in item:
            seeders = int(item["seeders"]) if item["seeders"] is not None else None
        if "leechers" in item or "peers" in item:
            leechers = int(item.get("leechers") or item.get("peers") or 0) if item.get("leechers") or item.get("peers") else None

        # Parse indexer info
        indexer = "Unknown"
        indexer_id = 0
        if "indexer" in item:
            indexer = item["indexer"]
        elif "indexerName" in item:
            indexer = item["indexerName"]
        if "indexerId" in item:
            indexer_id = int(item["indexerId"])

        # Parse protocol
        protocol = "torrent"
        if item.get("protocol"):
            protocol = str(item["protocol"]).lower()
        elif item.get("downloadUrl", "").endswith(".nzb"):
            protocol = "usenet"

        # Parse categories
        categories = []
        category_names = []
        if "categories" in item and isinstance(item["categories"], list):
            for cat in item["categories"]:
                if isinstance(cat, dict):
                    if "id" in cat:
                        categories.append(int(cat["id"]))
                    if "name" in cat:
                        category_names.append(cat["name"])
                elif isinstance(cat, int):
                    categories.append(cat)

        # Parse publish date
        publish_date = None
        date_str = item.get("publishDate") or item.get("pubDate")
        if date_str:
            try:
                # Handle ISO format with or without timezone
                if isinstance(date_str, str):
                    date_str = date_str.replace("Z", "+00:00")
                    publish_date = datetime.fromisoformat(date_str)
            except (ValueError, TypeError):
                pass

        # Parse quality info from title
        quality = self._parse_quality(title)

        # Detect content type from categories
        detected_type = ContentType.UNKNOWN
        if any(cat in MOVIE_CATEGORIES for cat in categories):
            detected_type = ContentType.MOVIE
        elif any(cat in TV_CATEGORIES for cat in categories):
            detected_type = ContentType.SERIES

        # Parse year, season, episode from title
        detected_year = self._extract_year(title)
        detected_season, detected_episode = self._extract_season_episode(title)
        is_season_pack = self._is_season_pack(title)

        # If has season info, likely a series
        if detected_season is not None and detected_type == ContentType.UNKNOWN:
            detected_type = ContentType.SERIES

        return SearchResult(
            guid=guid,
            indexer=indexer,
            indexer_id=indexer_id,
            title=title,
            size=size,
            seeders=seeders,
            leechers=leechers,
            protocol=protocol,
            download_url=item.get("downloadUrl"),
            info_url=item.get("infoUrl"),
            magnet_url=item.get("magnetUrl"),
            publish_date=publish_date,
            categories=categories,
            category_names=category_names,
            quality=quality,
            prowlarr_score=item.get("score"),
            detected_type=detected_type,
            detected_year=detected_year,
            detected_season=detected_season,
            detected_episode=detected_episode,
            is_season_pack=is_season_pack,
        )

    def _parse_quality(self, title: str) -> QualityInfo:
        """Parse quality information from release title."""
        title_lower = title.lower()

        # Resolution
        resolution = None
        if "2160p" in title_lower or "4k" in title_lower or "uhd" in title_lower:
            resolution = "2160p"
        elif "1080p" in title_lower:
            resolution = "1080p"
        elif "720p" in title_lower:
            resolution = "720p"
        elif "480p" in title_lower:
            resolution = "480p"
        elif "576p" in title_lower:
            resolution = "576p"

        # Source
        source = None
        if "bluray" in title_lower or "blu-ray" in title_lower or "bdrip" in title_lower:
            source = "BluRay"
        elif "web-dl" in title_lower or "webdl" in title_lower:
            source = "WEB-DL"
        elif "webrip" in title_lower or "web-rip" in title_lower:
            source = "WEBRip"
        elif "hdtv" in title_lower:
            source = "HDTV"
        elif "dvdrip" in title_lower or "dvd-rip" in title_lower:
            source = "DVDRip"
        elif any(x in title_lower for x in ["cam", "camrip", "hdcam"]):
            source = "CAM"
        elif any(x in title_lower for x in ["telesync", "ts", "hdts"]):
            source = "TS"
        elif "telecine" in title_lower or "tc" in title_lower:
            source = "TC"

        # Codec
        codec = None
        if "x265" in title_lower or "hevc" in title_lower or "h.265" in title_lower or "h265" in title_lower:
            codec = "x265"
        elif "x264" in title_lower or "h.264" in title_lower or "h264" in title_lower:
            codec = "x264"
        elif "av1" in title_lower:
            codec = "AV1"
        elif "xvid" in title_lower:
            codec = "XviD"
        elif "divx" in title_lower:
            codec = "DivX"

        # HDR
        hdr = None
        if "dolby vision" in title_lower or "dolby.vision" in title_lower or re.search(r"[\.\s\-]dv[\.\s\-]|[\.\s\-]dv$", title_lower) or "dovi" in title_lower:
            hdr = "DV"
        if "hdr10+" in title_lower:
            hdr = "HDR10+" if hdr is None else f"{hdr}+HDR10+"
        elif "hdr10" in title_lower:
            hdr = "HDR10" if hdr is None else f"{hdr}+HDR10"
        elif "hdr" in title_lower:
            hdr = "HDR" if hdr is None else f"{hdr}+HDR"

        # Audio
        audio = None
        if "atmos" in title_lower:
            audio = "Atmos"
        elif "truehd" in title_lower or "true-hd" in title_lower:
            audio = "TrueHD"
        elif "dts-hd" in title_lower or "dtshd" in title_lower:
            audio = "DTS-HD"
        elif "dts" in title_lower:
            audio = "DTS"
        elif "dd5.1" in title_lower or "dd 5.1" in title_lower or "dd.5.1" in title_lower or "ac3" in title_lower:
            audio = "DD5.1"
        elif "aac" in title_lower:
            audio = "AAC"

        # Subtitles / Russian audio
        subtitle = None
        if re.search(r"rus[\.\s\-_]?sub|russian[\.\s\-_]?sub|russub", title_lower):
            subtitle = "RusSub"
        elif re.search(r"[\.\s\-_]mvo[\.\s\-_$]", title_lower):
            subtitle = "MVO"
        elif re.search(r"[\.\s\-_]dvo[\.\s\-_$]", title_lower):
            subtitle = "DVO"
        elif re.search(r"[\.\s\-_]avo[\.\s\-_$]", title_lower):
            subtitle = "AVO"
        elif re.search(r"multi[\.\s\-_]?sub", title_lower):
            subtitle = "MultiSub"

        # Remux
        is_remux = "remux" in title_lower

        # Repack/Proper
        is_repack = "repack" in title_lower or "rerip" in title_lower
        is_proper = "proper" in title_lower

        return QualityInfo(
            resolution=resolution,
            source=source,
            codec=codec,
            hdr=hdr,
            audio=audio,
            subtitle=subtitle,
            is_remux=is_remux,
            is_repack=is_repack,
            is_proper=is_proper,
        )

    def _extract_year(self, title: str) -> Optional[int]:
        """Extract year from title."""
        # Match year in various formats: (2021), [2021], .2021., 2021
        patterns = [
            r"[\(\[](\d{4})[\)\]]",  # (2021) or [2021]
            r"\.(\d{4})\.",  # .2021.
            r"\s(\d{4})\s",  # space 2021 space
            r"[\.\s](\d{4})$",  # ends with .2021 or space 2021
        ]
        for pattern in patterns:
            match = re.search(pattern, title)
            if match:
                year = int(match.group(1))
                if 1900 <= year <= 2100:
                    return year
        return None

    def _extract_season_episode(self, title: str) -> tuple[Optional[int], Optional[int]]:
        """Extract season and episode numbers from title."""
        title_lower = title.lower()

        # S01E01 format
        match = re.search(r"s(\d{1,2})e(\d{1,3})", title_lower)
        if match:
            return int(match.group(1)), int(match.group(2))

        # S01 format (season only)
        match = re.search(r"s(\d{1,2})(?![e\d])", title_lower)
        if match:
            return int(match.group(1)), None

        # Season 1 Episode 2 format (supports dot/space separators)
        match = re.search(r"season[\s.]*(\d{1,2})(?:[\s.]*episode[\s.]*(\d{1,3}))?", title_lower)
        if match:
            season = int(match.group(1))
            episode = int(match.group(2)) if match.group(2) else None
            return season, episode

        # 1x01 format
        match = re.search(r"(\d{1,2})x(\d{1,3})", title_lower)
        if match:
            return int(match.group(1)), int(match.group(2))

        return None, None

    def _is_season_pack(self, title: str) -> bool:
        """Check if release is a season pack."""
        title_lower = title.lower()

        # Explicit season pack markers (check first — most reliable)
        if any(x in title_lower for x in ["complete season", "season pack", "full season"]):
            return True

        # S01 format — season pack only if no episode follows
        match = re.search(r"s(\d{1,2})(?!e\d)", title_lower)
        if match:
            # Also verify there's no episode range like S01E01-E10
            if not re.search(r"s\d{1,2}e\d", title_lower):
                return True

        # "Season X" without episode (supports dot/space separators)
        if re.search(r"season[\s.]*\d{1,2}(?![\s.]*episode)", title_lower):
            return True

        return False

    async def get_indexers(self) -> list[dict[str, Any]]:
        """Get list of configured indexers."""
        result = await self.get("/api/v1/indexer")
        return result if isinstance(result, list) else []

    async def grab_release(self, guid: str, indexer_id: int) -> dict[str, Any]:
        """
        Grab a release directly through Prowlarr.
        This sends the release to the download client.
        """
        payload = {
            "guid": guid,
            "indexerId": indexer_id,
        }
        result = await self.post("/api/v1/search", json_data=payload)
        return result if isinstance(result, dict) else {}
