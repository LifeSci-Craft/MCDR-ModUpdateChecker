"""The HTTP plumbing used to talk to Modrinth.

Three things a plugin talking to a public API for a live game server has to get right, and
that are therefore implemented once here rather than twice:

* **It must never hang the server.** Every request carries a timeout, and a check run is
  driven from a worker thread.
* **It must not get the admin rate-limited or banned.** Modrinth publishes a hard limit of
  300 requests per minute per IP and a ``429`` that carries ``X-Ratelimit-Reset``.
  :class:`RateLimiter` throttles proactively and the retry loop honours whatever the server
  tells us to wait.
* **It must survive a flaky or censored network.** ``429``/``5xx`` and transport errors are
  retried with exponential backoff, and the base URL is configurable, which matters a lot for
  players reaching Modrinth from mainland China.

The module deliberately does not import MCDR: it is directly unit-testable, and the plugin
injects the logger it wants to use.
"""

import json
import threading
import time
from collections import deque
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlparse

import requests

__all__ = [
    "UpstreamError",
    "Unauthorised",
    "NotFound",
    "RateLimited",
    "RateLimiter",
    "HttpClient",
    "DEFAULT_JSON_MAX_BYTES",
]

#: Statuses worth retrying: transient server-side problems and the explicit throttle.
_RETRY_STATUSES = frozenset((408, 425, 429, 500, 502, 503, 504))

#: The HTTP verbs a JSON client needs; kept as a constant so the retry loop stays small.
_JSON_HEADERS = {"Accept": "application/json"}

#: Largest JSON body this client will read. Every response used here is a list of versions or
#: projects — kilobytes, and never more than a few hundred — but the response is upstream data
#: and ``project_versions`` without filters can return a large project's entire history. The
#: download path has had a size bound from the start; this is the same bound for the other half.
DEFAULT_JSON_MAX_BYTES = 8 * 1024 * 1024

#: How long a download may take *in total*, as a multiple of the configured per-read timeout.
#: The timeout alone cannot bound a transfer: it is a per-*read* limit, so a server that sends
#: one byte just before each one keeps the connection alive forever. Multiplying by the declared
#: size gives a bound that scales with the amount of data expected, and one that a stalled or
#: dripping transfer cannot extend by staying alive.
_DEADLINE_FRACTION = 0.25

#: Redirects followed at most, per session. ``requests`` defaults to 30 and will happily follow
#: a chain to another host; a download URL comes from an upstream JSON document, so the target is
#: upstream data and deserves a smaller budget than "whatever the library thinks is fine".
_MAX_REDIRECTS = 10


