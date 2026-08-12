"""Search service: content-type detection and release listing via *arr.

Rollback 2026-08-10. What changed back and why:

- **Detection** fans out parallel `lookup` calls to Radarr, Sonarr and Lidarr
  again, guarded by a global semaphore and a per-service circuit breaker.
  The rollback removed both because the previous backend answered all three
  video facets in ONE `searchMetadataMulti` query — a concurrency limiter
  guarding three parallel calls had nothing to guard. Now there ARE three
  parallel calls again, and *arr's `lookup` proxies out to
  TMDb/TVDB/MusicBrainz (slow, externally rate-limited), so without a ceiling
  a burst of searches takes all three services down at once — the incident
  these existed to prevent before that migration. The TTL cache survived that
  migration unchanged and stays: a retried or double-tapped search still
  shouldn't hit the network twice.
- **Anime** is still a first-class outcome, not a flavour of SERIES, but it no
  longer comes from a dedicated facet on the previous backend. Sonarr's
  `/series/lookup` returns one flat list, and `series_type` on a result not
  yet in the catalog is always "standard" — so anime-ness is read from the
  `Animation` genre Sonarr/TVDB attaches to the title, splitting the flat
  list into series and anime candidate sets before the existing
  scoring/tie-break logic runs.
- **Release search** is `search_releases_for_title` (Task 9): Radarr/Sonarr's
  interactive search (`GET .../release?movieId=`/`?seriesId=`) already judges
  every candidate against the user's quality profile and custom formats —
  `customFormatScore`, `rejected`, human-readable `rejections`. That verdict
  is authoritative; the local `ScoringService` only breaks ties (see
  docs/superpowers/sdd/2026-08-10-arr-restore/task-9-brief.md for the
  live-measurement rationale). `search_metadata`/`get_seasons` are **gone**:
  they served the "resolve a catalog title, then list its releases by title
  id" flow, and the handlers now resolve the Radarr/Sonarr id themselves via
  `lookup_movies`/`lookup_series` before calling `search_releases_for_title`.
"""

import asyncio
import re
import time
from difflib import SequenceMatcher
from typing import Any, NamedTuple, Optional

import structlog

from bot.clients.base import AuthenticationError, ServiceConnectionError
from bot.clients.lidarr import LidarrClient
from bot.clients.prowlarr import ProwlarrClient
from bot.clients.radarr import RadarrClient
from bot.clients.slskd import SlskdClient
from bot.clients.sonarr import SonarrClient
from bot.models import ArtistInfo, ContentType, MovieInfo, SearchResult, SeriesInfo, VIDEO_CONTENT_TYPES
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
# `\d{1,2}(?!\d)` вместо `\d+`: «Ведьмак 2 сезон 1080p» иначе даёт season=1080.
_SEASON_WORD_RE = re.compile(r"(?:season|сезон)\s*(\d{1,2})(?!\d)", re.IGNORECASE)
# Обратный порядок — обычный для русского: «4 сезон», «3-й сезон». Живой прогон
# 2026-08-12: «Тед Лассо 4 сезон» давал season=None, и бот спрашивал сезон,
# который пользователь уже назвал.
_SEASON_BEFORE_WORD_RE = re.compile(r"(\d{1,2})\s*-?\s*(?:й|ый|ой)?\s*сезон", re.IGNORECASE)
_QUALITY_TOKENS = ("2160p", "4k", "4к", "uhd", "1080p", "720p", "480p")
# Each lookup fans out to TMDb/TVDB/MusicBrainz directly now (no more single
# round-trip to the previous backend). Live measurement 2026-08-10: Radarr
# lookup ~3.4s, Sonarr ~34.4s — 15s (the pre-rollback value) would spuriously
# time out most real Sonarr detections, so this needs real headroom above that.
_DETECT_TIMEOUT_S = 40.0
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

# One user search = three parallel metadata lookups, and *arr's `lookup`
# proxies to TMDb/TVDB/MusicBrainz, so it is slow. Without a ceiling a burst
# of searches takes all three services down at once — which is why these
# existed before the previous-backend migration removed them (that backend
# answered in one query, so they were genuinely dead weight then).
_DETECTION_SEMAPHORE = asyncio.Semaphore(6)


