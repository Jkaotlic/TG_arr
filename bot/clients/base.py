"""Base HTTP client with retry logic and error handling."""

import asyncio
import contextvars
import json
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, NoReturn, Optional

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
from bot.models import MIN_PACK_EPISODES, QualityProfile, RootFolder, SearchResult
from bot.services.release_parser import enrich_from_title, parse_episode_numbers, parse_quality_name

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


class ServiceUnavailable(ServiceConnectionError):
    """Raised to *background* callers, without touching the network, while a
    client's circuit breaker is open (audit 2026-09-23, NOTIF-08)."""

    pass


#: True inside `background_calls()`. Background pollers (library_watch, the
#: health monitor) set it at their call site; user-triggered handlers never do.
_BACKGROUND: contextvars.ContextVar[bool] = contextvars.ContextVar("tg_arr_background_calls", default=False)


@contextmanager
def background_calls() -> Iterator[None]:
    """Mark every client call made inside this block as a background call.

    Background calls make a single attempt (no tenacity retries), fail fast
    with `ServiceUnavailable` while the client's breaker is open, and do not
    log per-request warnings — the breaker logs the `service_down` /
    `service_up` transitions instead. The flag is a contextvar, so it follows
    the awaited call chain (and tasks spawned from it via `asyncio.gather`).
    """
    token = _BACKGROUND.set(True)
    try:
        yield
    finally:
        _BACKGROUND.reset(token)


def is_background_call() -> bool:
    return _BACKGROUND.get()


