"""Search service for finding content."""

import asyncio
import re
import time
from difflib import SequenceMatcher
from typing import NamedTuple, Optional

import structlog

from bot.clients.base import ServiceConnectionError
from bot.clients.lidarr import LidarrClient
from bot.clients.prowlarr import ProwlarrClient
from bot.clients.radarr import RadarrClient
from bot.clients.sonarr import SonarrClient
from bot.models import ArtistInfo, ContentType, MovieInfo, SearchResult, SeriesInfo
from bot.services.scoring import ScoringService

logger = structlog.get_logger()


# Pre-compiled patterns (PERF-22): avoid re.compile per detect call.
_SERIES_PATTERNS = [
    re.compile(r"\bs\d{1,2}\b", re.IGNORECASE),          # S01, S1 (BUG-11: \b)
    re.compile(r"\bs\d{1,2}e\d{1,3}\b", re.IGNORECASE),  # S01E01
    re.compile(r"\bseason\s*\d+", re.IGNORECASE),
    re.compile(r"\bseries\s*\d+", re.IGNORECASE),
    re.compile(r"\b\d{1,2}x\d{1,3}\b"),                  # 1x01
    re.compile(r"сезон", re.IGNORECASE),
    re.compile(r"серия", re.IGNORECASE),
]

_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2}|21\d{2})\b")
_SE_RE = re.compile(r"s(\d{1,2})(?:e(\d{1,3}))?", re.IGNORECASE)
_SEASON_WORD_RE = re.compile(r"(?:season|сезон)\s*(\d+)", re.IGNORECASE)
_QUALITY_TOKENS = ("2160p", "4k", "4к", "uhd", "1080p", "720p", "480p")
_DETECT_TIMEOUT_S = 8.0  # PERF-01: cap parallel lookups
_MUSIC_QUERY_HARD_FLOOR = 3   # ignore Lidarr matches when query <3 chars

# PERF-01: detection-burst guard. A single free-text message previously fired
# concurrent Radarr+Sonarr+Lidarr `/lookup` calls with no cap, no cache and no
# cooldown — the exact code-path behind the prod incident where Radarr and
# Lidarr went 503 at the same time. Three mitigations, all module-level so
# they're shared across every SearchService instance (there's effectively one
# per request via get_services(), so per-instance state would be useless):
#
#   1. A semaphore capping concurrent *arr lookup coroutines globally at 2 —
#      even if 3 users search simultaneously, only 2 lookup calls are ever
#      in flight across the whole process.
#   2. A TTL cache (300s) keyed by normalized query — a retried/duplicate
#      search doesn't re-trigger the lookup burst.
#   3. A per-service circuit breaker — once a service raises
#      ServiceConnectionError (or any APIError with a 503 status), detection
#      skips that service for 30s instead of hammering an already-struggling
#      instance.
_DETECT_SEMAPHORE = asyncio.Semaphore(2)
_DETECTION_CACHE_TTL_S = 300.0
_DETECTION_CACHE_CAP = 100
_DETECTION_CACHE: dict[str, tuple[float, "DetectionResult"]] = {}
_CIRCUIT_BREAKER_COOLDOWN_S = 30.0
_CIRCUIT_BREAKER: dict[str, float] = {}  # service name -> monotonic retry_after


def _normalize_query(query: str) -> str:
    """Normalize a query for cache-key purposes (PERF-01)."""
    return re.sub(r"\s+", " ", query.lower().strip())


def _cache_get(key: str) -> Optional["DetectionResult"]:
    entry = _DETECTION_CACHE.get(key)
    if entry is None:
        return None
    deadline, result = entry
    if time.monotonic() >= deadline:
        _DETECTION_CACHE.pop(key, None)
        return None
    return result


def _cache_put(key: str, result: "DetectionResult") -> None:
    if key not in _DETECTION_CACHE and len(_DETECTION_CACHE) >= _DETECTION_CACHE_CAP:
        # Evict the oldest entry (smallest deadline) to keep the cache bounded.
        oldest_key = min(_DETECTION_CACHE, key=lambda k: _DETECTION_CACHE[k][0])
        _DETECTION_CACHE.pop(oldest_key, None)
    _DETECTION_CACHE[key] = (time.monotonic() + _DETECTION_CACHE_TTL_S, result)