class UpstreamError(Exception):
    """A request could not be completed. Carries enough context to explain itself."""

    def __init__(
        self,
        message: str,
        status: Optional[int] = None,
        url: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.url = url

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


class _BodyInterrupted(UpstreamError):
    """The response body broke while it was being read — worth another attempt.

    Private, and a subclass of :class:`UpstreamError` on purpose: a caller that only knows
    "the upstream failed" (which is every caller) keeps working unchanged, while
    :meth:`HttpClient._request` can tell this one apart and retry it. It is never raised out
    of the client: once the attempts are used up the retry loop re-raises it, which reads to
    the caller exactly like the ``UpstreamError`` it is.
    """


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
        backoff_base: float = 0.6,
        session: Optional[requests.Session] = None,
        connect_timeout: Optional[float] = None,
        read_timeout: Optional[float] = None,
        json_max_bytes: int = DEFAULT_JSON_MAX_BYTES,
    ) -> None:
        self.timeout = float(timeout)
        #: ``(connect, read)``, which is what ``requests`` accepts. The two are separate because
        #: they measure different things and a CDN is usually fast to reach and slow to stream:
        #: one scalar for both means the slower half dictates the tolerance for the faster one.
        #: ``timeout`` remains the single knob the config exposes, and the default split reads as
        #: "allow the connection a little longer, but never more than the configured value".
        self.connect_timeout = float(
            self.timeout if connect_timeout is None else connect_timeout
        )
        self.read_timeout = float(self.timeout if read_timeout is None else read_timeout)
        self.retries = max(0, int(retries))
        self.rate_limiter = rate_limiter
        self.logger = logger
        self.backoff_base = float(backoff_base)
        self.json_max_bytes = max(1, int(json_max_bytes))
        self.user_agent = user_agent
        #: How a session is made. A caller may inject one — the tests do, to watch the requests
        #: — and that injected session serves the thread that made the client; any other thread
        #: builds its own, because a session holds mutable state and ``requests`` documents it as
        #: not thread-safe while this client is handed to pools of up to eight workers.
        if session is None:
            self._session_factory: Callable[[], requests.Session] = requests.Session
        elif callable(session) and not hasattr(session, "request"):
            self._session_factory = session
        else:
            self._session_factory = lambda: session  # noqa: E731 - one injected instance
        self._thread_local = threading.local()
        self._sessions: Dict[int, requests.Session] = {}
        self._sessions_lock = threading.Lock()

    # -- internals ---------------------------------------------------------------------

    def _new_session(self) -> requests.Session:
        """A session with this client's agent and redirect budget."""
        session = self._session_factory()
        session.headers["User-Agent"] = self.user_agent
        session.max_redirects = _MAX_REDIRECTS
        return session

    @property
    def _session(self) -> requests.Session:
        """This thread's session, created on first use.

        One session per thread rather than one per client. ``requests``' own FAQ lists ``Session``
        as not thread-safe: it carries a cookie jar, a redirect history and adapter state that
        concurrent requests would share. The connection pools underneath are thread-safe, so the
        failure mode is rare and timing-dependent — the kind of bug that gets blamed on the
        network for months. A request-per-session would be the other safe option and is
        deliberately not taken: it loses connection reuse, which is most of what a session is for.
        """
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = self._new_session()
            self._thread_local.session = session
            with self._sessions_lock:
                self._sessions[threading.get_ident()] = session
        return session

    @property
    def timeouts(self) -> Tuple[float, float]:
        """The ``(connect, read)`` tuple handed to ``requests``."""
        return (self.connect_timeout, self.read_timeout)

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
                    timeout=self.timeouts,
                    stream=True,
                )
            except requests.RequestException as error:
                last_error = error
                if attempt >= self.retries:
                    raise UpstreamError(
                        "{}: {}".format(type(error).__name__, error),
                        url=url,
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
                response.close()
                if allow_404:
                    return None
                raise NotFound("resource not found", status=status, url=url)
            if status in (401, 403):
                text = self._read_body(response, url, status)
                response.close()
                raise Unauthorised(
                    (text or "").strip()[:200] or "not authorised",
                    status=status,
                    url=url,
                )
            if status in _RETRY_STATUSES and attempt < self.retries:
                wait = self._retry_after(response, attempt, self.backoff_base)
                response.close()
                self._log(
                    "debug",
                    "HTTP {} from {}, retrying in {:.1f}s".format(status, url, wait),
                )
                time.sleep(wait)
                continue
            if status == 429:
                response.close()
                raise RateLimited("rate limited", status=status, url=url)
            if status >= 400:
                text = self._read_body(response, url, status)
                response.close()
                raise UpstreamError(
                    (text or "").strip()[:200] or "request failed",
                    status=status,
                    url=url,
                )

            # Read the body rather than letting ``response.json()`` pull it in: the declared
            # length is checked first, and the bytes are then counted as they arrive, so an
            # unbounded or lying response cannot become an unbounded allocation.
            #
            # A body that breaks *while* it is being read is the one failure that can be
            # retried, and it is the common one on a censored or flaky link — the connection
            # was up for the headers and died mid-stream. The retry loop above covers the
            # connection, so it has to cover this too, or ``network.retries`` only helps when
            # the request never started.
            try:
                return self._read_json(response, url, status)
            except _BodyInterrupted as error:
                last_error = error
                if attempt >= self.retries:
                    raise
                wait = min(self.backoff_base * (2 ** attempt), 20.0)
                self._log(
                    "debug",
                    "reading {} failed ({}), retrying in {:.1f}s".format(
                        url, type(error.__cause__).__name__ or "error", wait
                    ),
                )
                time.sleep(wait)
                continue
            finally:
                response.close()

        raise UpstreamError(
            "{}".format(last_error or "request failed"), url=url
        )

    def _read_body(self, response: "requests.Response", url: str, status: int) -> str:
        """An error response's text, bounded like any other body."""
        try:
            return response.content[: self.json_max_bytes].decode("utf-8", errors="replace")
        except requests.RequestException:
            return ""

    def _check_declared_size(self, response: "requests.Response", url: str, status: int) -> None:
        declared = response.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > self.json_max_bytes:
            raise UpstreamError(
                "the response declares {} bytes, over the {} byte limit".format(
                    declared, self.json_max_bytes
                ),
                status=status,
                url=url,
            )

    def _read_json(self, response: "requests.Response", url: str, status: int) -> Any:
        """Decode a JSON body, refusing one larger than :attr:`json_max_bytes`.

        Two checks rather than one, for the same reason the download path has two: the declared
        length catches a large response before any of it is read, and the running total catches a
        response that declares nothing, declares a lie, or is chunked.

        Two kinds of failure leave here as an :class:`UpstreamError`, and the distinction is
        the caller's: a body that broke mid-read (``ChunkedEncodingError``,
        ``IncompleteRead``, a reset connection) is retried, because the next attempt may well
        succeed, while anything the parser itself rejected is not, because the same bytes will
        be rejected again. ``RecursionError`` belongs to the second group even though it looks
        like a crash: ``json.loads`` raises it for a deeply nested document, and it is not a
        ``ValueError``, so it used to escape the client entirely — past the retry loop, past
        every ``except UpstreamError`` in the callers, and out into the check that had no
        report. The document that does it costs a few hundred kilobytes, well under
        :attr:`json_max_bytes`, so the size limit is not a defence against it.
        """
        self._check_declared_size(response, url, status)
        chunks = []
        total = 0
        try:
            for chunk in response.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                total += len(chunk)
                if total > self.json_max_bytes:
                    raise UpstreamError(
                        "the response passed the {} byte limit".format(self.json_max_bytes),
                        status=status,
                        url=url,
                    )
                chunks.append(chunk)
        except requests.RequestException as error:
            raise _BodyInterrupted(
                "{}: {}".format(type(error).__name__, error), status=status, url=url
            ) from error

        raw = b"".join(chunks)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise UpstreamError(
                "the response was not JSON ({}: {})".format(type(error).__name__, error),
                status=status,
                url=url,
            ) from error
        except MemoryError as error:
            # A nesting bomb is the one shape that reaches here as an allocation failure rather
            # than as ``RecursionError``, depending on the interpreter's limits. Both mean the
            # same thing to the caller: this response is not usable, and asking for it again
            # would do the same thing again.
            raise UpstreamError(
                "the response was too deeply nested to parse (MemoryError)",
                status=status,
                url=url,
            ) from error
        except RecursionError as error:
            raise UpstreamError(
                "the response was too deeply nested to parse (RecursionError)",
                status=status,
                url=url,
            ) from error

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

    def download(
        self,
        url: str,
        write: Callable[[bytes], Any],
        max_bytes: int,
        chunk_size: int = 65536,
    ) -> int:
        """Stream ``url`` into ``write`` (a callable taking bytes). Returns the byte count.

        Three deliberate differences from :meth:`_request`:

        * **No retry.** Once the first chunk has been handed to ``write`` a retry cannot be
          done correctly without asking the sink to rewind, and having this method own the
          file instead would push file policy into an HTTP class. Retrying is also unnecessary
          here: the caller verifies a hash and discards anything incomplete, and this is the
          one operation that already repeats on a schedule.
        * **The caller's size limit is enforced on the bytes that actually arrive**, not only
          against the size the API declared. The declared size is upstream data; the point of
          the limit is to bound what a hostile or broken response can do to the disk.
        * **A total deadline as well as a per-read timeout.** The timeout is a limit on one
          *read*, so a server that drips a byte just before each one keeps the connection alive
          for as long as it likes — measured at three seconds for a one-second timeout. The
          deadline is derived from the declared size, so it scales with the amount of data and
          cannot be extended by staying alive.
        """
        limit = max(0, int(max_bytes))
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            # The URL comes out of an upstream JSON document, so it is upstream data. ``file:``
            # or an unexpected scheme is not a download this plugin should attempt to perform.
            raise UpstreamError(
                "refusing a download from a {} URL".format(parsed.scheme or "schemeless"),
                url=url,
            )
        if self.rate_limiter is not None:
            # The same budget as every other request this plugin makes. ``network
            # .requests_per_minute`` reads as "how fast this plugin talks to the upstream", and a
            # download that does not count is a download that can double the real rate — most
            # visibly when ``download.retries`` turns a failure into a burst.
            self.rate_limiter.acquire()
        try:
            response = self._session.get(url, stream=True, timeout=self.timeouts)
        except requests.RequestException as error:
            raise UpstreamError(
                "{}: {}".format(type(error).__name__, error), url=url
            ) from error

        with response:
            status = response.status_code
            if status == 404:
                raise NotFound("file not found", status=status, url=url)
            if status in (401, 403):
                raise Unauthorised("not authorised to download", status=status, url=url)
            if status >= 400:
                raise UpstreamError("download failed", status=status, url=url)
            # A cheap pre-flight when the server declares a length, so an oversized body is
            # rejected before a single byte lands on disk.
            declared = response.headers.get("Content-Length")
            declared_size = int(declared) if declared and declared.isdigit() else 0
            if declared_size > limit:
                raise UpstreamError(
                    "the file is {} bytes, over the {} byte limit".format(declared, limit),
                    status=status,
                    url=url,
                )

            budget = self._deadline_for(declared_size, limit)
            deadline = time.monotonic() + budget
            total = 0
            try:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if not chunk:
                        continue
                    if time.monotonic() > deadline:
                        raise UpstreamError(
                            "the download did not finish within {:.0f}s".format(budget),
                            status=status,
                            url=url,
                        )
                    total += len(chunk)
                    if total > limit:
                        raise UpstreamError(
                            "the download passed the {} byte limit".format(limit),
                            status=status,
                            url=url,
                        )
                    write(chunk)
            except requests.RequestException as error:
                raise UpstreamError(
                    "{}: {}".format(type(error).__name__, error),
                    status=status,
                    url=url,
                ) from error
            return total

    def _deadline_for(self, declared_size: int, limit: int) -> float:
        """How long this transfer may take in total, in seconds.

        A quarter of the per-read timeout per declared mebibyte: a 4 MiB jar with the default
        20 s timeout has 20 s of slack, and a truncated or unannounced body is budgeted by the
        download limit instead. The floor is the read timeout itself, so a small file is never
        held to a shorter wall clock than a single read would already have been allowed.
        """
        mebibyte = 1024 * 1024
        expected = declared_size or limit
        return max(self.read_timeout, (expected / mebibyte) * self.read_timeout * _DEADLINE_FRACTION)

    def close(self) -> None:
        """Close every session this client opened, whichever thread opened it.

        Every thread's session, not only the closing thread's: the pools that matter were created
        by the workers, and leaving them open would leave their sockets behind until the process
        ends. Each session is closed exactly once even when one thread owns several logical
        users of the client.
        """
        with self._sessions_lock:
            sessions = list(dict.fromkeys(self._sessions.values()))
            self._sessions.clear()
        for session in sessions:
            try:
                session.close()
            except Exception:  # noqa: BLE001 - closing must never raise into the caller
                pass