class _CircuitBreaker:
    """Stop calling a service that keeps failing.

    Opens after `threshold` consecutive failures and stays open for
    `cooldown_s`; a single success closes it again.
    """

    def __init__(self, threshold: int = 3, cooldown_s: float = 60.0):
        self._threshold = threshold
        self._cooldown_s = cooldown_s
        self._failures: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}

    def is_open(self, service: str) -> bool:
        opened = self._opened_at.get(service)
        if opened is None:
            return False
        if time.monotonic() - opened >= self._cooldown_s:
            # Cooldown elapsed — let exactly one probe through.
            self._opened_at.pop(service, None)
            self._failures[service] = 0
            return False
        return True

    def record_success(self, service: str) -> None:
        self._failures[service] = 0
        self._opened_at.pop(service, None)

    def record_failure(self, service: str) -> None:
        count = self._failures.get(service, 0) + 1
        self._failures[service] = count
        if count >= self._threshold:
            self._opened_at[service] = time.monotonic()

    def reset(self) -> None:
        """Test helper — forget every recorded failure."""
        self._failures.clear()
        self._opened_at.clear()


_CIRCUIT_BREAKER = _CircuitBreaker()


def describe_search_failure(exc: BaseException) -> str:
    """Turn a search failure into something the user can act on."""
    if isinstance(exc, ServiceConnectionError):
        return (
            "Индексеры не ответили вовремя. Обычно это залипший трекер — "
            "попробуйте повторить запрос. Состояние сервисов — в /health."
        )
    if isinstance(exc, AuthenticationError):
        return "Ошибка авторизации в медиасервисе — проверьте API-ключи."
    return "Поиск временно недоступен"


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