def _breaker_is_open(service: str) -> bool:
    retry_after = _CIRCUIT_BREAKER.get(service)
    if retry_after is None:
        return False
    if time.monotonic() >= retry_after:
        _CIRCUIT_BREAKER.pop(service, None)
        return False
    return True


def _breaker_trip(service: str) -> None:
    _CIRCUIT_BREAKER[service] = time.monotonic() + _CIRCUIT_BREAKER_COOLDOWN_S


def _is_service_down_error(exc: BaseException) -> bool:
    """True for connection failures / 503s that should trip the breaker."""
    if isinstance(exc, ServiceConnectionError):
        return True
    status_code = getattr(exc, "status_code", None)
    return status_code == 503


async def _guarded_lookup(service: str, coro_factory):
    """Run a detection lookup under the global semaphore + circuit breaker.

    ``coro_factory`` is a zero-arg callable that creates the coroutine lazily
    — when the breaker is open we must not even instantiate it (an
    unawaited AsyncMock coroutine still warns/leaks).

    Returns the lookup result, or the underlying exception (never raised —
    detect_with_confidence already treats exceptions-in-gather as failures
    via `return_exceptions=True`, this just short-circuits the call entirely
    when the breaker is open).
    """
    if _breaker_is_open(service):
        return ServiceConnectionError(f"{service} circuit breaker open")
    async with _DETECT_SEMAPHORE:
        try:
            return await coro_factory()
        except BaseException as exc:  # noqa: BLE001 - surfaced to gather(return_exceptions=True) caller
            if _is_service_down_error(exc):
                _breaker_trip(service)
            return exc


class DetectionResult(NamedTuple):
    """Result of content type detection (LOGIC-28: confidence-based UX)."""
    content_type: ContentType
    confidence: float                       # 0.0..1.0
    reason: str                             # short label for logs
    candidates: dict[str, list[str]]        # {"movie": [...titles], "series": [...], "music": [...]}
    # LOGIC-06: the *full* lookup objects behind `candidates["movie"/"series"]`
    # (titles-only) for the winning content_type — so callers that already ran
    # detection don't have to re-hit Radarr/Sonarr lookup for the same query.
    # Empty for MUSIC/UNKNOWN winners or when detection short-circuited before
    # a lookup ran (e.g. "series_pattern"/"too_short"/"lookup_timeout").
    lookup_results: list = []


