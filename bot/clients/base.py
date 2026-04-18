"""Base HTTP client with retry logic and error handling."""

import asyncio
import json
import time
from typing import Any, Optional

import httpx
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from bot.config import get_settings

logger = structlog.get_logger()


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

    def __init__(self, base_url: str, api_key: str, service_name: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.service_name = service_name
        self._client: Optional[httpx.AsyncClient] = None
        self._client_lock = asyncio.Lock()
        # LOGIC-07: lazy settings lookup — don't touch env at import/construction time
        # so tests can instantiate clients without a fully configured environment.
        self._settings: Optional[object] = None

    def _get_http_timeout(self) -> float:
        if self._settings is None:
            self._settings = get_settings()
        return self._settings.http_timeout

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        async with self._client_lock:
            if self._client is None or self._client.is_closed:
                self._client = httpx.AsyncClient(
                    base_url=self.base_url,
                    headers=self._get_headers(),
                    timeout=httpx.Timeout(self._get_http_timeout()),
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

    # BUG-19: shorten backoff so we stay within Telegram's ~15s callback window.
    @retry(
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.ConnectError, RetryableAPIError)),
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1, min=1, max=5),
        reraise=True,
    )
    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[dict[str, Any]] = None,
        json_data: Optional[dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> dict[str, Any] | list[Any]:
        """Make HTTP request with retry logic."""
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
            )
            elapsed = (time.monotonic() - start_time) * 1000

            log.debug(
                "API request completed",
                status_code=response.status_code,
                elapsed_ms=round(elapsed, 2),
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

            # BUG-04: treat transient 5xx (incl. 500/502 from reverse-proxies) as retryable.
            if response.status_code in (429, 500, 502, 503, 504):
                log.warning("Retryable HTTP error", status_code=response.status_code)
                raise RetryableAPIError(
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

            return response.json()

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
        json_data: Optional[dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> dict[str, Any] | list[Any]:
        """Wrap _request to convert transport errors after retries are exhausted."""
        try:
            return await self._request(method, endpoint, params=params, json_data=json_data, timeout=timeout)
        except httpx.TimeoutException as e:
            raise ServiceConnectionError(f"Таймаут соединения с {self.service_name}") from e
        except httpx.ConnectError as e:
            raise ServiceConnectionError(f"Не удалось подключиться к {self.service_name} ({self.base_url})") from e

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
        json_data: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> dict[str, Any] | list[Any]:
        """HTTP POST request."""
        return await self._safe_request("POST", endpoint, params=params, json_data=json_data, timeout=timeout)

    async def delete(
        self,
        endpoint: str,
        params: Optional[dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> dict[str, Any] | list[Any]:
        """HTTP DELETE request."""
        return await self._safe_request("DELETE", endpoint, params=params, timeout=timeout)

    async def _post_no_retry(
        self,
        endpoint: str,
        json_data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any] | list[Any]:
        """POST without retry — for non-idempotent operations like grab/push."""
        client = await self._get_client()
        url = endpoint if endpoint.startswith("/") else f"/{endpoint}"
        try:
            response = await client.request(
                method="POST", url=url, params=params, json=json_data, timeout=timeout,
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
