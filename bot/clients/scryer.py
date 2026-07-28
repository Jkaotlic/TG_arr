"""Scryer GraphQL client.

Scryer 0.17.2 replaced the Radarr/Sonarr pair on 2026-07-24. It exposes a
single GraphQL endpoint (`POST /graphql`) — there is no REST API — and
authenticates with a JWT obtained from the `login` mutation.

Three things about this API drive the design here:

1. **The token expires.** `SCRYER_JWT_ACCESS_TTL_SECONDS=86400`, so a bot that
   caches the token forever breaks once a day. `execute()` logs in lazily,
   refreshes proactively before the recorded expiry, and re-logs-in once on an
   auth rejection (see `_is_auth_error`).
2. **Errors arrive with HTTP 200.** A failed operation returns
   `{"data": null, "errors": [...]}` — status 200. Every response is checked
   for `errors` explicitly, otherwise a failure reads as a success with empty
   data.
3. **Release candidates carry secrets.** `downloadUrl` embeds Prowlarr's api
   key and the tracker passkey; `candidateToken` is a JWT with that same URL
   inside it. Neither is ever logged — see `mask_release_secrets`.

Scryer owns the quality policy (profile `4K Remux + 1080P Fallback` for
movies/series, `1080p` for anime, plus the `English Audio + Russian Subtitles`
Rego rule). The bot does not re-implement it: `searchReleases` already returns
Scryer's verdict per release (`qualityProfileDecision`), which the bot surfaces
and sorts by rather than overriding.
"""

import hashlib
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Optional

import structlog

from bot.clients.base import (
    APIError,
    AuthenticationError,
    BaseAPIClient,
)
from bot.models import (
    ContentType,
    MovieInfo,
    QualityProfile,
    QueueResult,
    AddTitleOutcome,
    RootFolder,
    ScryerCalendarItem,
    ScryerHealth,
    ScryerQueueItem,
    ScryerWantedItem,
    SearchResult,
    IndexerStat,
    QualityInfo,
    SeriesInfo,
)
from bot.services.release_parser import (
    extract_season_episode,
    extract_year,
    is_season_pack,
    parse_quality,
)

logger = structlog.get_logger()

#: Query params in an indexer download URL that carry credentials. Mirrors
#: `add_service._SENSITIVE_QUERY_PARAMS` — Prowlarr's download proxy nests the
#: original tracker URL (with its passkey) inside `link`/`file`.
_SENSITIVE_PARAMS = {"apikey", "api_key", "token", "passkey", "auth", "authkey", "link", "file", "r", "rss"}

#: Substrings that mark a GraphQL error as "your token is no longer valid".
_AUTH_ERROR_MARKERS = ("unauthorized", "unauthenticated", "invalid token", "token expired", "expired token")

#: Refresh the JWT this many seconds before it actually expires, so a long
#: search started just before expiry doesn't fail halfway through.
_TOKEN_REFRESH_MARGIN_S = 300.0

#: Fallback lifetime when Scryer doesn't report `expiresAt` (TTL is 24h).
_TOKEN_DEFAULT_TTL_S = 23 * 3600.0


def root_folder_id(path: str) -> str:
    """Stable, callback-safe id for a Scryer root folder.

    Scryer's `RootFolderPayload` has no id — the path is the identity. The path
    itself cannot be used as a settings `callback_data` value: aiogram's
    CallbackData packs fields separated by ':' and a Windows path
    ("G:\\radarr\\Films") contains one, so packing raises — and Telegram caps
    callback_data at 64 bytes anyway. A short digest of the path is stable
    across restarts (unlike a list index) and safe in both respects.
    """
    return hashlib.sha1(path.encode("utf-8")).hexdigest()[:12]  # noqa: S324 -- identity, not security


def mask_release_secrets(url: Optional[str]) -> str:
    """Return a log-safe form of an indexer download URL.

    Keeps scheme/host/path (useful for "which indexer served this") and masks
    every credential-bearing query parameter.
    """
    if not url:
        return ""
    if url.startswith("magnet:"):
        return url[:80]
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return "***"
    if not parsed.query:
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"[:120]
    parts = [
        f"{k}=***" if k.lower() in _SENSITIVE_PARAMS else f"{k}={v}"
        for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    ]
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{'&'.join(parts)}"[:120]


class ScryerGraphQLError(APIError):
    """A GraphQL operation returned `errors` (with HTTP 200)."""

    def __init__(self, message: str, errors: Optional[list[dict[str, Any]]] = None):
        super().__init__(message)
        self.errors = errors or []


# --------------------------------------------------------------------------
# GraphQL documents. Kept as module constants so they are compiled once and
# are greppable when comparing against the schema.
# --------------------------------------------------------------------------

_LOGIN = """
mutation Login($input: LoginInput!) {
  login(input: $input) { token expiresAt }
}
"""

_METADATA_FIELDS = "tvdbId name imdbId slug type year overview posterUrl language runtimeMinutes"

