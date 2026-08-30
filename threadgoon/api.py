"""Rate-limited 4chan JSON API client with conditional caching."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Callable

import requests
from requests import Response
from requests.exceptions import RequestException

from .database import Repository

API_BASE_URL = "https://a.4cdn.org"
MEDIA_BASE_URL = "https://i.4cdn.org"
SOURCE_BASE_URL = "https://boards.4chan.org"


class ApiError(RuntimeError):
    """Base API failure."""


class ApiNotFound(ApiError):
    """A board or thread no longer exists."""


class ApiRateLimited(ApiError):
    """The API refused requests after all retries."""


class ApiCancelled(ApiError):
    """The user cancelled a pending API operation."""


@dataclass(slots=True)
class ApiResult:
    data: Any
    not_modified: bool = False
    stale: bool = False
    last_modified: str | None = None
    etag: str | None = None


class RateLimiter:
    """Serialize metadata requests and keep their start times apart."""

    def __init__(
        self,
        interval: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        if interval < 1.0:
            raise ValueError("API interval must be at least one second")
        self.interval = interval
        self._clock = clock
        self._sleep = sleeper
        self._lock = threading.Lock()
        self._last_request: float | None = None

    def acquire(self, cancel_event: threading.Event | None = None) -> None:
        with self._lock:
            if self._last_request is not None:
                remaining = self.interval - (self._clock() - self._last_request)
                while remaining > 0:
                    if cancel_event and cancel_event.is_set():
                        raise ApiCancelled("Операция отменена")
                    step = min(remaining, 0.1)
                    self._sleep(step)
                    remaining = self.interval - (
                        self._clock() - self._last_request
                    )
            if cancel_event and cancel_event.is_set():
                raise ApiCancelled("Операция отменена")
            self._last_request = self._clock()


class ApiClient:
    """Synchronous client intended to run in Textual worker threads."""

    def __init__(
        self,
        repository: Repository,
        *,
        interval: float = 1.0,
        session: requests.Session | None = None,
        timeout: tuple[float, float] = (10.0, 30.0),
        max_attempts: int = 4,
        limiter: RateLimiter | None = None,
    ):
        self.repository = repository
        self.session = session or requests.Session()
        self.timeout = timeout
        self.max_attempts = max(1, max_attempts)
        self.limiter = limiter or RateLimiter(interval)
        self.logger = logging.getLogger("threadgoon.api")
        self.session.headers.update(
            {
                "User-Agent": "threadgoon/6.0",
                "Accept": "application/json, text/plain, */*",
            }
        )

    def close(self) -> None:
        self.session.close()

    def fetch_boards(
        self, *, cancel_event: threading.Event | None = None
    ) -> ApiResult:
        return self.get_json(
            "boards",
            f"{API_BASE_URL}/boards.json",
            cancel_event=cancel_event,
        )

    def fetch_catalog(
        self, board: str, *, cancel_event: threading.Event | None = None
    ) -> ApiResult:
        return self.get_json(
            f"catalog:{board}",
            f"{API_BASE_URL}/{board}/catalog.json",
            cancel_event=cancel_event,
        )

    def fetch_thread(
        self,
        board: str,
        thread_id: int,
        *,
        cancel_event: threading.Event | None = None,
    ) -> ApiResult:
        return self.get_json(
            f"thread:{board}:{thread_id}",
            f"{API_BASE_URL}/{board}/thread/{thread_id}.json",
            cancel_event=cancel_event,
        )

    def get_json(
        self,
        cache_key: str,
        url: str,
        *,
        cancel_event: threading.Event | None = None,
        allow_stale: bool = True,
    ) -> ApiResult:
        cache = self.repository.get_cache(cache_key)
        headers: dict[str, str] = {}
        if cache and cache.last_modified:
            headers["If-Modified-Since"] = cache.last_modified
        if cache and cache.etag:
            headers["If-None-Match"] = cache.etag

        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            self.limiter.acquire(cancel_event)
            try:
                response = self.session.get(
                    url,
                    headers=headers,
                    timeout=self.timeout,
                )
            except RequestException as exc:
                last_error = exc
                self.logger.warning("Request failed for %s: %s", url, exc)
                if attempt + 1 < self.max_attempts:
                    self._backoff(attempt, None, cancel_event)
                    continue
                break

            try:
                if response.status_code == 304:
                    if not cache:
                        raise ApiError(f"API returned 304 without cache for {url}")
                    self.repository.touch_cache(cache_key)
                    return ApiResult(
                        data=cache.payload,
                        not_modified=True,
                        last_modified=cache.last_modified,
                        etag=cache.etag,
                    )
                if response.status_code == 404:
                    raise ApiNotFound(f"Ресурс недоступен: {url}")
                if response.status_code == 429:
                    last_error = ApiRateLimited(
                        f"API временно ограничил запросы: {url}"
                    )
                    if attempt + 1 < self.max_attempts:
                        self._backoff(attempt, response, cancel_event)
                        continue
                    break
                if response.status_code in {500, 502, 503, 504}:
                    last_error = ApiError(
                        f"API ответил {response.status_code}: {url}"
                    )
                    if attempt + 1 < self.max_attempts:
                        self._backoff(attempt, response, cancel_event)
                        continue
                    break
                response.raise_for_status()
                try:
                    data = response.json()
                except ValueError as exc:
                    raise ApiError(f"Некорректный JSON: {url}") from exc
                last_modified = response.headers.get("Last-Modified")
                etag = response.headers.get("ETag")
                self.repository.save_cache(
                    cache_key,
                    data,
                    last_modified=last_modified,
                    etag=etag,
                )
                return ApiResult(
                    data=data,
                    last_modified=last_modified,
                    etag=etag,
                )
            except ApiNotFound:
                raise
            except RequestException as exc:
                last_error = exc
            finally:
                response.close()

        if allow_stale and cache:
            self.logger.warning("Using stale cache for %s after %s", url, last_error)
            return ApiResult(
                data=cache.payload,
                stale=True,
                last_modified=cache.last_modified,
                etag=cache.etag,
            )
        if isinstance(last_error, ApiRateLimited):
            raise last_error
        raise ApiError(f"Не удалось получить {url}: {last_error}") from last_error

    def _backoff(
        self,
        attempt: int,
        response: Response | None,
        cancel_event: threading.Event | None,
    ) -> None:
        delay = min(2 ** attempt, 30.0)
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            parsed = _parse_retry_after(retry_after)
            if parsed is not None:
                delay = min(max(parsed, delay), 60.0)
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            if cancel_event and cancel_event.is_set():
                raise ApiCancelled("Операция отменена")
            time.sleep(min(0.1, deadline - time.monotonic()))


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            target = parsedate_to_datetime(value)
            return max(0.0, target.timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return None
