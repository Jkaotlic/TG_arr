"""Base HTTP client with retry logic and error handling."""

import asyncio
import json
import time
from typing import Any, Optional

import httpx
import structlog
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from bot.config import Settings, get_settings
from bot.models import QualityProfile, RootFolder, SearchResult
from bot.services.release_parser import parse_quality_name

logger = structlog.get_logger()


def _log_before_sleep(retry_state: RetryCallState) -> None:
    """OBS-14: tenacity retries silently by default — log each retried
    attempt with its number so "1 timeout out of 3, recovered" is
    distinguishable in prod logs from "all 3 attempts failed" (the latter
    additionally gets a WARNING from the _safe_request wrapper once retries
    are exhausted and reraise=True surfaces the last exception).
    """
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    instance = retry_state.args[0] if retry_state.args else None
    service = getattr(instance, "service_name", "unknown")
    logger.warning(
        "request_retry_attempt",
        service=service,
        attempt=retry_state.attempt_number,
        error=str(exc) if exc else None,
    )


class APIError(Exception):
    """Base exception for API errors."""

    def __init__(self, message: str, status_code: Optional[int] = None, response_body: Optional[str] = None):
        self.message = message
        self.status_code = status_code
        self.response_body = response_body
        super().__init__(self.message)


class ServiceConnectionError(APIError):
    """Connection error to the service."""

    pass


class AuthenticationError(APIError):
    """Authentication error (invalid API key)."""

    pass


class NotFoundError(APIError):
    """Resource not found error."""

    pass


class RetryableAPIError(APIError):
    """Transient API error that should be retried (429, 503, 504)."""

    pass