_SEARCH_METADATA_MULTI = f"""
query SearchMulti($query: String!, $limit: Int!, $language: String!) {{
  searchMetadataMulti(query: $query, limit: $limit, language: $language) {{
    movies {{ {_METADATA_FIELDS} }}
    series {{ {_METADATA_FIELDS} }}
    anime {{ {_METADATA_FIELDS} }}
  }}
}}
"""

_SEARCH_METADATA = f"""
query SearchMetadata($query: String!, $type: MediaFacetValue!, $limit: Int!, $language: String!, $year: Int) {{
  searchMetadata(query: $query, type: $type, limit: $limit, language: $language, year: $year) {{
    {_METADATA_FIELDS}
  }}
}}
"""

_TITLE_FIELDS = """
  id name slug facet year monitored overview posterUrl imdbId runtimeMinutes
  qualityTier currentQualityTier qualityProfileId rootFolderId rootFolderPath libraryId
  episodesOwned episodesTotal sizeBytes
  externalIds { source value }
  mediaFiles { id }
"""

_TITLES = f"""
query Titles($facet: MediaFacetValue, $query: String, $limit: Int, $offset: Int) {{
  titles(facet: $facet, query: $query, limit: $limit, offset: $offset) {{
    totalCount hasMore
    items {{ {_TITLE_FIELDS} }}
  }}
}}
"""

_TITLE = f"""
query Title($id: ID!) {{
  title(id: $id) {{ {_TITLE_FIELDS} }}
}}
"""

_SEARCH_RELEASES = """
query SearchReleases($input: SearchReleasesInput!) {
  searchReleases(input: $input) {
    source title link downloadUrl sourceKind sizeBytes publishedAt
    seeders peers infoHash freeleech candidateToken
    autoEligible autoDecisionCode autoDecisionSummary
    parsedRelease {
      rawTitle quality source videoCodec audio isRemux isProperUpload
      isDualAudio isAtmos isDolbyVision detectedHdr
      episode { season episodeNumbers }
    }
    qualityProfileDecision { allowed blockCodes releaseScore preferenceScore }
    queueScope {
      __typename
      ... on TitleScopePayload { wholeTitle }
      ... on EpisodeScopePayload { episodeId }
      ... on EpisodeSetScopePayload { episodeIds }
      ... on SeriesMovieScopePayload { seriesMovieLinkId }
      ... on CollectionScopePayload { collectionId }
    }
  }
}
"""

_ADD_TITLE_RESULT = f"""
    title {{ {_TITLE_FIELDS} }}
    reusedExistingTitle
    downloadJobId
    queuedDownload {{ status jobId titleId titleName }}
"""

_ADD_TITLE = f"""
mutation AddTitle($input: AddTitleInput!) {{
  addTitle(input: $input) {{ {_ADD_TITLE_RESULT} }}
}}
"""

_ADD_TITLE_AND_QUEUE = f"""
mutation AddTitleAndQueue($input: AddTitleInput!) {{
  addTitleAndQueueDownload(input: $input) {{ {_ADD_TITLE_RESULT} }}
}}
"""

_QUEUE_EXISTING = """
mutation QueueExisting($input: QueueDownloadInput!) {
  queueExistingTitleDownload(input: $input) { status jobId titleId titleName }
}
"""

_SET_MONITORED = """
mutation SetMonitored($input: SetTitleMonitoredInput!) {
  setTitleMonitored(input: $input) { id monitored }
}
"""

_DOWNLOAD_QUEUE = """
query DownloadQueue($titleId: ID) {
  downloadQueue(titleId: $titleId) {
    id titleId episodeId titleName facet state displayState progressPercent
    sizeBytes remainingSeconds queuedAt clientName attentionRequired attentionReason
    importStatus downloadId
  }
}
"""

_CALENDAR = """
query Calendar($startDate: Date!, $endDate: Date!) {
  calendarEpisodes(startDate: $startDate, endDate: $endDate) {
    id titleId titleName titleFacet seasonNumber episodeNumber episodeTitle airDate monitored
  }
}
"""

_WANTED = """
query Wanted($kind: WantedKindValue!, $facet: MediaFacetValue, $limit: Int!, $offset: Int!) {
  wantedItems(wantedKind: $kind, facet: $facet, limit: $limit, offset: $offset) {
    totalCount hasMore
    items { id titleId titleName titleFacet seasonNumber episodeNumber status mediaType }
  }
}
"""

_HEALTH = """
query Health {
  scryerVersion
  systemHealth {
    serviceReady totalTitles monitoredTitles titlesMovie titlesSeries titlesAnime
    indexerStats { indexerName queriesLast24H successfulLast24H failedLast24H }
  }
}
"""

_VERSION = "query Version { scryerVersion }"

_QUALITY_PROFILES = """
query Profiles { qualityProfileSettings { globalProfileId profiles { id name } } }
"""

_ROOT_FOLDERS = """
query RootFolders($facet: MediaFacetValue!) { rootFolders(facet: $facet) { path isDefault } }
"""

_LIBRARIES = """
query Libraries($facet: MediaFacetValue) {
  libraries(facet: $facet) { id facet name slug isDefault qualityProfileId roots { path } }
}
"""

