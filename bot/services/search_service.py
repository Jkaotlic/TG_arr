"""Search service: content-type detection and release listing via Scryer.

Migration 2026-07-28. What changed and why:

- **Detection** used to fan out parallel `lookup` calls to Radarr, Sonarr and
  Lidarr, guarded by a global semaphore, a TTL cache and a per-service circuit
  breaker (all of which existed to survive the lookup burst taking three
  services down at once). Scryer answers all three video facets in ONE
  `searchMetadataMulti` query, so the semaphore and circuit breaker are gone.
  The TTL cache stays — a retried or double-tapped search still shouldn't hit
  the network twice.
- **Anime** is now a first-class outcome, not a flavour of SERIES.
- **Release search** is no longer a free-text Prowlarr query. Scryer searches
  per *title*, so the caller resolves/creates the title first (AddService.
  ensure_title) and passes its id here.
"""

import asyncio
import re
import time
from difflib import SequenceMatcher
from typing import NamedTuple, Optional

import structlog

from bot.clients.scryer import ScryerClient
from bot.models import ArtistInfo, ContentType, SearchResult, VIDEO_CONTENT_TYPES
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
_DETECT_TIMEOUT_S = 15.0  # metadata search fans out to TMDb/TVDB behind Scryer
_MUSIC_QUERY_HARD_FLOOR = 3   # ignore music matches when query <3 chars

# PERF-01: a retried/duplicate search (double-tap, "повторить") must not
# re-trigger the metadata lookup. Module-level so it is shared across the
# per-request SearchService instances created by get_services().
_DETECTION_CACHE_TTL_S = 300.0
_DETECTION_CACHE_CAP = 100
_DETECTION_CACHE: dict[str, tuple[float, "DetectionResult"]] = {}
# A close runner-up means the query has more than one credible interpretation.
# Keep the decision with the user rather than auto-selecting a content type.
_AMBIGUITY_MARGIN = 0.15
# Anime titles are also indexed as series by the metadata providers, so a
# near-tie between the two is the normal case rather than a real ambiguity —
# prefer the anime facet, which has its own library and `1080p` profile.
_ANIME_OVER_SERIES_MARGIN = 0.1


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


def _cache_clear() -> None:
    """Test helper — drop the shared detection cache."""
    _DETECTION_CACHE.clear()


class DetectionResult(NamedTuple):
    """Result of content type detection (LOGIC-28: confidence-based UX)."""
    content_type: ContentType
    confidence: float                       # 0.0..1.0
    reason: str                             # short label for logs
    candidates: dict[str, list[str]]        # {"movie": [...titles], "series": [...], ...}
    # The full metadata objects behind `candidates` for the winning type, so
    # callers that already ran detection don't have to search again before
    # adding the title. Empty for MUSIC/UNKNOWN winners or when detection
    # short-circuited before a lookup ran.
    lookup_results: list = []