class BaseAPIClient:
    """Base async HTTP client with retry logic."""

    # PERF-07: profiles/root-folders change only when the user edits *arr
    # settings — polling them on every grab/settings-menu open is a wasted
    # RTT on rpie4. 600s (10 min) balances freshness vs. round-trips.
    _PROFILE_CACHE_TTL = 600.0

    def __init__(self, base_url: str, api_key: str, service_name: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.service_name = service_name
        self._client: Optional[httpx.AsyncClient] = None
        self._client_lock = asyncio.Lock()
        # LOGIC-07: lazy settings lookup — don't touch env at import/construction time
        # so tests can instantiate clients without a fully configured environment.
        self._settings: Optional["Settings"] = None
        self._ttl_cache: dict[str, tuple[float, Any]] = {}
        self._ttl_cache_lock = asyncio.Lock()

    def _get_http_timeout(self) -> float:
        if self._settings is None:
            self._settings = get_settings()
        return self._settings.http_timeout

    async def _ttl_cached(self, key: str, ttl: float, fetch):
        """Return a cached value for `key` if fetched within the last `ttl`
        seconds, otherwise call the async `fetch()` and cache the result.

        Uses time.monotonic() (immune to wall-clock adjustments) and a lock
        so concurrent callers during a cold cache don't fan out N identical
        requests.
        """
        async with self._ttl_cache_lock:
            cached = self._ttl_cache.get(key)
            now = time.monotonic()
            if cached is not None and (now - cached[0]) < ttl:
                return cached[1]
            value = await fetch()
            self._ttl_cache[key] = (now, value)
            return value

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        async with self._client_lock:
            if self._client is None or self._client.is_closed:
                # PERF-07: keepalive_expiry=300s avoids paying TCP/TLS handshake
                # again on rpie4 ↔ VPS for every search after a quiet minute.
                # Trim defaults — we only talk to one server per client.
                self._client = httpx.AsyncClient(
                    base_url=self.base_url,
                    headers=self._get_headers(),
                    timeout=httpx.Timeout(self._get_http_timeout()),
                    limits=httpx.Limits(
                        max_keepalive_connections=4,
                        max_connections=10,
                        keepalive_expiry=300.0,
                    ),
                )
        return self._client

    def _get_headers(self) -> dict[str, str]:
        """Get default headers. Override in subclasses if needed."""
        return {
            "X-Api-Key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "TG_arr-bot/1.0",
        }

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    # BUG-28 / PERF-09: 3 attempts (1 + 2 retries) for slow Wi-Fi → VPS on rpie4.
    # Retry only on transient *network* errors and 429 (server-asked).
    # 5xx is no longer retried here: Prowlarr/Radarr 502/504 usually mean the
    # downstream is overloaded — retrying compounds the wait. Surface the error
    # so the user can retry manually.
    @retry(
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.ConnectError, RetryableAPIError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=4),
        reraise=True,
        before_sleep=_log_before_sleep,
    )
    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[dict[str, Any]] = None,
        json_data: Optional[dict[str, Any] | list[Any]] = None,
        timeout: Optional[float] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> dict[str, Any] | list[Any]:
        """Make HTTP request with retry logic.

        ``headers`` are merged on top of the pooled client's defaults for this
        one call — needed by ScryerClient, whose bearer token is refreshed
        independently of the (long-lived) httpx client.
        """
        client = await self._get_client()
        url = endpoint if endpoint.startswith("/") else f"/{endpoint}"

        log = logger.bind(
            service=self.service_name,
            method=method,
            endpoint=endpoint,
        )

        start_time = time.monotonic()

        try:
            response = await client.request(
                method=method,
                url=url,
                params=params,
                json=json_data,
                timeout=timeout,
                headers=headers,
            )
            elapsed = (time.monotonic() - start_time) * 1000
            elapsed_rounded = round(elapsed, 2)

            log.debug(
                "API request completed",
                status_code=response.status_code,
                elapsed_ms=elapsed_rounded,
            )
            # OBS-16: surface slow API calls to INFO so latency-distribution can
            # be reconstructed from prod logs without flipping LOG_LEVEL=DEBUG.
            if elapsed > 2000:
                log.warning(
                    "slow_api_call",
                    status_code=response.status_code,
                    elapsed_ms=elapsed_rounded,
                )

            if response.status_code == 401:
                raise AuthenticationError(
                    f"Ошибка авторизации в {self.service_name}. Проверьте API ключ.",
                    status_code=401,
                )

            if response.status_code == 404:
                raise NotFoundError(
                    f"Ресурс не найден: {endpoint}",
                    status_code=404,
                )

            # PERF-09: only 429 is retried (server asks to back off). 5xx fails
            # immediately so the user can react instead of waiting another cycle.
            if response.status_code == 429:
                log.warning("Rate limited, will retry", status_code=429)
                raise RetryableAPIError(
                    f"{self.service_name} ограничивает запросы (429)",
                    status_code=429,
                )
            if response.status_code in (500, 502, 503, 504):
                log.warning("Server error", status_code=response.status_code)
                raise APIError(
                    f"{self.service_name} временно недоступен ({response.status_code})",
                    status_code=response.status_code,
                )

            if response.status_code >= 400:
                response_text = response.text[:500] if response.text else ""
                raise APIError(
                    f"Ошибка {self.service_name}: {response.status_code}",
                    status_code=response.status_code,
                    response_body=response_text,
                )

            if response.status_code == 204:
                return {}

            # A 200 with a non-JSON body happens (a proxy's HTML error page, an
            # empty body). Surfacing a raw JSONDecodeError from here tells the
            # caller nothing — an empty payload lets the domain layer report
            # "no data" the same way it would for `{}`. `_post_no_retry` and the
            # qBittorrent client have always done this; this path had not.
            try:
                return response.json()
            except (json.JSONDecodeError, ValueError):
                log.warning(
                    "non_json_response",
                    status_code=response.status_code,
                    body_prefix=(response.text or "")[:120],
                )
                return {}

        except httpx.TimeoutException:
            log.warning("Request timeout, will retry if attempts remain")
            raise  # Let tenacity handle retries

        except httpx.ConnectError:
            log.warning("Connection error, will retry if attempts remain")
            raise  # Let tenacity handle retries

    async def _safe_request(
        self,
        method: str,
        endpoint: str,
        params: Optional[dict[str, Any]] = None,
        json_data: Optional[dict[str, Any] | list[Any]] = None,
        timeout: Optional[float] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> dict[str, Any] | list[Any]:
        """Wrap _request to convert transport errors after retries are exhausted."""
        try:
            return await self._request(
                method, endpoint, params=params, json_data=json_data, timeout=timeout, headers=headers,
            )
        except httpx.TimeoutException as e:
            # OBS-14: tenacity's `before_sleep` already logged each retried
            # attempt — this WARNING marks that all attempts were exhausted
            # (vs. "1 timeout out of 3, recovered", which never reaches here).
            logger.warning(
                "request_retries_exhausted",
                service=self.service_name,
                method=method,
                endpoint=endpoint,
                error=str(e),
            )
            raise ServiceConnectionError(f"Таймаут соединения с {self.service_name}") from e
        except httpx.ConnectError as e:
            logger.warning(
                "request_retries_exhausted",
                service=self.service_name,
                method=method,
                endpoint=endpoint,
                error=str(e),
            )
            raise ServiceConnectionError(f"Не удалось подключиться к {self.service_name} ({self.base_url})") from e
        except RetryableAPIError as e:
            logger.warning(
                "request_retries_exhausted",
                service=self.service_name,
                method=method,
                endpoint=endpoint,
                error=str(e),
            )
            raise

    async def get(
        self,
        endpoint: str,
        params: Optional[dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> dict[str, Any] | list[Any]:
        """HTTP GET request."""
        return await self._safe_request("GET", endpoint, params=params, timeout=timeout)

    async def post(
        self,
        endpoint: str,
        json_data: Optional[dict[str, Any] | list[Any]] = None,
        params: Optional[dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> dict[str, Any] | list[Any]:
        """HTTP POST request."""
        return await self._safe_request("POST", endpoint, params=params, json_data=json_data, timeout=timeout)

    # DEAD-11: no HTTP DELETE method — removed (zero callers; Radarr/Sonarr/
    # Lidarr/Scryer client methods never delete resources, and qBittorrent
    # has its own dedicated `delete()` on QBittorrentClient, unrelated to
    # this base HTTP client).

    async def _post_no_retry(
        self,
        endpoint: str,
        json_data: dict[str, Any] | list[Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
        headers: Optional[dict[str, str]] = None,
    ) -> dict[str, Any] | list[Any]:
        """POST without retry — for non-idempotent operations like grab/push.

        ``headers`` are merged over the pooled client's defaults for this one
        call, same as `_request` — ScryerClient needs it to attach a bearer
        token that is refreshed independently of the httpx client.
        """
        client = await self._get_client()
        url = endpoint if endpoint.startswith("/") else f"/{endpoint}"
        log = logger.bind(
            service=self.service_name,
            method="POST",
            endpoint=endpoint,
        )
        start_time = time.monotonic()
        try:
            response = await client.request(
                method="POST", url=url, params=params, json=json_data,
                timeout=timeout, headers=headers,
            )
            # OBS-02: mirror _request — time the call and surface slow ones to
            # WARNING so push/grab latency is reconstructable from prod logs.
            elapsed = (time.monotonic() - start_time) * 1000
            elapsed_rounded = round(elapsed, 2)
            log.debug(
                "API request completed",
                status_code=response.status_code,
                elapsed_ms=elapsed_rounded,
            )
            if elapsed > 2000:
                log.warning(
                    "slow_api_call",
                    status_code=response.status_code,
                    elapsed_ms=elapsed_rounded,
                )
            if response.status_code == 401:
                raise AuthenticationError(f"Ошибка авторизации в {self.service_name}", status_code=401)
            if response.status_code >= 400:
                raise APIError(f"Ошибка {self.service_name}: {response.status_code}", status_code=response.status_code)
            if response.status_code == 204:
                return {}
            try:
                return response.json()
            except (json.JSONDecodeError, ValueError):
                return {"raw": response.text}
        except httpx.TimeoutException:
            raise ServiceConnectionError(f"Таймаут соединения с {self.service_name}")
        except httpx.ConnectError:
            raise ServiceConnectionError(f"Не удалось подключиться к {self.service_name} ({self.base_url})")

    async def check_connection(self) -> tuple[bool, Optional[str], Optional[float]]:
        """Check if service is available. Returns (available, version, response_time_ms)."""
        start_time = time.monotonic()
        try:
            result = await self.get("/api/v1/system/status")
            elapsed = (time.monotonic() - start_time) * 1000
            version = result.get("version") if isinstance(result, dict) else None
            return True, version, round(elapsed, 2)
        except Exception as e:
            elapsed = (time.monotonic() - start_time) * 1000
            logger.warning("health_check_failed", service=self.service_name, error=str(e))
            return False, None, round(elapsed, 2)


class ArrBaseClient(BaseAPIClient):
    """Common base for the Radarr/Sonarr/Lidarr *arr clients.

    r5 follow-up (ArrBaseClient dedup): these three clients are almost
    verbatim duplicates for push/profiles/root-folders/health-check, differing
    only in their API version prefix (`/api/v3` for Radarr & Sonarr, `/api/v1`
    for Lidarr). Subclasses set `_api_prefix` and get the shared methods below
    for free; only lookup/add/parse logic (which genuinely differs per media
    type) stays in the subclass.
    """

    #: API version prefix, e.g. "/api/v3" or "/api/v1". Must be set by subclasses.
    _api_prefix: str = "/api/v3"

    async def check_connection(self) -> tuple[bool, Optional[str], Optional[float]]:
        """Check if service is available. Returns (available, version, response_time_ms)."""
        start_time = time.monotonic()
        try:
            result = await self.get(f"{self._api_prefix}/system/status")
            elapsed = (time.monotonic() - start_time) * 1000
            version = result.get("version") if isinstance(result, dict) else None
            return True, version, round(elapsed, 2)
        except Exception as e:
            elapsed = (time.monotonic() - start_time) * 1000
            logger.warning("health_check_failed", service=self.service_name, error=str(e))
            return False, None, round(elapsed, 2)

    async def grab_release(self, guid: str, indexer_id: int) -> bool:
        """Grab a release *arr itself offered.

        Rollback 2026-08-10 (Task 10): the native path. The release came
        from *arr's own interactive search (`_get_releases`), so *arr
        already has it in its release cache and can act on the guid alone.
        *arr resolves Prowlarr's `301 -> magnet` redirect itself, hands the
        torrent to its own download client and imports the result — exactly
        the capability whose absence killed the previous (Scryer) backend,
        which could not expand that redirect itself. Not retried:
        `_post_no_retry` is used deliberately — grabbing twice would queue
        the release twice.
        """
        await self._post_no_retry(
            f"{self._api_prefix}/release",
            json_data={"guid": guid, "indexerId": indexer_id},
        )
        return True

    async def push_release(
        self,
        title: str,
        download_url: str,
        protocol: str = "torrent",
        publish_date: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Push a release to the *arr service for processing.

        Args:
            title: Release title
            download_url: Download URL
            protocol: Protocol (torrent or usenet)
            publish_date: Publication date ISO string

        Returns:
            Push result
        """
        payload: dict[str, Any] = {
            "title": title,
            "downloadUrl": download_url,
            "protocol": protocol.capitalize(),
        }

        if publish_date:
            payload["publishDate"] = publish_date

        result = await self._post_no_retry(f"{self._api_prefix}/release/push", json_data=payload)
        # BUG-01: POST /release/push returns List<ReleaseResource>, not a
        # single object — unwrap it so callers can read `approved`.
        if isinstance(result, list):
            return result[0] if result and isinstance(result[0], dict) else {}
        return result if isinstance(result, dict) else {}

    async def get_quality_profiles(self) -> list[QualityProfile]:
        """Get all quality profiles (PERF-07: cached for _PROFILE_CACHE_TTL)."""
        return await self._ttl_cached(
            "quality_profiles", self._PROFILE_CACHE_TTL, self._fetch_quality_profiles,
        )

    async def _fetch_quality_profiles(self) -> list[QualityProfile]:
        results = await self.get(f"{self._api_prefix}/qualityprofile")
        profiles = []
        if isinstance(results, list):
            for item in results:
                try:
                    profiles.append(QualityProfile(
                        id=item["id"],
                        name=item["name"],
                    ))
                except (KeyError, TypeError) as e:
                    logger.warning("Skipping malformed profile", error=str(e))
        return profiles

    async def get_root_folders(self) -> list[RootFolder]:
        """Get all root folders (PERF-07: cached for _PROFILE_CACHE_TTL)."""
        return await self._ttl_cached(
            "root_folders", self._PROFILE_CACHE_TTL, self._fetch_root_folders,
        )

    async def _fetch_root_folders(self) -> list[RootFolder]:
        results = await self.get(f"{self._api_prefix}/rootfolder")
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

    async def _get_wanted(self, page_size: int = 50) -> list[dict[str, Any]]:
        """Read the "missing" list. Rollback 2026-08-10: /wanted lived on
        Scryer's catalog query before; *arr paginates it instead.

        No `resource` parameter: Radarr and Sonarr hit the identical
        `{prefix}/wanted/missing` endpoint — only the record shape in the
        response differs, which callers parse themselves. (Fix round 1,
        2026-08-10 review: the plan's own brief was self-contradictory here —
        prose said no resource parameter, its code sample had an unused one.)
        """
        result = await self.get(
            f"{self._api_prefix}/wanted/missing",
            params={"pageSize": page_size, "sortKey": "title", "sortDirection": "ascending"},
        )
        if isinstance(result, dict):
            records = result.get("records", [])
            return records if isinstance(records, list) else []
        return []

    async def _set_monitored(self, resource: str, resource_id: int, monitored: bool) -> bool:
        """Flip the monitored flag, preserving every other field.

        *arr's PUT replaces the whole resource, so the current one is fetched
        first — a partial body silently wipes profile and path.
        """
        current = await self.get(f"{self._api_prefix}/{resource}/{resource_id}")
        if not isinstance(current, dict):
            return False
        current["monitored"] = monitored
        # Fix round 1 (2026-08-10 review): go through _safe_request, not
        # _request directly — _safe_request is what converts a timeout/connect
        # error surviving tenacity's retries into the domain
        # ServiceConnectionError every other public method raises. Calling
        # _request bare here leaked a raw httpx exception instead.
        await self._safe_request(
            "PUT", f"{self._api_prefix}/{resource}/{resource_id}", json_data=current,
        )
        return True

    async def _delete_resource(
        self, resource: str, resource_id: int, delete_files: bool = False,
    ) -> bool:
        """Remove a catalog entry. `delete_files` defaults to False so a
        catalog cleanup can never take the media with it.
        """
        # Fix round 1 (2026-08-10 review): same _safe_request fix as
        # _set_monitored above. Retrying a DELETE is still safe (it's
        # idempotent — a repeat DELETE 404s rather than double-deleting); the
        # bug was the missing error translation, not the retry.
        await self._safe_request(
            "DELETE",
            f"{self._api_prefix}/{resource}/{resource_id}",
            params={"deleteFiles": delete_files, "addImportListExclusion": False},
        )
        return True

    async def _get_releases(self, params: dict[str, Any]) -> list["SearchResult"]:
        """Interactive search: releases already judged by the user's profile.

        Unlike a raw Prowlarr query this carries *arr's own verdict —
        `customFormatScore`, `rejected` and the human-readable `rejections` —
        so the bot can tell the user WHY a release will not be taken.
        Slow: it fans out to every indexer, hence the search timeout.
        """
        settings = get_settings()
        results = await self.get(
            f"{self._api_prefix}/release",
            params=params,
            timeout=settings.prowlarr_search_timeout,
        )
        if not isinstance(results, list):
            return []

        releases = []
        for item in results:
            try:
                release = self._parse_release(item)
                if release:
                    releases.append(release)
            except Exception as e:
                # Carry guid/title (when the row has them) so "why did release
                # X vanish" is answerable from prod logs instead of just
                # knowing *something* in this batch was malformed. `.get()` is
                # guarded by isinstance since a truly malformed row may not
                # even be a dict — logging the failure must never itself raise.
                guid = item.get("guid") if isinstance(item, dict) else None
                title = item.get("title") if isinstance(item, dict) else None
                logger.warning(
                    "Skipping malformed release", error=str(e), guid=guid, title=title,
                )
        return releases

    def _parse_release(self, item: dict[str, Any]) -> Optional["SearchResult"]:
        """Map one *arr release row onto SearchResult, verdict included."""
        guid = item.get("guid")
        title = item.get("title")
        if not guid or not title:
            return None

        quality_name = ""
        quality_block = item.get("quality")
        if isinstance(quality_block, dict):
            inner = quality_block.get("quality")
            if isinstance(inner, dict):
                quality_name = inner.get("name") or ""

        languages = [
            lang.get("name")
            for lang in item.get("languages") or []
            if isinstance(lang, dict) and lang.get("name")
        ]

        return SearchResult(
            guid=guid,
            indexer_id=item.get("indexerId") or 0,
            title=title,
            download_url=item.get("downloadUrl"),
            magnet_url=item.get("magnetUrl"),
            info_url=item.get("infoUrl"),
            indexer=item.get("indexer") or "Unknown",
            size=item.get("size") or 0,
            seeders=item.get("seeders") or 0,
            leechers=item.get("leechers") or 0,
            protocol=(item.get("protocol") or "torrent").lower(),
            publish_date=item.get("publishDate"),
            quality=parse_quality_name(quality_name),
            custom_format_score=item.get("customFormatScore") or 0,
            rejected=bool(item.get("rejected")) or bool(item.get("temporarilyRejected")),
            rejections=[str(r) for r in (item.get("rejections") or [])],
            languages=languages,
        )
