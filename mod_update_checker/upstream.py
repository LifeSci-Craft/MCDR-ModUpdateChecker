"""The HTTP plumbing shared by the Modrinth and CurseForge clients.

Three things a plugin talking to a public API for a live game server has to get right, and
that are therefore implemented once here rather than twice:

* **It must never hang the server.** Every request carries a timeout, and a check run is
  driven from a worker thread.
* **It must not get the admin rate-limited or banned.** Modrinth publishes a hard limit of
  300 requests per minute per IP and a ``429`` that carries ``X-Ratelimit-Reset``;
  CurseForge rate-limits per key. :class:`RateLimiter` throttles proactively and the retry
  loop honours whatever the server tells us to wait.
* **It must survive a flaky or censored network.** ``429``/``5xx`` and transport errors are
  retried with exponential backoff; a mirror or proxy can be pointed at through the
  configurable base URL / proxy settings, which matters a lot for players reaching
  Modrinth from mainland China.

The module deliberately does not import MCDR: it is directly unit-testable, and the plugin
injects the logger it wants to use.
"""

import threading
import time
from collections import deque
from typing import Any, Callable, Dict, Iterable, Optional, Sequence

import requests

__all__ = [
    "UpstreamError",
    "Unauthorised",
    "NotFound",
    "RateLimited",
    "RateLimiter",
    "HttpClient",
]

#: Statuses worth retrying: transient server-side problems and the explicit throttle.
_RETRY_STATUSES = frozenset((408, 425, 429, 500, 502, 503, 504))

#: The HTTP verbs a JSON client needs; kept as a constant so the retry loop stays small.
_JSON_HEADERS = {"Accept": "application/json"}


class UpstreamError(Exception):
    """A request could not be completed. Carries enough context to explain itself."""

    def __init__(
        self,
        message: str,
        status: Optional[int] = None,
        url: Optional[str] = None,
        attempts: int = 1,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.url = url
        self.attempts = attempts

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.status is not None:
            parts.append("HTTP {}".format(self.status))
        if self.url:
            parts.append(self.url)
        return " | ".join(parts)


class Unauthorised(UpstreamError):
    """401/403 — almost always a missing or rejected API key."""


class NotFound(UpstreamError):
    """404 — the project or file simply does not exist upstream."""


class RateLimited(UpstreamError):
    """429 after the retries were exhausted."""


class RateLimiter:
    """A sliding-window limiter: at most ``per_minute`` grants per rolling minute.

    Thread-safe, because a check run fans out across a small thread pool and each worker
    must not be able to push the total past the limit on its own.
    """

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic) -> None:
        self._per_minute = max(0, int(per_minute))
        self._clock = clock
        self._grants: "deque[float]" = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Block until another request may be made. A no-op when disabled."""
        if self._per_minute <= 0:
            return
        while True:
            with self._lock:
                now = self._clock()
                while self._grants and now - self._grants[0] >= 60.0:
                    self._grants.popleft()
                if len(self._grants) < self._per_minute:
                    self._grants.append(now)
                    return
                wait = 60.0 - (now - self._grants[0])
            # Sleep outside the lock so other threads can still make progress.
            time.sleep(max(0.01, min(wait, 5.0)))


class HttpClient:
    """A small JSON-over-HTTP client with retries, throttling and a descriptive agent."""

    def __init__(
        self,
        user_agent: str,
        timeout: float = 20.0,
        retries: int = 3,
        rate_limiter: Optional[RateLimiter] = None,
        logger: Optional[Any] = None,
        proxies: Optional[Dict[str, str]] = None,
        backoff_base: float = 0.6,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.timeout = float(timeout)
        self.retries = max(0, int(retries))
        self.rate_limiter = rate_limiter
        self.logger = logger
        self.backoff_base = float(backoff_base)
        self._session = session or requests.Session()
        self._session.headers.update({"User-Agent": user_agent})
        if proxies:
            # An explicit empty value in the config means "do not proxy this host", which
            # is why falsy entries are kept rather than filtered out.
            self._session.proxies.update(proxies)

    # -- internals ---------------------------------------------------------------------

    def _log(self, level: str, message: str) -> None:
        if self.logger is None:
            return
        handler = getattr(self.logger, level, None)
        if handler is not None:
            handler(message)

    @staticmethod
    def _retry_after(response: "requests.Response", attempt: int, base: float) -> float:
        """Seconds to wait before retrying, biased toward what the server asked for."""
        for header in ("Retry-After", "X-Ratelimit-Reset"):
            raw = response.headers.get(header)
            if not raw:
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            # ``X-Ratelimit-Reset`` is an absolute epoch second on Modrinth, while
            # ``Retry-After`` is a delta. Anything absurdly large is the epoch form.
            if value > 1e6:
                value = max(0.0, value - time.time())
            if value >= 0:
                return min(value + 0.25, 60.0)
        return min(base * (2 ** attempt), 20.0)

    def _request(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Any] = None,
        headers: Optional[Dict[str, str]] = None,
        allow_404: bool = False,
    ) -> Optional[Any]:
        """Perform a request and decode the JSON body. ``None`` when 404 was tolerated."""
        merged = dict(_JSON_HEADERS)
        if headers:
            merged.update(headers)

        last_error: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            if self.rate_limiter is not None:
                self.rate_limiter.acquire()
            try:
                response = self._session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=merged,
                    timeout=self.timeout,
                )
            except requests.RequestException as error:
                last_error = error
                if attempt >= self.retries:
                    raise UpstreamError(
                        "{}: {}".format(type(error).__name__, error),
                        url=url,
                        attempts=attempt + 1,
                    ) from error
                wait = min(self.backoff_base * (2 ** attempt), 20.0)
                self._log(
                    "debug",
                    "request to {} failed ({}), retrying in {:.1f}s".format(
                        url, type(error).__name__, wait
                    ),
                )
                time.sleep(wait)
                continue

            status = response.status_code
            if status == 404:
                if allow_404:
                    return None
                raise NotFound("resource not found", status=status, url=url)
            if status in (401, 403):
                raise Unauthorised(
                    (response.text or "").strip()[:200] or "not authorised",
                    status=status,
                    url=url,
                )
            if status in _RETRY_STATUSES and attempt < self.retries:
                wait = self._retry_after(response, attempt, self.backoff_base)
                self._log(
                    "debug",
                    "HTTP {} from {}, retrying in {:.1f}s".format(status, url, wait),
                )
                time.sleep(wait)
                continue
            if status == 429:
                raise RateLimited("rate limited", status=status, url=url, attempts=attempt + 1)
            if status >= 400:
                raise UpstreamError(
                    (response.text or "").strip()[:200] or "request failed",
                    status=status,
                    url=url,
                    attempts=attempt + 1,
                )

            try:
                return response.json()
            except ValueError as error:
                raise UpstreamError(
                    "the response was not JSON ({}: {})".format(
                        type(error).__name__, error
                    ),
                    status=status,
                    url=url,
                ) from error

        raise UpstreamError(
            "{}".format(last_error or "request failed"), url=url, attempts=self.retries + 1
        )

    # -- public ------------------------------------------------------------------------

    def get_json(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        allow_404: bool = False,
    ) -> Optional[Any]:
        return self._request("GET", url, params=params, headers=headers, allow_404=allow_404)

    def post_json(
        self,
        url: str,
        json_body: Any,
        headers: Optional[Dict[str, str]] = None,
        allow_404: bool = False,
    ) -> Optional[Any]:
        return self._request(
            "POST", url, json_body=json_body, headers=headers, allow_404=allow_404
        )

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:  # noqa: BLE001 - closing must never raise into the caller
            pass