class SearchService:
    """Service for searching content across Prowlarr, Radarr, Sonarr, Lidarr."""

    def __init__(
        self,
        prowlarr: ProwlarrClient,
        radarr: RadarrClient,
        sonarr: SonarrClient,
        scoring: Optional[ScoringService] = None,
        lidarr: Optional[LidarrClient] = None,
    ):
        self.prowlarr = prowlarr
        self.radarr = radarr
        self.sonarr = sonarr
        self.lidarr = lidarr
        self.scoring = scoring or ScoringService()

    async def detect_with_confidence(self, query: str) -> DetectionResult:
        """
        Detect movie / series / music with confidence score.

        Year-aware priority (LOGIC-03): if query has a year, music is dropped —
        artists don't have release-year semantics in user queries.

        Fuzzy match (BUG-01, LOGIC-02): SequenceMatcher.ratio() instead of
        substring containment, so "Joker" doesn't match a random "Joker"-named
        artist with 1-letter overlap.

        Bounded latency (PERF-01): parallel lookups capped at 8s; on timeout
        returns UNKNOWN with confidence 0 so the user gets the button choice.

        Detection-burst guard (PERF-01): lookups are additionally routed
        through a global semaphore(2) + 300s TTL cache (by normalized query)
        + a 30s per-service circuit breaker after a connection error/503 —
        see the module-level `_DETECT_SEMAPHORE`/`_DETECTION_CACHE`/
        `_CIRCUIT_BREAKER` docstring for the full rationale.

        Failure surfacing (BUG-05): exceptions during lookups don't silently
        become empty arrays — they downgrade confidence and reason gets
        "lookup_failures=N" so the user sees the type-question instead of a
        wrong auto-pick.
        """
        log = logger.bind(query=query)
        clean_query = self._strip_quality_tokens(query.strip())
        clean_query_no_year = _YEAR_RE.sub("", clean_query).strip()
        query_year = self._extract_query_year(query)

        # Pre-filter (PERF-06): too short to meaningfully classify
        if len(clean_query_no_year) < 2:
            return DetectionResult(ContentType.UNKNOWN, 0.0, "too_short", {})

        # Series indicators in query text — high-confidence shortcut
        for pattern in _SERIES_PATTERNS:
            if pattern.search(clean_query):
                log.info("detect_content_type", winner="series", reason="series_pattern")
                return DetectionResult(ContentType.SERIES, 0.9, "series_pattern", {})

        # PERF-01: repeated/duplicate searches (retries, double-taps) must not
        # re-trigger the *arr lookup burst — serve from the TTL cache.
        cache_key = _normalize_query(query)
        cached = _cache_get(cache_key)
        if cached is not None:
            log.info("content_type_detected", winner=cached.content_type.value, reason="cache_hit")
            return cached

        # Parallel lookups with hard timeout (PERF-01), each routed through the
        # global semaphore + circuit breaker via _guarded_lookup.
        # Use clean_query_no_year for Lidarr (LOGIC-01) — MusicBrainz returns garbage on year strings.
        tasks: list = [
            asyncio.create_task(
                _guarded_lookup("radarr", lambda: self.radarr.lookup_movie(clean_query))
            ),
            asyncio.create_task(
                _guarded_lookup("sonarr", lambda: self.sonarr.lookup_series(clean_query))
            ),
        ]
        if self.lidarr is not None and len(clean_query_no_year) >= _MUSIC_QUERY_HARD_FLOOR:
            tasks.append(
                asyncio.create_task(
                    _guarded_lookup("lidarr", lambda: self.lidarr.lookup_artist(clean_query_no_year))
                )
            )

        try:
            gathered = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=_DETECT_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            for t in tasks:
                t.cancel()
            log.warning("detect_content_type", winner="unknown", reason="lookup_timeout")
            return DetectionResult(ContentType.UNKNOWN, 0.0, "lookup_timeout", {})

        movies_result, series_result = gathered[0], gathered[1]
        artists_result = gathered[2] if len(gathered) > 2 else []

        movies = movies_result if isinstance(movies_result, list) else []
        series = series_result if isinstance(series_result, list) else []
        artists = artists_result if isinstance(artists_result, list) else []

        failure_count = sum(
            1 for r in gathered if isinstance(r, BaseException)
        )

        if isinstance(movies_result, BaseException):
            log.warning("Radarr lookup failed during detection", error=str(movies_result))
        if isinstance(series_result, BaseException):
            log.warning("Sonarr lookup failed during detection", error=str(series_result))
        if len(gathered) > 2 and isinstance(artists_result, BaseException):
            log.warning("Lidarr lookup failed during detection", error=str(artists_result))

        # If most lookups failed, can't decide → UNKNOWN (BUG-05).
        if failure_count >= len(tasks):
            return DetectionResult(ContentType.UNKNOWN, 0.0, "all_lookups_failed", {})

        # Score top candidates from each service.
        movie_score = self._best_match_score(clean_query_no_year, movies, query_year, prefer_year=True)
        series_score = self._best_match_score(clean_query_no_year, series, query_year, prefer_year=True)
        # Music: stricter — require year to be absent in query (artists aren't year-tagged).
        if query_year is not None:
            music_score = 0.0
        else:
            music_score = self._best_match_score(clean_query_no_year, artists, None, prefer_year=False)
            # Music demotion: only beat movie/series if query is unambiguous (>=0.92).
            if music_score < 0.92:
                music_score *= 0.7

        scored = [
            (ContentType.MOVIE, movie_score, "movie_match", [getattr(m, "title", "?") for m in movies[:3]]),
            (ContentType.SERIES, series_score, "series_match", [getattr(s, "title", "?") for s in series[:3]]),
            (ContentType.MUSIC, music_score, "music_match", [getattr(a, "name", "?") for a in artists[:3]]),
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        top_type, top_score, reason, _ = scored[0]
        runner_up_score = scored[1][1]

        candidates = {
            "movie": [getattr(m, "title", "?") for m in movies[:3]],
            "series": [getattr(s, "title", "?") for s in series[:3]],
            "music": [getattr(a, "name", "?") for a in artists[:3]],
        }

        # OBS-11: log the winner with all candidates
        log.info(
            "content_type_detected",
            winner=top_type.value,
            confidence=round(top_score, 3),
            runner_up=round(runner_up_score, 3),
            reason=reason,
            failure_count=failure_count,
            candidates=candidates,
        )

        # Confidence threshold: below 0.7 → UNKNOWN so user gets the question.
        if top_score < 0.7:
            result = DetectionResult(ContentType.UNKNOWN, top_score, "low_confidence", candidates)
        # Tie-break: if top and runner-up are within 0.05, ask the user.
        elif top_score - runner_up_score < 0.05 and runner_up_score > 0.6:
            result = DetectionResult(ContentType.UNKNOWN, top_score, "ambiguous", candidates)
        else:
            # LOGIC-06: carry the full lookup objects for the winning type so
            # the caller (process_search -> SearchSession.lookup_candidates)
            # can skip a second identical Radarr/Sonarr lookup later in the
            # same flow (release selection / grab_best).
            winning_lookup = movies if top_type == ContentType.MOVIE else (
                series if top_type == ContentType.SERIES else []
            )
            result = DetectionResult(top_type, top_score, reason, candidates, list(winning_lookup))

        # PERF-01: cache the classification (even UNKNOWN/low-confidence — a
        # repeat of the exact same ambiguous query would just burn another
        # lookup burst for the same answer) unless a service failed, so a
        # transient 503 doesn't poison the cache for the retry.
        if failure_count == 0:
            _cache_put(cache_key, result)
        return result

    @staticmethod
    def _strip_quality_tokens(query: str) -> str:
        """Drop noisy quality tokens before similarity-matching titles."""
        out = query
        for tok in _QUALITY_TOKENS:
            out = re.sub(re.escape(tok), "", out, flags=re.IGNORECASE)
        return re.sub(r"\s+", " ", out).strip()

    @staticmethod
    def _extract_query_year(query: str) -> Optional[int]:
        m = _YEAR_RE.search(query)
        if not m:
            return None
        try:
            year = int(m.group(1))
        except ValueError:
            return None
        if 1900 <= year <= 2100:
            return year
        return None

    def _best_match_score(
        self,
        query: str,
        candidates: list,
        query_year: Optional[int],
        *,
        prefer_year: bool,
    ) -> float:
        """
        Score 0..1 for the best matching candidate using fuzzy ratio + year bonus.

        - SequenceMatcher.ratio over normalised lower-cased strings.
        - +0.15 bonus when years match within ±1 (only if prefer_year).
        - -0.20 penalty if year present in query but candidate.year is far off.
        """
        if not candidates:
            return 0.0

        q = query.lower().strip()
        if not q:
            return 0.0

        best = 0.0
        for cand in candidates[:5]:
            title = (
                getattr(cand, "title", None)
                or getattr(cand, "name", None)
                or ""
            )
            if not title:
                continue
            cand_lower = title.lower().strip()
            ratio = SequenceMatcher(None, q, cand_lower).ratio()

            # Substring helper: very-long candidate that fully contains the query.
            if len(q) >= 3 and q in cand_lower:
                ratio = max(ratio, min(0.85, len(q) / max(len(cand_lower), len(q))))

            cand_year = getattr(cand, "year", None)
            if prefer_year and query_year is not None and cand_year:
                if abs(query_year - cand_year) <= 1:
                    ratio = min(1.0, ratio + 0.15)
                else:
                    ratio = max(0.0, ratio - 0.20)

            if ratio > best:
                best = ratio

        return best

    async def search_releases(
        self,
        query: str,
        content_type: ContentType = ContentType.UNKNOWN,
        sort_by_score: bool = True,
        preferred_resolution: Optional[str] = None,
    ) -> list[SearchResult]:
        """Search for releases using Prowlarr.

        Important (LOGIC-04): we no longer drop results whose `detected_type`
        disagrees with the requested type — Russian indexers regularly mis-tag
        categories, and dropping cuts legitimate releases. Trust Prowlarr's
        category filter (driven by `content_type`) and let scoring rank.

        DEAD-06: `preferred_resolution` (user's "Качество" setting) is passed
        through to scoring so a same-quality release at the preferred
        resolution ranks above others instead of being purely resolution-maxed.
        """
        log = logger.bind(query=query, content_type=content_type.value)
        log.info("Searching for releases")

        t0 = time.monotonic()
        results = await self.prowlarr.search(query, content_type)
        prowlarr_ms = round((time.monotonic() - t0) * 1000, 1)

        if not results:
            log.info("No results found", prowlarr_ms=prowlarr_ms)
            return []

        raw_count = len(results)

        if sort_by_score:
            results = self.scoring.sort_results(results, content_type, preferred_resolution)

        # OBS-13: log top-N for debug
        top_preview = [
            {
                "title": (r.title or "?")[:80],
                "score": getattr(r, "calculated_score", 0),
                "indexer": r.indexer,
                "seeders": r.seeders,
                "size_gb": r.get_size_gb(),
                "detected_type": r.detected_type.value if r.detected_type else None,
                "detected_year": r.detected_year,
            }
            for r in results[:5]
        ]
        log.info(
            "search_completed",
            raw_count=raw_count,
            result_count=len(results),
            prowlarr_ms=prowlarr_ms,
            top=top_preview,
        )
        return results

    async def lookup_movie(self, query: str) -> list[MovieInfo]:
        """Look up movies in Radarr."""
        return await self.radarr.lookup_movie(query)

    async def lookup_series(self, query: str) -> list[SeriesInfo]:
        """Look up series in Sonarr."""
        return await self.sonarr.lookup_series(query)

    async def lookup_artist(self, query: str) -> list[ArtistInfo]:
        """Look up artists in Lidarr (returns [] if Lidarr is not configured)."""
        if self.lidarr is None:
            return []
        return await self.lidarr.lookup_artist(query)

    def parse_query(self, query: str) -> dict:
        """
        Parse search query to extract metadata.

        Note: the `title` field has year/season/quality stripped — that's the
        clean form for *lookup* APIs (Radarr/Sonarr lookup), not for Prowlarr
        search. Prowlarr should receive the original query so trackers can
        match against the year (LOGIC-05).
        """
        result = {
            "original": query,
            "title": query,
            "year": None,
            "season": None,
            "episode": None,
            "quality": None,
        }

        query_lower = query.lower()

        # Year — bounded with \b to avoid latching on 4-digit substrings (BUG-06).
        year_match = _YEAR_RE.search(query)
        if year_match:
            year = int(year_match.group(1))
            result["year"] = year
            result["title"] = _YEAR_RE.sub("", result["title"]).strip()

        # Season/episode — SxxEyy
        se_match = _SE_RE.search(query_lower)
        if se_match:
            result["season"] = int(se_match.group(1))
            if se_match.group(2):
                result["episode"] = int(se_match.group(2))
            result["title"] = _SE_RE.sub("", result["title"]).strip()

        # "Season N" / "сезон N" wording
        season_match = _SEASON_WORD_RE.search(query_lower)
        if season_match and result["season"] is None:
            result["season"] = int(season_match.group(1))
            result["title"] = _SEASON_WORD_RE.sub("", result["title"]).strip()

        # Quality — strip ALL recognised tokens (BUG-29) including Cyrillic "4К" (BUG-30).
        first_quality: Optional[str] = None
        for q in _QUALITY_TOKENS:
            if q.lower() in query_lower:
                token = q if q != "4k" else "2160p"
                if first_quality is None:
                    first_quality = token if token != "4к" else "2160p"
                result["title"] = re.sub(re.escape(q), "", result["title"], flags=re.IGNORECASE).strip()
        if first_quality:
            result["quality"] = first_quality

        # Collapse whitespace — also drops dangling punctuation around stripped year.
        result["title"] = re.sub(r"\s+", " ", result["title"]).strip(" -_:.()[]")

        return result