def _reset_module_state() -> None:
    """Test helper — reset every module-level piece of shared state.

    `_DETECTION_CACHE` and `_CIRCUIT_BREAKER` are both process-wide globals
    (shared across every SearchService instance, not per-request), so pytest
    running the whole suite in one process means a test that trips the
    breaker for a service can leave it open for an unrelated, later test in
    a different file. tests/conftest.py's autouse fixture calls this (instead
    of `_cache_clear()` alone) so both reset between every test.
    """
    _cache_clear()
    _CIRCUIT_BREAKER.reset()


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
    """Content-type detection and release listing, backed by the *arr stack."""

    def __init__(
        self,
        radarr: RadarrClient,
        sonarr: SonarrClient,
        lidarr: Optional[LidarrClient] = None,
        prowlarr: Optional[ProwlarrClient] = None,
        scoring: Optional[ScoringService] = None,
        slskd: Optional[SlskdClient] = None,
    ):
        self.radarr = radarr
        self.sonarr = sonarr
        self.lidarr = lidarr
        self.prowlarr = prowlarr
        self.slskd = slskd
        self.scoring = scoring or ScoringService()

    async def _lookup_branch(self, service: str, coro_factory) -> list[Any]:
        """Run one lookup under the semaphore, respecting the breaker."""
        if _CIRCUIT_BREAKER.is_open(service):
            logger.warning("lookup_skipped_breaker_open", service=service)
            return []
        try:
            async with _DETECTION_SEMAPHORE:
                result = await coro_factory()
            _CIRCUIT_BREAKER.record_success(service)
            return result
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _CIRCUIT_BREAKER.record_failure(service)
            logger.warning("lookup_failed", service=service, error=str(e))
            return []

    async def lookup_movies(self, query: str) -> list[MovieInfo]:
        """Guarded Radarr title lookup for a caller outside `detect_content_type`
        (Task 12, review fix round 1: an explicit `/movie` search, or a
        title-candidate re-lookup, used to call `self.radarr.lookup_movie`
        directly — bypassing the semaphore/circuit-breaker `_lookup_branch`
        exists specifically to provide. Every entry point that hits *arr's
        slow, externally rate-limited TMDb-backed lookup goes through the
        same protection now, not just auto-detection; see `_lookup_branch`'s
        docstring for the "burst of searches takes the service down"
        incident this exists to prevent).
        """
        return await self._lookup_branch("radarr", lambda: self.radarr.lookup_movie(query))

    async def lookup_series(self, query: str) -> list[SeriesInfo]:
        """Guarded Sonarr title lookup — see `lookup_movies`. Returns the raw,
        unsplit list; pair with `split_series_candidates` to separate anime
        from plain series, the same way `detect_content_type` does.
        """
        return await self._lookup_branch("sonarr", lambda: self.sonarr.lookup_series(query))

    @staticmethod
    def split_series_candidates(series_list: list) -> tuple[list, list]:
        """Partition Sonarr's lookup_series results into (series, anime).

        *arr has no separate anime facet: Sonarr's `/series/lookup` returns
        one flat list, and `series_type` on a result not yet in the catalog
        is always "standard" (rollback 2026-08-10 live measurement). Genre is
        the only signal *arr offers before a title is added, so an
        "Animation" genre marks a result as an anime candidate instead of a
        plain series one.

        Public (Task 12, review fix round 1 — was `_split_series_candidates`):
        `bot/handlers/search/commands.py` needed this same split for an
        explicit `/series`/`/anime` search, not just auto-detection.
        """
        series: list = []
        anime: list = []
        for item in series_list:
            genres = {g.lower() for g in (getattr(item, "genres", None) or [])}
            (anime if "animation" in genres else series).append(item)
        return series, anime

    async def detect_content_type(self, query: str) -> DetectionResult:
        """
        Detect movie / series / anime / music with a confidence score.

        Year-aware priority (LOGIC-03): if the query has a year, music is
        dropped — artists don't have release-year semantics in user queries.

        Fuzzy match (BUG-01, LOGIC-02): SequenceMatcher.ratio() instead of
        substring containment, so "Joker" doesn't match a random "Joker"-named
        artist with 1-letter overlap.

        Failure surfacing (BUG-05): a lookup failing doesn't silently become
        an empty result set — each branch is guarded by `_lookup_branch`
        (semaphore + circuit breaker), and if every branch comes back empty
        the confidence score is 0 so the user gets the type question instead
        of a wrong auto-pick.
        """
        log = logger.bind(query=query)
        clean_query = self._strip_quality_tokens(query.strip())
        clean_query_no_year = _YEAR_RE.sub("", clean_query).strip()
        query_year = self._extract_query_year(query)

        # Pre-filter (PERF-06): too short to meaningfully classify. A 1-char
        # query is a cheap way to trigger three external TMDb/TVDB/MusicBrainz
        # lookups; this guard exists to make that not free. The bot's own
        # caller (bot/handlers/search/commands.py) already rejects len<2
        # before calling here, but detect_content_type is a public method and
        # must not rely on that — a future caller that skips it must not get
        # three lookups for a single keystroke.
        if len(clean_query_no_year) < 2:
            return DetectionResult(ContentType.UNKNOWN, 0.0, "too_short", {})

        # A season/episode marker settles "not a movie, not music" — but NOT
        # series-vs-anime, which score separately. So it narrows the
        # candidate set instead of short-circuiting before the lookup runs.
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
                    self._lookup_branch("radarr", lambda: self.radarr.lookup_movie(clean_query)),
                    self._lookup_branch("sonarr", lambda: self.sonarr.lookup_series(clean_query)),
                    self._lookup_branch("lidarr", lambda: self._lookup_artists(clean_query_no_year))
                    if music_allowed
                    else _empty_list(),
                    return_exceptions=True,
                ),
                timeout=_DETECT_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            log.warning("detect_content_type", winner="unknown", reason="lookup_timeout")
            return self._episodic_fallback(episodic, "lookup_timeout")

        # BUG-04: `return_exceptions=True` also captures CancelledError, which
        # would turn a shutdown (or a cancelled callback task) into a normal
        # "detection failed" answer. Re-raise it instead. (_lookup_branch
        # already swallows every other exception into `[]`, so this is the
        # only kind of BaseException that can still show up here.)
        for outcome in gathered:
            if isinstance(outcome, asyncio.CancelledError):
                raise outcome

        movies_result, series_result, artists_result = gathered
        movies = movies_result if isinstance(movies_result, list) else []
        all_series = series_result if isinstance(series_result, list) else []
        artists = artists_result if isinstance(artists_result, list) else []

        series_candidates, anime_candidates = self.split_series_candidates(all_series)
        facet_candidates = {
            ContentType.MOVIE: movies,
            ContentType.SERIES: series_candidates,
            ContentType.ANIME: anime_candidates,
        }

        # An episodic query can only be SERIES or ANIME — a movie match there
        # would be a metadata coincidence, not the user's intent.
        facets = (
            (ContentType.SERIES, ContentType.ANIME) if episodic else VIDEO_CONTENT_TYPES
        )
        scored: list[tuple[ContentType, float, str, list]] = []
        for content_type in facets:
            items = facet_candidates[content_type]
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
            content_type.value: [getattr(i, "title", "?") for i in (facet_candidates.get(content_type) or [])[:3]]
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

    # `search_metadata` and `get_seasons` are gone (rollback 2026-08-10,
    # Tasks 14/15). They existed to serve the removed backend's "resolve a
    # catalog title, then list its releases by title id" flow. *arr's
    # interactive search lists releases straight from a Radarr/Sonarr id, so
    # Task 12's handlers resolve the id themselves via `lookup_movies` /
    # `lookup_series` and call `search_releases_for_title` directly. The two
    # methods survived Tasks 8-12 only as NotImplementedError stubs naming a
    # future task; with no caller left in `bot/`, keeping permanently-raising
    # public methods on the service would be worse than deleting them.
    # Season data now comes from Sonarr's own series payload, and the picker
    # searches one season via `SonarrClient.search_season`.

    async def search_releases_for_title(
        self,
        content_type: ContentType,
        arr_id: int,
        season: Optional[int] = None,
        preferred_resolution: Optional[str] = None,
    ) -> list[SearchResult]:
        """Releases for a title already in the catalog, ordered by *arr's verdict.

        Order: accepted before rejected, then by customFormatScore, then by the
        local ScoringService as a tie-break only. Rejected releases are kept
        rather than hidden — the user may still want one, and `rejections`
        explains the cost.

        Ordering is delegated to `ScoringService.sort_results` rather than
        re-implemented here. That matters beyond DRY: `sort_results` also
        *writes* `calculated_score` onto each result, which the release card
        renders and which gates the "Скачать лучшее" button against
        `auto_grab_score_threshold`. An inline sort using the score only as a
        sort key left every result at its default 0 — the card showed
        "Оценка: 0/100" and the auto-grab button could never appear.
        """
        if content_type is ContentType.MOVIE:
            releases = await self.radarr.get_releases(arr_id)
        else:
            releases = await self.sonarr.get_releases(arr_id, season_number=season)

        ordered = self.scoring.sort_results(releases, content_type, preferred_resolution)

        # DEAD-13/OBS: log what the user is about to be shown, so a complaint
        # like "it picked the wrong one" can be reconstructed from prod logs
        # without asking them to search again. The preview carries *arr's
        # verdict alongside the size, since the verdict now drives the order.
        logger.info(
            "search_completed",
            content_type=content_type.value,
            arr_id=arr_id,
            season=season,
            result_count=len(ordered),
            rejected_count=sum(1 for r in ordered if r.rejected),
            top=[
                {
                    "title": r.title[:120],
                    "indexer": r.indexer,
                    "size_gb": round(r.get_size_gb(), 2),
                    "seeders": r.seeders,
                    "custom_format_score": r.custom_format_score,
                    "rejected": r.rejected,
                }
                for r in ordered[:3]
            ],
        )
        return ordered

    async def search_free_text(
        self,
        query: str,
        content_type: ContentType = ContentType.UNKNOWN,
        preferred_resolution: Optional[str] = None,
    ) -> list[SearchResult]:
        """Releases for a title Radarr/Sonarr's catalogue does not know.

        `search_releases_for_title` above needs a movie/series id already in the
        library — that is *arr's interactive search, and the only way to get
        *arr's own verdict on a release. This is the other mode: a raw Prowlarr
        query with no catalogue entry behind it, for what the library cannot
        express (a title TMDb/TVDB never heard of, a concert, a rip under a name
        no metadata provider carries).

        Results carry `origin="prowlarr"`, which is what routes them down
        `AddService.grab_release`'s push chain instead of the native path —
        Prowlarr numbers indexers differently from *arr, so its `indexer_id` is
        meaningless to *arr's native grab endpoint.

        No *arr verdict exists here, so `ScoringService` is the sole ranking
        (see `sort_results`, case 3) — and it writes `calculated_score` as well
        as ordering by it, which the release card renders.

        No category filter when the facet is UNKNOWN: the point of this mode is
        that the type is not known up front. `_normalize_result` still fills
        `detected_type` from Prowlarr's own category ids.
        """
        if self.prowlarr is None:
            raise ServiceConnectionError(
                "Prowlarr не настроен — свободный поиск недоступен"
            )

        results = await self.prowlarr.search(query, content_type)
        if not results:
            logger.info("free_search_completed", query=query, result_count=0)
            return []

        ordered = self.scoring.sort_results(results, content_type, preferred_resolution)
        logger.info(
            "free_search_completed",
            query=query,
            content_type=content_type.value,
            result_count=len(ordered),
            top=[
                {
                    "title": r.title[:120],
                    "indexer": r.indexer,
                    "size_gb": round(r.get_size_gb(), 2),
                    "seeders": r.seeders,
                    "score": r.calculated_score,
                }
                for r in ordered[:3]
            ],
        )
        return ordered

    async def lookup_artist(self, query: str) -> list[ArtistInfo]:
        """Look up artists (Lidarr, falling back to slskd)."""
        return await self._lookup_artists(query)

    def parse_query(self, query: str) -> dict[str, Any]:
        """
        Parse a search query into title / year / season / episode / quality.

        The `title` field has year/season/quality stripped — that's the clean
        form for the metadata search. Season/episode are used to narrow the
        release search to one season or episode.
        """
        result: dict[str, Any] = {
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

        # "N сезон" / "N-й сезон" — обратный порядок, обычный для русского
        season_ru_match = _SEASON_BEFORE_WORD_RE.search(query_lower)
        if season_ru_match and result["season"] is None:
            result["season"] = int(season_ru_match.group(1))
            result["title"] = _SEASON_BEFORE_WORD_RE.sub("", result["title"]).strip()

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