_TRIGGER_JOB = "mutation TriggerJob($jobKey: JobKeyValue!) { triggerJob(jobKey: $jobKey) { id } }"


def _int_or_none(value: Any) -> Optional[int]:
    """Scryer returns season/episode numbers as strings ("2"); coerce safely."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class ScryerClient(BaseAPIClient):
    """Async GraphQL client for Scryer with automatic re-login."""

    _PROFILE_CACHE_TTL = 600.0

    def __init__(self, base_url: str, username: str, password: str, timeout: Optional[float] = None):
        # BaseAPIClient's `api_key` is the *arr X-Api-Key concept, which Scryer
        # does not use — pass an empty one and override `_get_headers`.
        super().__init__(base_url, "", "scryer")
        self.username = username
        self.password = password
        self._timeout_override = timeout
        self._token: Optional[str] = None
        self._token_expires_at: Optional[float] = None  # time.monotonic() deadline

    # ----------------------------------------------------------------- auth
    def _get_headers(self) -> dict[str, str]:
        """Static headers only — Authorization is attached per request because
        the token is refreshed independently of the pooled httpx client."""
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "TG_arr-bot/1.0",
        }

    def _get_http_timeout(self) -> float:
        if self._timeout_override is not None:
            return self._timeout_override
        return super()._get_http_timeout()

    def _token_is_fresh(self) -> bool:
        if not self._token:
            return False
        if self._token_expires_at is None:
            return True
        return time.monotonic() < self._token_expires_at

    async def login(self) -> str:
        """Authenticate and cache the JWT. Raises AuthenticationError on refusal."""
        payload = await self._post_graphql(
            _LOGIN,
            {"input": {"username": self.username, "password": self.password}},
            with_auth=False,
        )
        errors = payload.get("errors")
        if errors:
            # Never echo the credentials or the server's raw error body.
            logger.warning("scryer_login_failed", error_count=len(errors))
            raise AuthenticationError(
                "Не удалось войти в Scryer — проверьте SCRYER_USERNAME / SCRYER_PASSWORD",
                status_code=401,
            )
        login = (payload.get("data") or {}).get("login") or {}
        token = login.get("token")
        if not token:
            raise AuthenticationError("Scryer не вернул токен при входе", status_code=401)

        self._token = token
        self._token_expires_at = self._compute_expiry(login.get("expiresAt"))
        logger.info("scryer_login_ok", expires_at=login.get("expiresAt"))
        return token

    @staticmethod
    def _compute_expiry(expires_at: Any) -> float:
        """Monotonic deadline at which the token should be refreshed."""
        parsed = _parse_dt(expires_at)
        if parsed is None:
            return time.monotonic() + _TOKEN_DEFAULT_TTL_S
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        seconds_left = (parsed - datetime.now(timezone.utc)).total_seconds() - _TOKEN_REFRESH_MARGIN_S
        return time.monotonic() + max(seconds_left, 60.0)

    @staticmethod
    def _is_auth_error(errors: list[dict[str, Any]]) -> bool:
        """Whether a GraphQL `errors` array means "log in again"."""
        for err in errors:
            message = str(err.get("message", "")).lower()
            if any(marker in message for marker in _AUTH_ERROR_MARKERS):
                return True
            extensions = err.get("extensions") or {}
            code = str(extensions.get("code", "")).lower()
            if code in ("unauthenticated", "unauthorized", "forbidden"):
                return True
        return False

    # -------------------------------------------------------------- transport
    async def _post_graphql(
        self,
        query: str,
        variables: Optional[dict[str, Any]] = None,
        *,
        with_auth: bool = True,
        timeout: Optional[float] = None,
    ) -> dict[str, Any]:
        """POST one GraphQL document and return the decoded envelope.

        Transport-level retries/timeouts/logging come from BaseAPIClient; this
        only adds the Authorization header and returns the raw envelope so the
        caller can inspect `errors` itself.
        """
        body: dict[str, Any] = {"query": query}
        if variables is not None:
            body["variables"] = variables
        headers = None
        if with_auth and self._token:
            headers = {"Authorization": f"Bearer {self._token}"}
        result = await self._safe_request(
            "POST", "/graphql", json_data=body, timeout=timeout, headers=headers
        )
        return result if isinstance(result, dict) else {"data": None}

    async def execute(
        self,
        query: str,
        variables: Optional[dict[str, Any]] = None,
        *,
        timeout: Optional[float] = None,
        operation: str = "graphql",
    ) -> dict[str, Any]:
        """Run a GraphQL document and return its `data`, re-logging-in once.

        Raises ScryerGraphQLError when the response carries `errors` — including
        the HTTP-200-with-errors case that would otherwise read as success.
        """
        if not self._token_is_fresh():
            await self.login()

        for attempt in (1, 2):
            try:
                payload = await self._post_graphql(query, variables, timeout=timeout)
            except AuthenticationError:
                # HTTP 401 from the transport layer.
                if attempt == 2:
                    raise
                logger.info("scryer_token_rejected", operation=operation, via="http_401")
                await self.login()
                continue

            errors = payload.get("errors") or []
            if errors and self._is_auth_error(errors):
                if attempt == 2:
                    raise AuthenticationError(
                        "Scryer отклонил токен после повторного входа", status_code=401
                    )
                logger.info("scryer_token_rejected", operation=operation, via="graphql_errors")
                await self.login()
                continue

            if errors:
                messages = "; ".join(str(e.get("message", "?")) for e in errors)[:300]
                logger.warning("scryer_graphql_error", operation=operation, error=messages)
                raise ScryerGraphQLError(f"Scryer вернул ошибку: {messages}", errors)

            return payload.get("data") or {}

        raise ScryerGraphQLError("Scryer: не удалось выполнить запрос")  # pragma: no cover

    # ------------------------------------------------------------- metadata
    def _metadata_to_model(self, item: dict[str, Any], content_type: ContentType):
        """Map a `MetadataSearchItemPayload` onto MovieInfo/SeriesInfo."""
        name = item.get("name") or "?"
        year = item.get("year") or None
        runtime = item.get("runtimeMinutes") or None
        if content_type == ContentType.MOVIE:
            return MovieInfo(
                title=name,
                year=year,
                imdb_id=item.get("imdbId"),
                overview=item.get("overview"),
                poster_url=item.get("posterUrl"),
                runtime=runtime,
                metadata_id=str(item["tvdbId"]) if item.get("tvdbId") else None,
                slug=item.get("slug"),
            )
        return SeriesInfo(
            title=name,
            year=year,
            imdb_id=item.get("imdbId"),
            overview=item.get("overview"),
            poster_url=item.get("posterUrl"),
            runtime=runtime,
            metadata_id=str(item["tvdbId"]) if item.get("tvdbId") else None,
            slug=item.get("slug"),
            facet=content_type.scryer_facet or "SERIES",
        )

    async def search_metadata_multi(
        self, query: str, limit: int = 5, language: str = "ru"
    ) -> dict[ContentType, list]:
        """One round-trip metadata search across movie / series / anime.

        Replaces the old parallel Radarr+Sonarr lookups (and the semaphore /
        circuit-breaker machinery they needed) with a single call.
        """
        data = await self.execute(
            _SEARCH_METADATA_MULTI,
            {"query": query, "limit": limit, "language": language},
            operation="searchMetadataMulti",
        )
        multi = data.get("searchMetadataMulti") or {}
        return {
            ContentType.MOVIE: [
                self._metadata_to_model(i, ContentType.MOVIE) for i in (multi.get("movies") or [])
            ],
            ContentType.SERIES: [
                self._metadata_to_model(i, ContentType.SERIES) for i in (multi.get("series") or [])
            ],
            ContentType.ANIME: [
                self._metadata_to_model(i, ContentType.ANIME) for i in (multi.get("anime") or [])
            ],
        }

    async def search_metadata(
        self,
        query: str,
        content_type: ContentType,
        limit: int = 10,
        language: str = "ru",
        year: Optional[int] = None,
    ) -> list:
        """Metadata search restricted to one facet."""
        facet = content_type.scryer_facet
        if facet is None:
            return []
        data = await self.execute(
            _SEARCH_METADATA,
            {"query": query, "type": facet, "limit": limit, "language": language, "year": year},
            operation="searchMetadata",
        )
        return [self._metadata_to_model(i, content_type) for i in (data.get("searchMetadata") or [])]

    # -------------------------------------------------------------- catalog
    def _title_to_model(self, row: dict[str, Any]):
        """Map a `TitlePayload` onto MovieInfo/SeriesInfo."""
        content_type = ContentType.from_scryer_facet(row.get("facet"))
        externals = {
            str(e.get("source", "")).lower(): str(e.get("value"))
            for e in (row.get("externalIds") or [])
            if e.get("value") is not None
        }
        has_file = bool(row.get("mediaFiles")) or bool(row.get("episodesOwned"))
        common = {
            "title": row.get("name") or "?",
            "year": row.get("year") or None,
            "overview": row.get("overview"),
            "poster_url": row.get("posterUrl"),
            "imdb_id": row.get("imdbId") or externals.get("imdb"),
            "runtime": row.get("runtimeMinutes") or None,
            "scryer_id": row.get("id"),
            "metadata_id": externals.get("tvdb"),
            "slug": row.get("slug"),
            "library_id": row.get("libraryId"),
            "quality_tier": row.get("qualityTier"),
            "current_quality_tier": row.get("currentQualityTier"),
            "monitored": bool(row.get("monitored")),
            "quality_profile_id": row.get("qualityProfileId"),
            "root_folder_path": row.get("rootFolderPath"),
            "has_file": has_file,
        }
        tmdb_id = _int_or_none(externals.get("tmdb")) or 0
        if content_type == ContentType.MOVIE:
            return MovieInfo(tmdb_id=tmdb_id, is_available=has_file, **common)
        return SeriesInfo(
            tvdb_id=_int_or_none(externals.get("tvdb")) or 0,
            tmdb_id=tmdb_id or None,
            facet=row.get("facet") or "SERIES",
            episodes_owned=row.get("episodesOwned") or 0,
            episodes_total=row.get("episodesTotal") or 0,
            **common,
        )

    async def get_titles(
        self,
        content_type: Optional[ContentType] = None,
        query: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list, int, bool]:
        """Page through the catalog. Returns (items, totalCount, hasMore)."""
        facet = content_type.scryer_facet if content_type else None
        data = await self.execute(
            _TITLES,
            {"facet": facet, "query": query, "limit": limit, "offset": offset},
            operation="titles",
        )
        catalog = data.get("titles") or {}
        items = [self._title_to_model(row) for row in (catalog.get("items") or [])]
        return items, catalog.get("totalCount", len(items)), bool(catalog.get("hasMore"))

    async def get_title(self, title_id: str):
        """Fetch one title by id, or None when it no longer exists."""
        data = await self.execute(_TITLE, {"id": title_id}, operation="title")
        row = data.get("title")
        return self._title_to_model(row) if row else None

    async def find_title(
        self, content_type: ContentType, name: str, year: Optional[int] = None
    ):
        """Find an already-cataloged title by name (and year when known).

        Used before adding, so a repeat search doesn't create a duplicate and
        the release list can be fetched against the existing id.
        """
        items, _total, _more = await self.get_titles(content_type, query=name, limit=10)
        if not items:
            return None
        wanted = name.strip().casefold()
        exact = [t for t in items if (t.title or "").strip().casefold() == wanted]
        pool = exact or items
        if year:
            for title in pool:
                if title.year and abs(int(title.year) - int(year)) <= 1:
                    return title
            # A year was requested and nothing matches it — do not fall back to
            # an arbitrary title, the caller would silently grab the wrong one.
            return None
        return pool[0]

    # ------------------------------------------------------------- releases
    def _release_to_model(self, row: dict[str, Any], title_id: str) -> SearchResult:
        """Map an `IndexerSearchResultPayload` onto the bot's SearchResult."""
        parsed = row.get("parsedRelease") or {}
        decision = row.get("qualityProfileDecision") or {}
        episode = parsed.get("episode") or {}
        episode_numbers = [n for n in (episode.get("episodeNumbers") or []) if n is not None]
        scope = self._scope_payload_to_input(row.get("queueScope"))
        title_text = row.get("title") or ""

        # Scryer's parser leaves `videoCodec`/`audio` null for most Russian
        # tracker releases, and the local scoring (plus the subtitle/dub rules)
        # reads those fields — so fall back to title parsing for whatever
        # Scryer didn't report. Scryer's own values always win.
        fallback = parse_quality(title_text)
        fallback_season, fallback_episode = extract_season_episode(title_text)
        quality = QualityInfo(
            resolution=parsed.get("quality") or fallback.resolution,
            source=parsed.get("source") or fallback.source,
            codec=parsed.get("videoCodec") or fallback.codec,
            audio=parsed.get("audio") or fallback.audio,
            subtitle=fallback.subtitle,
            hdr=(
                "HDR"
                if parsed.get("detectedHdr") or parsed.get("isDolbyVision")
                else fallback.hdr
            ),
            is_remux=bool(parsed.get("isRemux")) or fallback.is_remux,
            is_repack=fallback.is_repack,
            is_proper=bool(parsed.get("isProperUpload")) or fallback.is_proper,
        )

        source_kind = (row.get("sourceKind") or "").upper()
        protocol = "usenet" if source_kind.startswith("NZB") else "torrent"
        download_url = row.get("downloadUrl")
        magnet = download_url if (download_url or "").startswith("magnet:") else None

        # `guid` must be stable and unique per release: prefer the infoHash,
        # fall back to the (already unique) title+indexer pair. The download
        # URL is deliberately NOT used — it carries credentials.
        guid = row.get("infoHash") or f"{row.get('source', '?')}::{row.get('title', '?')}"

        return SearchResult(
            guid=guid,
            indexer=row.get("source") or "Unknown",
            title=row.get("title") or "?",
            size=row.get("sizeBytes") or 0,
            seeders=row.get("seeders"),
            leechers=row.get("peers"),
            protocol=protocol,
            download_url=download_url,
            magnet_url=magnet,
            info_url=row.get("link"),
            publish_date=_parse_dt(row.get("publishedAt")),
            quality=quality,
            detected_year=extract_year(title_text),
            # `ParsedEpisodePayload` is {season, episodeNumbers[]} — a season
            # pack is "a season with no specific episodes", which is also how
            # the title parser reports it.
            detected_season=_int_or_none(episode.get("season")) or fallback_season,
            detected_episode=(episode_numbers[0] if episode_numbers else None) or fallback_episode,
            is_season_pack=(
                (episode.get("season") is not None and not episode_numbers)
                or is_season_pack(title_text)
            ),
            scryer_title_id=title_id,
            candidate_token=row.get("candidateToken"),
            queue_scope=scope,
            scryer_score=decision.get("releaseScore"),
            scryer_preference_score=decision.get("preferenceScore"),
            scryer_allowed=decision.get("allowed"),
            block_codes=list(decision.get("blockCodes") or []),
            auto_eligible=bool(row.get("autoEligible")),
            auto_decision_summary=row.get("autoDecisionSummary"),
        )

    @staticmethod
    def _scope_payload_to_input(scope: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        """Translate a `QueueDownloadScopePayload` union member into the
        `QueueDownloadScopeInput` shape needed to queue that same candidate."""
        if not isinstance(scope, dict):
            return None
        kind = scope.get("__typename")
        if kind == "EpisodeScopePayload" and scope.get("episodeId"):
            return {"episode": scope["episodeId"]}
        if kind == "EpisodeSetScopePayload" and scope.get("episodeIds"):
            return {"episodeSet": list(scope["episodeIds"])}
        if kind == "SeriesMovieScopePayload" and scope.get("seriesMovieLinkId"):
            return {"seriesMovie": scope["seriesMovieLinkId"]}
        if kind == "CollectionScopePayload" and scope.get("collectionId"):
            return {"collection": scope["collectionId"]}
        if kind == "TitleScopePayload":
            return {"title": True}
        return None

    async def search_releases(
        self,
        title_id: str,
        season: Optional[int] = None,
        episode: Optional[int] = None,
        limit: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> list[SearchResult]:
        """Ask Scryer to query the indexers for one title.

        Scryer talks to Prowlarr itself and applies the profile + Rego rules,
        so the returned candidates already carry an authoritative verdict.
        """
        payload: dict[str, Any] = {"titleId": title_id}
        if season is not None:
            payload["season"] = str(season)
        if episode is not None:
            payload["episode"] = str(episode)
        if limit is not None:
            payload["limit"] = limit

        t0 = time.monotonic()
        data = await self.execute(
            _SEARCH_RELEASES, {"input": payload}, timeout=timeout, operation="searchReleases"
        )
        rows = data.get("searchReleases") or []
        results = [self._release_to_model(row, title_id) for row in rows]
        allowed = sum(1 for r in results if r.scryer_allowed)
        logger.info(
            "scryer_search_releases",
            title_id=title_id,
            season=season,
            episode=episode,
            count=len(results),
            allowed=allowed,
            elapsed_ms=round((time.monotonic() - t0) * 1000, 1),
        )
        return results

    # ----------------------------------------------------------- add / queue
    @staticmethod
    def _add_title_input(
        content,
        content_type: ContentType,
        *,
        monitored: bool,
        quality_profile_id: Optional[str] = None,
        root_folder_path: Optional[str] = None,
        library_id: Optional[str] = None,
        monitor_type: Optional[str] = None,
    ) -> dict[str, Any]:
        """Build `AddTitleInput` from a MovieInfo/SeriesInfo."""
        external_ids = []
        metadata_id = getattr(content, "metadata_id", None)
        if metadata_id:
            external_ids.append({"source": "tvdb", "value": str(metadata_id)})
        imdb_id = getattr(content, "imdb_id", None)
        if imdb_id:
            external_ids.append({"source": "imdb", "value": str(imdb_id)})
        tmdb_id = getattr(content, "tmdb_id", None)
        if tmdb_id:
            external_ids.append({"source": "tmdb", "value": str(tmdb_id)})

        options: dict[str, Any] = {}
        if quality_profile_id:
            options["qualityProfileId"] = quality_profile_id
        if monitor_type:
            options["monitorType"] = monitor_type

        payload: dict[str, Any] = {
            "name": content.title,
            "facet": content_type.scryer_facet or "MOVIE",
            "monitored": monitored,
            "tags": [],
            "externalIds": external_ids,
        }
        if content.year:
            payload["year"] = int(content.year)
        if getattr(content, "overview", None):
            payload["overview"] = content.overview
        if getattr(content, "runtime", None):
            payload["runtimeMinutes"] = int(content.runtime)
        if library_id:
            payload["libraryId"] = library_id
        if root_folder_path:
            # Scryer resolves the root folder through the library; the path is
            # accepted as a hint on the options object.
            options.setdefault("rootFolderId", root_folder_path)
        if options:
            payload["options"] = options
        return payload

    def _parse_add_result(self, node: dict[str, Any]) -> AddTitleOutcome:
        queued = node.get("queuedDownload")
        return AddTitleOutcome(
            title=self._title_to_model(node["title"]),
            reused_existing=bool(node.get("reusedExistingTitle")),
            download_job_id=node.get("downloadJobId"),
            queued_download=QueueResult(**{
                "status": queued.get("status", "QUEUED"),
                "job_id": queued.get("jobId"),
                "title_id": queued.get("titleId"),
                "title_name": queued.get("titleName"),
            }) if queued else None,
        )

    async def add_title(
        self,
        content,
        content_type: ContentType,
        *,
        monitored: bool = True,
        quality_profile_id: Optional[str] = None,
        root_folder_path: Optional[str] = None,
        library_id: Optional[str] = None,
        monitor_type: Optional[str] = None,
    ) -> AddTitleOutcome:
        """Add a title to the catalog WITHOUT queueing a download.

        Used before an interactive release pick: `searchReleases` needs a
        `titleId`, which only exists once the title is in the catalog.
        """
        payload = self._add_title_input(
            content, content_type, monitored=monitored,
            quality_profile_id=quality_profile_id, root_folder_path=root_folder_path,
            library_id=library_id, monitor_type=monitor_type,
        )
        data = await self.execute(_ADD_TITLE, {"input": payload}, operation="addTitle")
        return self._parse_add_result(data["addTitle"])

    async def add_title_and_queue_download(
        self,
        content,
        content_type: ContentType,
        *,
        monitored: bool = True,
        quality_profile_id: Optional[str] = None,
        root_folder_path: Optional[str] = None,
        library_id: Optional[str] = None,
        monitor_type: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> AddTitleOutcome:
        """Add a title and let Scryer pick + queue the best allowed release."""
        payload = self._add_title_input(
            content, content_type, monitored=monitored,
            quality_profile_id=quality_profile_id, root_folder_path=root_folder_path,
            library_id=library_id, monitor_type=monitor_type,
        )
        data = await self.execute(
            _ADD_TITLE_AND_QUEUE, {"input": payload},
            timeout=timeout, operation="addTitleAndQueueDownload",
        )
        return self._parse_add_result(data["addTitleAndQueueDownload"])

    async def queue_existing_title_download(
        self,
        *,
        title_id: str,
        candidate_token: str,
        scope: Optional[dict[str, Any]] = None,
        replace_in_progress: bool = False,
    ) -> QueueResult:
        """Queue a specific release candidate for a title already in the catalog."""
        payload: dict[str, Any] = {
            "titleId": title_id,
            "candidateToken": candidate_token,
            "scope": scope or {"title": True},
        }
        if replace_in_progress:
            payload["replaceInProgress"] = True
        data = await self.execute(_QUEUE_EXISTING, {"input": payload}, operation="queueExistingTitleDownload")
        node = data.get("queueExistingTitleDownload") or {}
        result = QueueResult(
            status=node.get("status", "QUEUED"),
            job_id=node.get("jobId"),
            title_id=node.get("titleId"),
            title_name=node.get("titleName"),
            conflict=node.get("conflict"),
        )
        logger.info(
            "scryer_queue_download",
            title_id=title_id,
            status=result.status,
            job_id=result.job_id,
            # Never the candidate token itself — it embeds the tracker passkey.
            candidate=f"…{candidate_token[-8:]}" if candidate_token else None,
        )
        return result

    async def set_title_monitored(self, title_id: str, monitored: bool) -> bool:
        """Flip a title's monitored flag."""
        data = await self.execute(
            _SET_MONITORED,
            {"input": {"titleId": title_id, "monitored": monitored}},
            operation="setTitleMonitored",
        )
        node = data.get("setTitleMonitored") or {}
        return bool(node.get("monitored", monitored))

    # ------------------------------------------------------------ downloads
    async def get_download_queue(self, title_id: Optional[str] = None) -> list[ScryerQueueItem]:
        """Current download/import activity known to Scryer."""
        data = await self.execute(_DOWNLOAD_QUEUE, {"titleId": title_id}, operation="downloadQueue")
        items = []
        for row in data.get("downloadQueue") or []:
            items.append(
                ScryerQueueItem(
                    id=row.get("id", "?"),
                    title_id=row.get("titleId"),
                    episode_id=row.get("episodeId"),
                    title_name=row.get("titleName") or "?",
                    content_type=ContentType.from_scryer_facet(row.get("facet")),
                    state=row.get("state") or "UNKNOWN",
                    display_state=row.get("displayState") or "UNKNOWN",
                    progress_percent=row.get("progressPercent") or 0,
                    size_bytes=row.get("sizeBytes"),
                    remaining_seconds=row.get("remainingSeconds"),
                    queued_at=_parse_dt(row.get("queuedAt")),
                    client_name=row.get("clientName"),
                    attention_required=bool(row.get("attentionRequired")),
                    attention_reason=row.get("attentionReason"),
                    import_status=row.get("importStatus"),
                    download_id=row.get("downloadId"),
                )
            )
        return items

    async def get_calendar(self, start_date: str, end_date: str) -> list[ScryerCalendarItem]:
        """Upcoming episodes between two ISO dates (inclusive)."""
        data = await self.execute(
            _CALENDAR, {"startDate": start_date, "endDate": end_date}, operation="calendarEpisodes"
        )
        return [
            ScryerCalendarItem(
                id=row.get("id", "?"),
                title_id=row.get("titleId", "?"),
                title_name=row.get("titleName") or "?",
                content_type=ContentType.from_scryer_facet(row.get("titleFacet")),
                season_number=_int_or_none(row.get("seasonNumber")),
                episode_number=_int_or_none(row.get("episodeNumber")),
                episode_title=row.get("episodeTitle"),
                air_date=row.get("airDate"),
                monitored=bool(row.get("monitored")),
            )
            for row in data.get("calendarEpisodes") or []
        ]

    async def get_wanted(
        self,
        kind: str = "MISSING",
        content_type: Optional[ContentType] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[ScryerWantedItem], int, bool]:
        """Missing / cutoff-upgrade items. Returns (items, totalCount, hasMore)."""
        facet = content_type.scryer_facet if content_type else None
        data = await self.execute(
            _WANTED,
            {"kind": kind, "facet": facet, "limit": limit, "offset": offset},
            operation="wantedItems",
        )
        node = data.get("wantedItems") or {}
        items = [
            ScryerWantedItem(
                id=row.get("id", "?"),
                title_id=row.get("titleId", "?"),
                title_name=row.get("titleName") or "?",
                content_type=ContentType.from_scryer_facet(row.get("titleFacet")),
                season_number=_int_or_none(row.get("seasonNumber")),
                episode_number=_int_or_none(row.get("episodeNumber")),
                status=row.get("status"),
                media_type=row.get("mediaType"),
            )
            for row in node.get("items") or []
        ]
        return items, node.get("totalCount", len(items)), bool(node.get("hasMore"))

    # --------------------------------------------------------------- system
    async def system_health(self) -> ScryerHealth:
        """Service readiness, catalog counters and per-indexer 24h stats."""
        data = await self.execute(_HEALTH, operation="systemHealth")
        node = data.get("systemHealth") or {}
        return ScryerHealth(
            service_ready=bool(node.get("serviceReady")),
            total_titles=node.get("totalTitles") or 0,
            monitored_titles=node.get("monitoredTitles") or 0,
            titles_movie=node.get("titlesMovie") or 0,
            titles_series=node.get("titlesSeries") or 0,
            titles_anime=node.get("titlesAnime") or 0,
            version=data.get("scryerVersion"),
            indexers=[
                IndexerStat(
                    name=stat.get("indexerName") or "?",
                    queries_24h=stat.get("queriesLast24H") or 0,
                    successful_24h=stat.get("successfulLast24H") or 0,
                    failed_24h=stat.get("failedLast24H") or 0,
                )
                for stat in node.get("indexerStats") or []
            ],
        )

    async def check_connection(self) -> tuple[bool, Optional[str], Optional[float]]:
        """Health probe shaped like the other clients: (ok, version, ms)."""
        start = time.monotonic()
        try:
            data = await self.execute(_VERSION, operation="scryerVersion")
            elapsed = (time.monotonic() - start) * 1000
            return True, data.get("scryerVersion"), round(elapsed, 2)
        except Exception as e:
            elapsed = (time.monotonic() - start) * 1000
            logger.warning("health_check_failed", service=self.service_name, error=str(e))
            return False, None, round(elapsed, 2)

    async def get_quality_profiles(self) -> list[QualityProfile]:
        """Quality profiles configured in Scryer (TTL-cached)."""
        return await self._ttl_cached(
            "quality_profiles", self._PROFILE_CACHE_TTL, self._fetch_quality_profiles
        )

    async def _fetch_quality_profiles(self) -> list[QualityProfile]:
        data = await self.execute(_QUALITY_PROFILES, operation="qualityProfileSettings")
        node = data.get("qualityProfileSettings") or {}
        return [
            QualityProfile(id=p["id"], name=p.get("name") or str(p["id"]))
            for p in node.get("profiles") or []
            if p.get("id")
        ]

    async def get_root_folders(self, content_type: ContentType = ContentType.MOVIE) -> list[RootFolder]:
        """Root folders for one facet (TTL-cached per facet)."""
        facet = content_type.scryer_facet or "MOVIE"
        return await self._ttl_cached(
            f"root_folders:{facet}",
            self._PROFILE_CACHE_TTL,
            lambda: self._fetch_root_folders(facet),
        )

    async def _fetch_root_folders(self, facet: str) -> list[RootFolder]:
        data = await self.execute(_ROOT_FOLDERS, {"facet": facet}, operation="rootFolders")
        return [
            RootFolder(id=root_folder_id(row["path"]), path=row["path"], is_default=bool(row.get("isDefault")))
            for row in data.get("rootFolders") or []
            if row.get("path")
        ]

    async def get_libraries(self, content_type: Optional[ContentType] = None) -> list[dict[str, Any]]:
        """Raw library rows (id/facet/name/slug/qualityProfileId/roots)."""
        facet = content_type.scryer_facet if content_type else None
        data = await self.execute(_LIBRARIES, {"facet": facet}, operation="libraries")
        return list(data.get("libraries") or [])

    async def default_library_id(self, content_type: ContentType) -> Optional[str]:
        """Id of the default library for a facet (cached — it never changes)."""
        facet = content_type.scryer_facet
        if facet is None:
            return None

        async def _fetch() -> Optional[str]:
            libraries = await self.get_libraries(content_type)
            for library in libraries:
                if library.get("isDefault"):
                    return library.get("id")
            return libraries[0].get("id") if libraries else None

        return await self._ttl_cached(f"default_library:{facet}", self._PROFILE_CACHE_TTL, _fetch)

    async def trigger_job(self, job_key: str) -> Optional[str]:
        """Kick off a background job (e.g. PROWLARR_SYNC). Returns its run id."""
        data = await self.execute(_TRIGGER_JOB, {"jobKey": job_key}, operation="triggerJob")
        return (data.get("triggerJob") or {}).get("id")