class CircuitBreaker:
    """Per-client availability memory (audit 2026-09-23, NOTIF-08).

    Closed until a call fails with a transport error; then open with a
    growing backoff (30 → 60 → 120 → 300 → 600 s, capped). While open:

    - background callers get `ServiceUnavailable` immediately until the
      backoff expires, then exactly ONE background probe goes through
      (half-open) — the first caller takes the slot, the rest keep failing
      fast until the probe resolves (round 2 / D3);
    - interactive callers always get one attempt (no retries), so a user
      pressing a button is never refused on stale information.

    Only transitions are logged: `service_down` on the first failure and
    `service_up` (with `downtime_s`) on the first success after it.
    """

    BACKOFF_S: tuple[int, ...] = (30, 60, 120, 300, 600)

    def __init__(self, service: str, clock: Callable[[], float] = time.monotonic):
        self.service = service
        self._clock = clock
        self._failures = 0
        self._open_until = 0.0
        self._down_since: Optional[float] = None
        #: A half-open background probe is in flight (round 2 / D3).
        self._probing = False
        #: Token of the current probe (fix round 1): a late-finishing old
        #: probe must not free a newer probe's slot.
        self.probe_token = 0

    @property
    def is_open(self) -> bool:
        """Down and still inside the backoff window."""
        return self._down_since is not None and self._clock() < self._open_until

    @property
    def is_down(self) -> bool:
        """The last call failed (open or half-open)."""
        return self._down_since is not None

    def allow(self, *, background: bool) -> bool:
        """May this call go to the network?

        Interactive: always. Background: yes while closed; no while open; in
        half-open only the first caller (it takes the probe slot, released by
        ``record_*`` or ``end_probe``).
        """
        if not background or self._down_since is None:
            return True
        if self.is_open or self._probing:
            return False
        self._probing = True
        self.probe_token += 1
        return True

    def end_probe(self, token: int | None = None) -> None:
        """Free the half-open slot (the probe ended without a verdict).

        With ``token``, only if that probe still holds the slot.
        """
        if token is None or token == self.probe_token:
            self._probing = False

    def record_failure(self, error: str = "") -> None:
        now = self._clock()
        self._probing = False
        self._failures += 1
        if self._down_since is None:
            self._down_since = now
            logger.warning("service_down", service=self.service, error=error)
        backoff = self.BACKOFF_S[min(self._failures, len(self.BACKOFF_S)) - 1]
        self._open_until = now + backoff

    def record_success(self) -> None:
        if self._down_since is not None:
            logger.info(
                "service_up",
                service=self.service,
                downtime_s=round(self._clock() - self._down_since),
            )
        self._failures = 0
        self._open_until = 0.0
        self._down_since = None
        self._probing = False


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
        #: NOTIF-08: shared by every call through `_safe_request`.
        self.breaker = CircuitBreaker(service_name)

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
        """`_request_once` with tenacity retries (interactive calls only)."""
        return await self._request_once(
            method, endpoint, params=params, json_data=json_data, timeout=timeout, headers=headers,
        )

    async def _request_once(
        self,
        method: str,
        endpoint: str,
        params: Optional[dict[str, Any]] = None,
        json_data: Optional[dict[str, Any] | list[Any]] = None,
        timeout: Optional[float] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> dict[str, Any] | list[Any]:
        """Make one HTTP request (retries, if any, are the caller's business).

        ``headers`` are merged on top of the pooled client's defaults for this
        one call — needed by callers whose auth value can change between
        requests independently of the (long-lived, cached-headers) httpx
        client, e.g. `EmbySyncHookClient`'s per-request token header.
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
                log.warning("rate_limited", status_code=429)
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

        # NOTIF-08: these used to say "will retry if attempts remain" even on
        # the last attempt. Whether a retry follows is logged by tenacity's
        # `before_sleep` (request_retry_attempt); exhaustion by `_safe_request`.
        # Background calls stay quiet here — the breaker logs transitions.
        except httpx.TimeoutException:
            if not is_background_call():
                log.debug("request_timeout")
            raise

        except httpx.ConnectError:
            if not is_background_call():
                log.debug("request_connect_error")
            raise

    async def _safe_request(
        self,
        method: str,
        endpoint: str,
        params: Optional[dict[str, Any]] = None,
        json_data: Optional[dict[str, Any] | list[Any]] = None,
        timeout: Optional[float] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> dict[str, Any] | list[Any]:
        """Wrap _request to convert transport errors after retries are exhausted.

        NOTIF-08 (audit 2026-09-23): consults the client's circuit breaker.
        Background calls (inside `background_calls()`) get `ServiceUnavailable`
        without a network attempt while the breaker is open, and otherwise a
        single attempt; interactive calls keep tenacity's retries while the
        service is healthy and fall back to a single attempt while it is down.
        """
        background = is_background_call()
        breaker = self.breaker
        if not breaker.allow(background=background):
            raise ServiceUnavailable(f"{self.service_name} временно недоступен")
        # Round 2 / D3: this call holds the half-open probe slot.
        probe_token = breaker.probe_token if (background and breaker.is_down) else None
        try:
            return await self._send_through_breaker(
                breaker, background, method, endpoint,
                params=params, json_data=json_data, timeout=timeout, headers=headers,
            )
        finally:
            if probe_token is not None:
                breaker.end_probe(probe_token)

    async def _send_through_breaker(
        self,
        breaker: "CircuitBreaker",
        background: bool,
        method: str,
        endpoint: str,
        *,
        params: Optional[dict[str, Any]],
        json_data: Optional[dict[str, Any] | list[Any]],
        timeout: Optional[float],
        headers: Optional[dict[str, str]],
    ) -> dict[str, Any] | list[Any]:
        """One request with the breaker's bookkeeping (see the caller)."""
        send = self._request_once if (background or breaker.is_down) else self._request
        try:
            result = await send(
                method, endpoint, params=params, json_data=json_data, timeout=timeout, headers=headers,
            )
        except (httpx.TimeoutException, httpx.ConnectError, RetryableAPIError) as e:
            breaker.record_failure(str(e))
            if background:
                if isinstance(e, RetryableAPIError):
                    raise
                raise ServiceConnectionError(
                    f"Не удалось подключиться к {self.service_name}"
                ) from e
            self._raise_exhausted(e, method, endpoint)
        except APIError as e:
            if e.status_code is not None and e.status_code >= 500:
                breaker.record_failure(str(e))
            else:
                breaker.record_success()  # it answered: reachable
            raise
        breaker.record_success()
        return result

    def _raise_exhausted(self, error: Exception, method: str, endpoint: str) -> NoReturn:
        """Log that every attempt failed and convert the transport error.

        OBS-14: tenacity's `before_sleep` already logged each retried attempt —
        this WARNING marks that all attempts were exhausted (vs. "1 timeout out
        of 3, recovered", which never reaches here).
        """
        logger.warning(
            "request_retries_exhausted",
            service=self.service_name,
            method=method,
            endpoint=endpoint,
            error=str(error),
        )
        if isinstance(error, httpx.TimeoutException):
            raise ServiceConnectionError(f"Таймаут соединения с {self.service_name}") from error
        if isinstance(error, httpx.ConnectError):
            raise ServiceConnectionError(
                f"Не удалось подключиться к {self.service_name} ({self.base_url})"
            ) from error
        raise error

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
    # Lidarr client methods never delete resources, and qBittorrent
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
        call, same as `_request` — see its docstring for why a per-call
        override is needed.
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
            log_fn = logger.debug if is_background_call() else logger.warning
            log_fn("health_check_failed", service=self.service_name, error=str(e))
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
            log_fn = logger.debug if is_background_call() else logger.warning
            log_fn("health_check_failed", service=self.service_name, error=str(e))
            return False, None, round(elapsed, 2)

    async def grab_release(self, guid: str, indexer_id: int) -> bool:
        """Grab a release *arr itself offered.

        Rollback 2026-08-10 (Task 10): the native path. The release came
        from *arr's own interactive search (`_get_releases`), so *arr
        already has it in its release cache and can act on the guid alone.
        *arr resolves Prowlarr's `301 -> magnet` redirect itself, hands the
        torrent to its own download client and imports the result — exactly
        the capability whose absence killed the previous backend, which
        could not expand that redirect itself. Not retried:
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

    async def find_in_queue(self, release_title: str) -> bool:
        """Whether a release with this title sits in *arr's download queue.

        Audit 2026-09-23 GRAB-14: a grab POST that timed out may still have
        landed. Queue records carry the release title (no guid), so the title
        is the match key, compared case-insensitively. Any error reads as
        "not found" — the caller then reports the failure it already had.
        """
        wanted = release_title.strip().casefold()
        if not wanted:
            return False
        try:
            result = await self.get(f"{self._api_prefix}/queue", params={"pageSize": 200})
        except Exception:
            return False
        records = result.get("records") if isinstance(result, dict) else result
        if not isinstance(records, list):
            return False
        return any(
            isinstance(r, dict) and str(r.get("title") or "").strip().casefold() == wanted
            for r in records
        )

    async def get_queue(self, page_size: int = 200) -> list[dict[str, Any]]:
        """The download queue's records (``[]`` on an unexpected shape).

        Records carry the release ``title``, ``downloadId`` (torrent hash)
        and the tracked-download status/messages; errors propagate.
        """
        result = await self.get(f"{self._api_prefix}/queue", params={"pageSize": page_size})
        records = result.get("records") if isinstance(result, dict) else result
        return [r for r in records if isinstance(r, dict)] if isinstance(records, list) else []

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

    async def _get_wanted(
        self, page_size: int = 50, extra_params: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, Any]]:
        """Read the "missing" list. Rollback 2026-08-10: /wanted lived on
        the previous backend's catalog query before; *arr paginates it instead.

        No `resource` parameter: Radarr and Sonarr hit the identical
        `{prefix}/wanted/missing` endpoint — only the record shape in the
        response differs, which callers parse themselves. (Fix round 1,
        2026-08-10 review: the plan's own brief was self-contradictory here —
        prose said no resource parameter, its code sample had an unused one.)

        `extra_params` (fix round 1, task-13 re-review): lets a subclass add
        query params the shared endpoint accepts but not every caller wants
        — Sonarr sends `includeSeries=true` here (see
        `SonarrClient.get_wanted_episodes`), Radarr sends nothing, since
        Radarr's movie records have no series to embed and the param would
        be meaningless noise on that call.
        """
        params: dict[str, Any] = {
            "pageSize": page_size, "sortKey": "title", "sortDirection": "ascending",
        }
        if extra_params:
            params.update(extra_params)
        result = await self.get(f"{self._api_prefix}/wanted/missing", params=params)
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

    #: Servarr's shared History `eventType` filter is bound from the query
    #: string as the enum's ordinal, not the camelCase name the JSON body
    #: uses for the same field (`"eventType": "downloadFolderImported"` in a
    #: record vs. `?eventType=3` to filter for it) — live-verified against
    #: both Radarr and Sonarr 2026-08-11: sending the string name 400s
    #: ("The value 'downloadFolderImported' is not valid"), sending `3`
    #: returns exactly the downloadFolderImported rows on both services.
    #: `grabbed` (1) — improvements round 2 (E1): the living status message
    #: links a bot grab to its torrent through the grabbed record, whose
    #: `data.guid` is the guid the bot pushed and `downloadId` the hash
    #: (live-verified 2026-09-24).
    _HISTORY_EVENT_TYPE_CODES: dict[str, int] = {"grabbed": 1, "downloadFolderImported": 3}

    async def get_history(
        self, event_type: str = "downloadFolderImported", page_size: int = 50,
        *, include_details: bool = False,
    ) -> list[dict[str, Any]]:
        """Read the *arr history journal, filtered to one event type.

        Used by `LibraryWatcher` to detect media that has actually landed —
        `downloadFolderImported` fires once the file is imported into the
        library, unlike `grabbed` (queued) or the transient download-queue
        state, which can flicker before a final decision.
        """
        try:
            code = self._HISTORY_EVENT_TYPE_CODES[event_type]
        except KeyError:
            raise ValueError(f"Unsupported history eventType filter: {event_type!r}") from None

        params: dict[str, Any] = {
            "eventType": code,
            "pageSize": page_size,
            "sortKey": "date",
            "sortDirection": "descending",
        }
        if include_details:
            # Radarr reads includeMovie, Sonarr includeSeries; each ignores
            # the other's flag. Adds the title and its provider ids.
            params["includeMovie"] = "true"
            params["includeSeries"] = "true"
        result = await self.get(f"{self._api_prefix}/history", params=params)
        if isinstance(result, dict):
            records = result.get("records", [])
            return records if isinstance(records, list) else []
        return []

    async def get_history_since(
        self, since: datetime, event_type: str = "downloadFolderImported",
        *, include_details: bool = False,
    ) -> list[dict[str, Any]]:
        """History records dated at or after `since`, oldest first (unpaged).

        Log review 2026-10-10: `LibraryWatcher` already keeps a per-service
        watermark; asking only for what is newer turned a ~205 KB poll into
        `[]` (live Radarr/Sonarr: same numeric `eventType`, same details flags).
        """
        try:
            code = self._HISTORY_EVENT_TYPE_CODES[event_type]
        except KeyError:
            raise ValueError(f"Unsupported history eventType filter: {event_type!r}") from None

        params: dict[str, Any] = {
            "date": since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "eventType": code,
        }
        if include_details:
            params["includeMovie"] = "true"
            params["includeSeries"] = "true"
        result = await self.get(f"{self._api_prefix}/history/since", params=params)
        return result if isinstance(result, list) else []

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

        custom_formats = [
            cf.get("name")
            for cf in item.get("customFormats") or []
            if isinstance(cf, dict) and cf.get("name")
        ]
        quality_weight = item.get("qualityWeight")

        # Sonarr уже разобрал заголовок сам — это его вердикт, а не вторая копия
        # эвристики бота (Radarr этих полей не отдаёт вовсе, там останется None).
        # 2026-08-12: раньше поля не читались, и на ЭТОМ пути — основном для
        # /series — сезон и признак пака всегда были пустыми, из-за чего пресет
        # мониторинга выбирался по пустоте, а автопоиск не сужался до сезона.
        season_number = item.get("seasonNumber")
        episode_numbers = [n for n in (item.get("episodeNumbers") or []) if isinstance(n, int)]
        # `fullSeason` — вердикт Sonarr о «целом сезоне». Живой замер 2026-08-12
        # (GET /api/v3/parse): «S2E1-8 of 8» он отдаёт как fullSeason=false с
        # восемью episodeNumbers — он не знает, что восемь и есть весь сезон.
        # Для бота такая раздача — пак, тем же порогом, что и на пути Prowlarr.
        # `discography` — музыкальный двойник `fullSeason` у Lidarr (живой
        # замер 2026-08-12): и то и другое значит «в раздаче больше, чем один
        # запрошенный элемент», и оба ведут к одному решению в UI и мониторинге.
        is_season_pack = (
            bool(item.get("fullSeason"))
            or bool(item.get("discography"))
            or len(episode_numbers) >= MIN_PACK_EPISODES
        )

        return SearchResult(
            guid=guid,
            origin="arr",  # fix round 2: renamed from `source` (collided with QualityInfo.source)
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
            # GRAB-10: *arr's name gives resolution/source; codec, HDR/DV,
            # audio and voice-over come from the title where *arr is silent.
            quality=enrich_from_title(parse_quality_name(quality_name), title),
            custom_format_score=item.get("customFormatScore") or 0,
            rejected=bool(item.get("rejected")) or bool(item.get("temporarilyRejected")),
            rejections=[str(r) for r in (item.get("rejections") or [])],
            languages=languages,
            quality_weight=quality_weight if isinstance(quality_weight, int) else None,
            custom_formats=custom_formats,
            detected_season=season_number if isinstance(season_number, int) else None,
            detected_episode=episode_numbers[0] if len(episode_numbers) == 1 else None,
            # Round 2 / D3: Sonarr's own list first, the title as a fallback.
            detected_episodes=sorted(set(episode_numbers)) or parse_episode_numbers(title),
            is_season_pack=is_season_pack,
        )