class SearchService:
    """Content-type detection and release listing, backed by Scryer."""

    def __init__(
        self,
        scryer: ScryerClient,
        scoring: Optional[ScoringService] = None,
        lidarr=None,
        slskd=None,
    ):
        self.scryer = scryer
        self.lidarr = lidarr
        self.slskd = slskd
        self.scoring = scoring or ScoringService()

    async def detect_with_confidence(self, query: str) -> DetectionResult:
        """
        Detect movie / series / anime / music with a confidence score.

        Year-aware priority (LOGIC-03): if the query has a year, music is
        dropped — artists don't have release-year semantics in user queries.

        Fuzzy match (BUG-01, LOGIC-02): SequenceMatcher.ratio() instead of
        substring containment, so "Joker" doesn't match a random "Joker"-named
        artist with 1-letter overlap.

        Failure surfacing (BUG-05): an exception during lookup doesn't silently
        become an empty result set — it returns UNKNOWN so the user gets the
        type question instead of a wrong auto-pick.
        """
        log = logger.bind(query=query)
        clean_query = self._strip_quality_tokens(query.strip())
        clean_query_no_year = _YEAR_RE.sub("", clean_query).strip()
        query_year = self._extract_query_year(query)

        # Pre-filter (PERF-06): too short to meaningfully classify
        if len(clean_query_no_year) < 2:
            return DetectionResult(ContentType.UNKNOWN, 0.0, "too_short", {})

        # A season/episode marker settles "not a movie, not music" — but NOT
        # series-vs-anime, which are separate Scryer libraries with separate
        # quality profiles. So it narrows the candidate set instead of
        # short-circuiting the way it used to.
        episodic = any(pattern.search(clean_query) for pattern in _SERIES_PATTERNS)

        cache_key = _normalize_query(query)
        cached = _cache_get(cache_key)
        if cached is not None:
            log.info("content_type_detected", winner=cached.content_type.value, reason="cache_hit")
            return cached

        music_allowed = (
            (self.lidarr is not None or self.slskd is not None)
            and len(clean_query_no_year) >= _MUSIC_QUERY_HARD_FLOOR
            and query_year is None
            and not episodic
        )

        try:
            gathered = await asyncio.wait_for(
                asyncio.gather(
                    self.scryer.search_metadata_multi(clean_query),
                    self._lookup_artists(clean_query_no_year) if music_allowed else _empty_list(),
                    return_exceptions=True,
                ),
                timeout=_DETECT_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            log.warning("detect_content_type", winner="unknown", reason="lookup_timeout")
            return self._episodic_fallback(episodic, "lookup_timeout")

        video_result, artists_result = gathered

        # BUG-04: `return_exceptions=True` also captures CancelledError, which
        # would turn a shutdown (or a cancelled callback task) into a normal
        # "detection failed" answer. Re-raise it instead.
        for outcome in gathered:
            if isinstance(outcome, asyncio.CancelledError):
                raise outcome

        if isinstance(video_result, BaseException):
            log.warning("scryer_metadata_lookup_failed", error=str(video_result))
            return self._episodic_fallback(episodic, "metadata_lookup_failed")
        if isinstance(artists_result, BaseException):
            log.warning("music_lookup_failed", error=str(artists_result))
            artists_result = []

        video = video_result or {}
        artists = artists_result or []

        # An episodic query can only be SERIES or ANIME — a movie match there
        # would be a metadata coincidence, not the user's intent.
        facets = (
            (ContentType.SERIES, ContentType.ANIME) if episodic else VIDEO_CONTENT_TYPES
        )
        scored: list[tuple[ContentType, float, str, list]] = []
        for content_type in facets:
            items = video.get(content_type) or []
            score = self._best_match_score(clean_query_no_year, items, query_year, prefer_year=True)
            scored.append((content_type, score, f"{content_type.value}_match", items))

        music_score = 0.0
        if artists:
            music_score = self._best_match_score(clean_query_no_year, artists, None, prefer_year=False)
            # Music demotion: only beat video if the query is unambiguous.
            if music_score < 0.92:
                music_score *= 0.7
        scored.append((ContentType.MUSIC, music_score, "music_match", []))

        scored.sort(key=lambda x: x[1], reverse=True)
        top_type, top_score, reason, winning_items = scored[0]
        runner_up_type, runner_up_score = scored[1][0], scored[1][1]

        # Anime and series share metadata sources — frequently the *same*
        # entry appears in both facets, scoring identically. Treating that as
        # "ambiguous" would ask the user a question they can't answer better
        # than we can, so prefer the anime facet (its own library, its own
        # 1080p profile) and mark the tie as already resolved.
        anime_resolved = False
        if (
            {top_type, runner_up_type} == {ContentType.SERIES, ContentType.ANIME}
            and abs(top_score - runner_up_score) < _ANIME_OVER_SERIES_MARGIN
        ):
            anime_entry = next(e for e in scored if e[0] == ContentType.ANIME)
            top_type, top_score, reason, winning_items = (
                anime_entry[0], max(top_score, anime_entry[1]), "anime_over_series", anime_entry[3]
            )
            runner_up_score = min(runner_up_score, anime_entry[1])
            anime_resolved = True

        candidates = {
            content_type.value: [getattr(i, "title", "?") for i in (video.get(content_type) or [])[:3]]
            for content_type in VIDEO_CONTENT_TYPES
        }
        candidates["music"] = [getattr(a, "name", "?") for a in artists[:3]]

        log.info(
            "content_type_detected",
            winner=top_type.value,
            confidence=round(top_score, 3),
            runner_up=round(runner_up_score, 3),
            reason=reason,
            episodic=episodic,
            candidates=candidates,
        )

        # Confidence threshold: below 0.7 → UNKNOWN so the user gets the question.
        if top_score < 0.7:
            # …unless the query is episodic, where "which of series/anime" is a
            # far better question to answer ourselves than to bounce back at a
            # user who typed an unambiguous "S01E05".
            result = (
                DetectionResult(top_type, top_score, "episodic_low_confidence", candidates, list(winning_items))
                if episodic and winning_items
                else self._episodic_fallback(episodic, "low_confidence", candidates)
            )
        elif (
            not anime_resolved
            and top_score - runner_up_score < _AMBIGUITY_MARGIN
            and runner_up_score > 0.6
        ):
            # `anime_resolved` guards the series/anime tie above: it is not a
            # real ambiguity, it was already decided in favour of anime.
            result = DetectionResult(ContentType.UNKNOWN, top_score, "ambiguous", candidates)
        else:
            result = DetectionResult(top_type, top_score, reason, candidates, list(winning_items))

        _cache_put(cache_key, result)
        return result

    @staticmethod
    def _episodic_fallback(
        episodic: bool, reason: str, candidates: Optional[dict] = None
    ) -> DetectionResult:
        """Degrade gracefully when the metadata lookup can't decide.

        For an episodic query ("Breaking Bad S01E05") the season marker is
        itself strong evidence, so fall back to SERIES rather than asking the
        user a question they already answered. Otherwise return UNKNOWN.
        """
        if episodic:
            return DetectionResult(ContentType.SERIES, 0.9, f"series_pattern:{reason}", candidates or {})
        return DetectionResult(ContentType.UNKNOWN, 0.0, reason, candidates or {})

    async def _lookup_artists(self, query: str) -> list[ArtistInfo]:
        """Artist lookup for detection — Lidarr first, slskd as the fallback."""
        if self.lidarr is not None:
            try:
                return await self.lidarr.lookup_artist(query)
            except Exception as e:
                logger.warning("lidarr_lookup_failed", error=str(e))
        if self.slskd is not None:
            try:
                return await self.slskd.lookup_artists(query)
            except Exception as e:
                logger.warning("slskd_lookup_failed", error=str(e))
        return []

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

    @staticmethod
    def _candidate_forms(candidate) -> list[str]:
        """Every string a candidate can legitimately be matched against.

        Metadata titles carry a subtitle far more often than a user types one
        ("Frieren: Beyond Journey's End"), and they are localised while the
        slug stays in latin script ("sousou-no-frieren"). Matching only against
        the display title made both cases score below the confidence bar and
        bounced a perfectly clear query back at the user as a question.
        """
        forms: list[str] = []
        title = getattr(candidate, "title", None) or getattr(candidate, "name", None) or ""
        if title:
            forms.append(title)
            # The part before a subtitle separator, e.g. "Frieren" in
            # "Frieren: Beyond Journey's End".
            head = re.split(r"\s*[:–—]\s|\s+-\s+", title, maxsplit=1)[0]
            if head and head != title:
                forms.append(head)
        slug = getattr(candidate, "slug", None)
        if slug:
            forms.append(slug.replace("-", " "))
        return [f.lower().strip() for f in forms if f and f.strip()]

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

        - SequenceMatcher.ratio over normalised lower-cased strings, taken over
          every form of the candidate (title, title-before-subtitle, slug).
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
            forms = self._candidate_forms(cand)
            if not forms:
                continue

            ratio = 0.0
            for form in forms:
                ratio = max(ratio, SequenceMatcher(None, q, form).ratio())
                # Substring helper for a long candidate containing the query.
                # Scaled by length, so it only helps when the query is a
                # meaningful share of the title — never enough on its own to
                # promote an unrelated long title.
                if len(q) >= 3 and q in form:
                    ratio = max(ratio, min(0.85, len(q) / max(len(form), len(q))))

            cand_year = getattr(cand, "year", None)
            if prefer_year and query_year is not None and cand_year:
                if abs(query_year - cand_year) <= 1:
                    ratio = min(1.0, ratio + 0.15)
                else:
                    ratio = max(0.0, ratio - 0.20)

            if ratio > best:
                best = ratio

        return best

    async def search_metadata(self, query: str, content_type: ContentType) -> list:
        """Metadata candidates for one facet (used when the user picks a type)."""
        if content_type not in VIDEO_CONTENT_TYPES:
            return []
        return await self.scryer.search_metadata(query, content_type)

    async def search_releases(
        self,
        title_id: str,
        content_type: ContentType = ContentType.UNKNOWN,
        season: Optional[int] = None,
        episode: Optional[int] = None,
        preferred_resolution: Optional[str] = None,
        limit: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> list[SearchResult]:
        """List indexer candidates for a title already present in Scryer.

        Scryer queries Prowlarr itself and applies the quality profile plus the
        Rego rules, so every result already carries a verdict; the local scoring
        only orders ties (see `ScoringService.sort_results`).
        """
        log = logger.bind(title_id=title_id, content_type=content_type.value)

        t0 = time.monotonic()
        results = await self.scryer.search_releases(
            title_id, season=season, episode=episode, limit=limit, timeout=timeout
        )
        search_ms = round((time.monotonic() - t0) * 1000, 1)

        if not results:
            log.info("No results found", search_ms=search_ms)
            return []

        results = self.scoring.sort_results(results, content_type, preferred_resolution)

        log.info(
            "search_completed",
            result_count=len(results),
            allowed_count=sum(1 for r in results if r.scryer_allowed),
            search_ms=search_ms,
            top=[
                {
                    "title": (r.title or "?")[:80],
                    "scryer_score": r.scryer_score,
                    "allowed": r.scryer_allowed,
                    "score": r.calculated_score,
                    "indexer": r.indexer,
                    "seeders": r.seeders,
                    "size_gb": round(r.get_size_gb(), 2),
                }
                for r in results[:5]
            ],
        )
        return results

    async def lookup_artist(self, query: str) -> list[ArtistInfo]:
        """Look up artists (Lidarr, falling back to slskd)."""
        return await self._lookup_artists(query)

    def parse_query(self, query: str) -> dict:
        """
        Parse a search query into title / year / season / episode / quality.

        The `title` field has year/season/quality stripped — that's the clean
        form for the metadata search. Season/episode are used to narrow the
        release search to one season or episode.
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


async def _empty_list() -> list:
    """Awaitable placeholder so `gather` keeps a fixed shape when music is off."""
    return []
