"""Prowlarr API client."""

import asyncio
import re
from datetime import datetime
from typing import Any, Optional

import httpx
import structlog

from bot.clients.base import APIError, BaseAPIClient, ServiceConnectionError
from bot.config import get_settings
from bot.models import MIN_PACK_EPISODES, ContentType, QualityInfo, SearchResult
from bot.services.release_parser import parse_episode_numbers, parse_title_quality

logger = structlog.get_logger()

# Category mappings for Prowlarr
MOVIE_CATEGORIES = [2000, 2010, 2020, 2030, 2040, 2045, 2050, 2060, 2070, 2080]
TV_CATEGORIES = [5000, 5010, 5020, 5030, 5040, 5045, 5050, 5060, 5070, 5080]
# Audio: 3000 root, 3010 MP3, 3020 Video, 3030 Audiobook, 3040 Lossless, 3050 Other, 3060 Foreign
MUSIC_CATEGORIES = [3000, 3010, 3020, 3030, 3040, 3050, 3060]

# Диапазон серий: «S02E01-08 of 08», «S2E1-8», «S01E01-E24 из 24». Находка
# живого прогона 2026-08-12 — на русских трекерах это массовая форма записи
# сезонного пака, но одиночный паттерн `s\d+e\d` в ней тоже находится, поэтому
# и сезон, и признак пака определялись неверно.
# Замыкающий lookahead отсекает то, что диапазоном лишь выглядит: «S01E05-720p»
# (разрешение), «S01E05-4HD» (релиз-группа с цифры) — за числом диапазона может
# стоять только разделитель, не буква и не ещё одна цифра.
_EPISODE_RANGE_RE = re.compile(r"s(\d{1,2})\s*e(\d{1,3})\s*-\s*e?(\d{1,3})(?![0-9a-zа-яё])")


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
        elif content_type in (ContentType.SERIES, ContentType.ANIME):
            params["categories"] = TV_CATEGORIES
        elif content_type == ContentType.MUSIC:
            params["categories"] = MUSIC_CATEGORIES

        log = logger.bind(query=query, content_type=content_type.value)
        log.info("Searching Prowlarr")

        settings = get_settings()
        timeout = settings.prowlarr_search_timeout
        max_attempts = 1 + max(0, settings.prowlarr_search_retries)

        # Один из индексеров (RuTracker через Cloudflare) периодически залипает на 30+с
        # и приводит к таймауту, при этом тот же запрос секундами позже отрабатывает
        # быстро (Prowlarr внутри помечает индексер как failing). Один retry превращает
        # transient-fail в успех без участия пользователя. Pause короткий — чтобы
        # суммарное ожидание не выходило за разумные рамки.
        last_timeout: Optional[httpx.TimeoutException] = None
        for attempt in range(1, max_attempts + 1):
            try:
                return await self._do_search(params, timeout, log, attempt=attempt)
            except httpx.TimeoutException as e:
                last_timeout = e
                log.warning(
                    "Prowlarr search timeout",
                    timeout_s=timeout,
                    attempt=attempt,
                    max_attempts=max_attempts,
                )
                if attempt < max_attempts:
                    await asyncio.sleep(2.0)
                    continue
                break
            except httpx.ConnectError as e:
                log.warning("Prowlarr connect error")
                raise ServiceConnectionError("Не удалось подключиться к Prowlarr") from e
            except APIError:
                raise
            except Exception as e:
                log.error("Search failed", error=str(e))
                raise

        raise ServiceConnectionError(
            f"Таймаут Prowlarr ({timeout:.0f}s, попыток: {max_attempts})"
        ) from last_timeout

    async def _do_search(
        self,
        params: dict[str, Any],
        timeout: float,
        log: Any,
        attempt: int,
    ) -> list[SearchResult]:
        """One Prowlarr search attempt. Raises httpx.TimeoutException on timeout."""
        client = await self._get_client()
        response = await client.request(
            method="GET",
            url="/api/v1/search",
            params=params,
            timeout=timeout,
        )
        if response.status_code >= 400:
            raise APIError(
                f"Ошибка Prowlarr: {response.status_code}",
                status_code=response.status_code,
            )
        results = response.json()

        if not isinstance(results, list):
            log.warning("Unexpected response type", response_type=type(results).__name__)
            return []

        normalized = []
        dropped_no_guid = 0
        dropped_no_title = 0
        for item in results:
            try:
                if not (item.get("guid") or item.get("downloadUrl") or item.get("infoUrl")):
                    dropped_no_guid += 1
                    continue
                if not (item.get("title") or item.get("fileName")):
                    dropped_no_title += 1
                    continue
                result = self._normalize_result(item)
                if result:
                    normalized.append(result)
            except Exception as e:
                log.warning("Failed to normalize result", error=str(e), item=item.get("title", "unknown"))

        # OBS-21: surface dropped items so "few results" complaints can be debugged.
        # OBS-07: snake_case event name, distinct from search_service.py's
        # "search_completed" (Prowlarr-level raw fetch vs. the higher-level
        # detect+score+dedupe pipeline) — prefixed so grep/jq can tell them apart.
        log.info(
            "prowlarr_search_completed",
            result_count=len(normalized),
            raw_count=len(results),
            dropped_no_guid=dropped_no_guid,
            dropped_no_title=dropped_no_title,
            attempt=attempt,
        )
        return normalized

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
            # BUG-02: 0 leechers is a valid value — use explicit None checks so a
            # fully-seeded torrent shows "Личи: 0" instead of hiding the field.
            raw_leechers = item.get("leechers")
            if raw_leechers is None:
                raw_leechers = item.get("peers")
            leechers = int(raw_leechers) if raw_leechers is not None else None

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
        elif any(cat in MUSIC_CATEGORIES for cat in categories):
            detected_type = ContentType.MUSIC

        # Parse year, season, episode from title
        detected_year = self._extract_year(title)
        detected_season, detected_episode = self._extract_season_episode(title)
        is_season_pack = self._is_season_pack(title)

        # If has season info, likely a series
        if detected_season is not None and detected_type == ContentType.UNKNOWN:
            detected_type = ContentType.SERIES

        return SearchResult(
            guid=guid,
            # Rollback 2026-08-10 (Task 10, fix round 1): this indexer_id is
            # PROWLARR's own numbering, not *arr's — it must never be handed
            # to *arr's native grab endpoint. `origin="prowlarr"` (renamed
            # from `source` in fix round 2 — collided with QualityInfo.source)
            # is what actually blocks that in AddService.grab_release;
            # indexer_id alone is not a safe signal (see SearchResult's
            # docstring). Kept explicit even though it's now also the default
            # — self-documenting, and defensive against the default changing.
            origin="prowlarr",
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
            detected_episodes=parse_episode_numbers(title),
            is_season_pack=is_season_pack,
        )

    def _parse_quality(self, title: str) -> QualityInfo:
        """Parse quality information from release title.

        Audit 2026-09-23 GRAB-10/17: the parser now lives in
        `bot.services.release_parser.parse_title_quality`, shared with the
        *arr path (which fills codec/HDR/voice-over from the title too).
        """
        return parse_title_quality(title)

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

    def _episode_range(self, title: str) -> Optional[tuple[int, int, int]]:
        """(сезон, первая серия, последняя) для «S02E01-08», иначе None."""
        match = _EPISODE_RANGE_RE.search(title.lower())
        if not match:
            return None
        season = int(match.group(1))
        first = int(match.group(2))
        last = int(match.group(3))
        if last - first + 1 < MIN_PACK_EPISODES:
            return None
        return season, first, last

    def _extract_season_episode(self, title: str) -> tuple[Optional[int], Optional[int]]:
        """Extract season and episode numbers from title."""
        title_lower = title.lower()

        # Диапазон серий («S02E01-08 of 08») — это пак: сезон у него есть,
        # а «серия» не определена. Проверяется ДО одиночного s\d+e\d, который
        # иначе выдал бы первую серию диапазона за единственную — и карточка
        # релиза печатала бы «Сезон 2 Серия 1 (сезон целиком)».
        episode_range = self._episode_range(title_lower)
        if episode_range is not None:
            return episode_range[0], None

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

        # Кириллица. Проверяется ПОСЛЕ латинских форм намеренно: русскоязычная
        # раздача обычно несёт и оригинальное название, а "S03E05" в нём
        # разбирается точнее, чем словесная форма. Кириллические «с»/«е»/«х» —
        # другие кодпоинты, чем латинские s/e/x, поэтому паттерны выше их не
        # видят вовсе.
        season_ru = re.search(r"сезон[\s.:]*(\d{1,2})", title_lower) or re.search(
            r"(\d{1,2})\s*-?\s*(?:й|ый|ой)?\s*сезон", title_lower
        )
        episode_ru = re.search(r"серия[\s.:]*(\d{1,3})", title_lower) or re.search(
            r"(\d{1,3})\s*-?\s*(?:я|ая)?\s*серия", title_lower
        )
        if season_ru or episode_ru:
            return (
                int(season_ru.group(1)) if season_ru else None,
                int(episode_ru.group(1)) if episode_ru else None,
            )

        return None, None

    def _is_season_pack(self, title: str) -> bool:
        """Check if release is a season pack."""
        title_lower = title.lower()

        # Explicit season pack markers (check first — most reliable)
        if any(x in title_lower for x in [
            "complete season", "season pack", "full season",
            "сезон целиком", "все серии", "полный сезон",
        ]):
            return True

        # "(01-12 из 12)" / "[01-08 of 08]" — форма «все N серий сезона».
        # Английское `of` — то же самое написание на тех же трекерах.
        if re.search(r"\d{1,3}\s*-\s*\d{1,3}\s*(?:из|of)\s*\d{1,3}", title_lower):
            return True

        # Диапазон серий («S02E01-08») — пак, хотя одиночный паттерн s\d+e\d
        # ниже нашёлся бы в нём первым и ответил бы «нет».
        if self._episode_range(title_lower) is not None:
            return True

        # Has any episode reference at all? If so, not a season pack.
        if re.search(r"s\d{1,2}e\d|(\d{1,2})x(\d{1,3})|episode[\s.]*\d|серия[\s.:]*\d", title_lower):
            return False

        # S01 format without any episode reference
        if re.search(r"s\d{1,2}(?:\b|[\.\s\-])", title_lower):
            return True

        # "Season X" without episode
        if re.search(r"season[\s.]*\d{1,2}(?![\s.]*episode)", title_lower):
            return True

        # «3 сезон» / «Сезон 3» без указания серии
        if re.search(r"сезон[\s.:]*\d{1,2}|\d{1,2}\s*-?\s*(?:й|ый|ой)?\s*сезон", title_lower):
            return True

        return False
