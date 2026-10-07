"""A local stand-in for the Modrinth API, used by the test suite.

Why a fake server instead of mocking the client: the interesting failures in this plugin are
protocol-level, not logic-level. Does an empty filter array mean "no filter" or "match
nothing"? Does a project with no build for the running game version come back as an empty
object or an empty list? Those questions are about HTTP shapes, and a fake that answers them
the way the real service does is the only thing that tests the code path a user will hit.

The behaviour reproduced here was read off the live services before being written down:

* ``POST /version_files`` returns a map containing **only** the hashes it recognises;
* ``POST /version_files/update`` returns ``{}`` when nothing matches the filters (verified
  against a deliberately impossible game version) and, importantly, treats an empty
  ``game_versions`` array as "no filter" — which is exactly why the client omits empty
  filters rather than sending them;
* ``GET /project/{id}`` and ``/project/{id}/version`` answer ``404`` for unknown ids.

Everything is served from one in-memory catalogue, and every request is recorded so tests can
assert on request counts (a batch lookup of 200 mods must not become 200 requests).
"""

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlparse

__all__ = [
    "FakeFile",
    "FakeVersion",
    "FakeProject",
    "FakeUpstream",
]


@dataclass
class FakeFile:
    """One Modrinth file."""

    sha1: str
    sha512: str = ""
    filename: str = "mod.jar"
    url: str = "https://cdn.example/mod.jar"
    size: int = 1024
    primary: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hashes": {"sha1": self.sha1, "sha512": self.sha512 or self.sha1},
            "url": self.url,
            "filename": self.filename,
            "size": self.size,
            "primary": self.primary,
        }


@dataclass
class FakeVersion:
    """One Modrinth version of a project."""

    id: str
    project_id: str
    version_number: str
    version_type: str = "release"
    loaders: Sequence[str] = ("fabric",)
    game_versions: Sequence[str] = ("26.3",)
    date_published: str = "2026-01-01T00:00:00Z"
    files: List[FakeFile] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "version_number": self.version_number,
            "name": "{} {}".format(self.project_id, self.version_number),
            "version_type": self.version_type,
            "loaders": list(self.loaders),
            "game_versions": list(self.game_versions),
            "date_published": self.date_published,
            "status": "listed",
            "files": [item.to_dict() for item in self.files],
        }


@dataclass
class FakeProject:
    """One Modrinth project."""

    id: str
    slug: str
    title: str = ""
    project_type: str = "mod"
    description: str = "a test project"
    versions: List[FakeVersion] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "slug": self.slug,
            "title": self.title or self.slug,
            "project_type": self.project_type,
            "description": self.description,
            "icon_url": "",
            "source_url": "",
            "game_versions": sorted({v for item in self.versions for v in item.game_versions}),
            "loaders": sorted({loader for item in self.versions for loader in item.loaders}),
            "client_side": "required",
            "server_side": "required",
            "downloads": 1234,
        }

    def versions_newest_first(self) -> List[FakeVersion]:
        return sorted(self.versions, key=lambda item: item.date_published, reverse=True)


class _Handler(BaseHTTPRequestHandler):
    """Routes a request against the owning :class:`FakeUpstream`."""

    protocol_version = "HTTP/1.1"
    server_version = "FakeUpstream/1"

    # -- plumbing ----------------------------------------------------------------------

    def log_message(self, *_args: Any) -> None:  # noqa: D102 - silence the test output
        pass

    @property
    def upstream(self) -> "FakeUpstream":
        return self.server.upstream  # type: ignore[attr-defined]

    def _read_body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return None

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, status: int, body: bytes, content_type: str = "application/java-archive",
                    content_length: bool = True) -> None:
        """Raw bytes, for the file-download route. Not JSON, so not ``_send``.

        ``content_length=False`` omits the header, which is what a streaming proxy does. The
        client then has no declared size to pre-check against and must bound the transfer from
        the bytes themselves — the case the mid-stream limit exists for.
        """
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if content_length:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self, method: str, path: str, body: Any) -> None:
        with self.upstream.lock:
            self.upstream.requests.append((method, path, body))
            self.upstream.request_headers.append(dict(self.headers.items()))

    def _intercept(self, path: str) -> Optional[int]:
        """Honour the fault-injection switches; returns a status to short-circuit with.

        Takes the *parsed* path rather than reading ``self.path``, because with an HTTP proxy
        in the environment ``requests`` sends an absolute-form request target
        (``http://host/v2/search``) instead of ``/v2/search`` — so a raw string comparison
        against a path silently never matches. That is exactly the kind of "the fault
        injection did nothing, so the test asserted the wrong thing" failure worth designing
        out rather than debugging twice.
        """
        upstream = self.upstream
        # ``fail_all`` is how "the upstream is down" is simulated. Closing the socket for
        # real also works, but on Windows a connection to a just-closed port can take
        # seconds to be refused, which makes the test look like a hang.
        if upstream.fail_all:
            self._send(503, {"error": "service unavailable"})
            return 503
        if path in upstream.fail_paths:
            self._send(503, {"error": "service unavailable"})
            return 503
        with upstream.lock:
            scripted = upstream.scripted_responses.pop(0) if upstream.scripted_responses else None
        # A ``None`` entry means "let this request through untouched", which is how a test
        # targets the *n*-th request without having to predict the URLs in between.
        if scripted is not None:
            if scripted == 429:
                self.send_response(429)
                self.send_header("Retry-After", "0")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return 429
            if scripted == 401:
                self._send(401, {"error": "unauthorised"})
                return 401
            if scripted == 404:
                self._send(404, {"error": "not found"})
                return 404
            if scripted == 500:
                self._send(500, {"error": "boom"})
                return 500
        return None

    # -- verbs -------------------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._dispatch("GET", None)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._dispatch("POST", self._read_body())

    def _dispatch(self, method: str, body: Any) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        self._record(method, path, body)

        status = self._intercept(path)
        if status is not None:
            return

        if path == "/plain":
            # A deliberately non-JSON 200, so the client's decode-error path is reachable.
            payload = b"<html><body>not json</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if path.startswith("/cdn/"):
            # The file-download route. Real bytes, so the auto-download feature can be tested
            # end to end against something that actually hashes to what the API declared.
            name = path[len("/cdn/"):]
            blob = self.upstream.cdn_files.get(name)
            if blob is None:
                self._send(404, {"detail": "no such file"})
                return

            with self.upstream.lock:
                remaining = self.upstream.flaky_downloads.get(name, 0)
                if remaining > 0:
                    # Fail the next N requests for this file, then serve it properly. That is
                    # what a flaky link looks like from the client's side, and it is the only
                    # way to prove a retry actually recovers rather than merely being present.
                    self.upstream.flaky_downloads[name] = remaining - 1
                    blob = blob + b"corrupted in transit"

            if name in self.upstream.fail_downloads:
                # Bytes that do not match the declared hash, to prove the downloader verifies.
                blob = blob + b"tampered"
            self._send_bytes(
                200, blob, content_length=name not in self.upstream.no_content_length
            )
            return

        handler = self.upstream.route(
            method, path, query, body, dict(self.headers.items())
        )
        if handler is None:
            self._send(404, {"detail": "no route for {} {}".format(method, path)})
            return
        status_code, payload = handler
        self._send(status_code, payload)


class FakeUpstream:
    """A running fake of both services, on one ephemeral port."""

    def __init__(self) -> None:
        self.projects: Dict[str, FakeProject] = {}
        #: Answers ``GET /v2/tag/game_version``; newest first, as the real API returns them.
        self.tag_game_versions: List[str] = ["26.3", "26.2", "26.1", "1.21.4", "1.21.1"]
        self.api_key_required = True
        self.api_key = "test-key"
        self.requests: List[Tuple[str, str, Any]] = []
        #: Headers of every request, in the same order as :attr:`requests`.
        self.request_headers: List[Dict[str, str]] = []
        #: When true, every request answers ``503`` — "the upstream is unreachable".
        self.fail_all = False
        #: Paths (query string ignored) that answer ``503`` while everything else works.
        self.fail_paths: set = set()
        #: Statuses to return for the next requests, in order, before normal routing.
        #: ``None`` in the list means "let this one through", for targeting a later request.
        self.scripted_responses: List[Optional[int]] = []
        #: Bytes served for ``GET /cdn/<name>`` — the download route.
        self.cdn_files: Dict[str, bytes] = {}
        #: Names in here are served with an extra byte appended, so the body no longer matches
        #: the hash the API declared. Used to prove the downloader actually verifies.
        self.fail_downloads: set = set()
        #: Names in here are served without a ``Content-Length``, like a streaming proxy.
        self.no_content_length: set = set()
        #: Published and perfectly downloadable, but the run must *not* fetch them — a mod the
        #: admin excluded from checking, for instance. Declared by the scenario rather than
        #: inferred, because "published" and "should be downloaded" are different questions.
        self.unwanted_downloads: set = set()
        #: name -> how many more requests to answer with corrupted bytes before serving the
        #: file properly. Drives the "a retry recovers from a flaky link" test.
        self.flaky_downloads: Dict[str, int] = {}
        self.lock = threading.Lock()
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.port = 0

    # -- lifecycle ---------------------------------------------------------------------

    def start(self) -> "FakeUpstream":
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.upstream = self  # type: ignore[attr-defined]
        self.port = self._server.server_address[1]
        # A short poll interval, otherwise every teardown pays the default 0.5s.
        self._thread = threading.Thread(
            target=lambda: self._server.serve_forever(poll_interval=0.02), daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> "FakeUpstream":
        return self.start()

    def __exit__(self, *_exc: Any) -> None:
        self.stop()

    @property
    def base(self) -> str:
        return "http://127.0.0.1:{}".format(self.port)

    @property
    def modrinth_base(self) -> str:
        return "{}/v2".format(self.base)

    # -- catalogue helpers -------------------------------------------------------------

    def add_project(self, project: FakeProject) -> FakeProject:
        self.projects[project.id] = project
        return project

    def file_url(self, name: str) -> str:
        """The URL a declared ``FakeFile`` should point at to be downloadable."""
        return "{}/cdn/{}".format(self.base, name)

    def serve_file(self, name: str, data: bytes) -> str:
        """Publish bytes on the download route and return the URL for them."""
        self.cdn_files[name] = data
        return self.file_url(name)

    def request_paths(self) -> List[str]:
        with self.lock:
            return [path for _method, path, _body in self.requests]

    def count_path(self, path: str) -> int:
        return sum(1 for item in self.request_paths() if item == path)

    def clear_requests(self) -> None:
        with self.lock:
            self.requests.clear()

    # -- routing -----------------------------------------------------------------------

    def route(
        self,
        method: str,
        path: str,
        query: Dict[str, List[str]],
        body: Any,
        headers: Optional[Dict[str, str]] = None,
    ) -> Optional[Tuple[int, Any]]:
        headers = {key.lower(): value for key, value in (headers or {}).items()}
        if path.startswith("/v2/"):
            return self._route_modrinth(method, path, query, body)
        return None

    # -- Modrinth ----------------------------------------------------------------------

    def _route_modrinth(
        self, method: str, path: str, query: Dict[str, List[str]], body: Any
    ) -> Optional[Tuple[int, Any]]:
        if method == "POST" and path == "/v2/version_files":
            return 200, self._version_files(body)
        if method == "POST" and path == "/v2/version_files/update":
            return 200, self._version_files_update(body)
        if method == "GET" and path == "/v2/projects":
            return 200, self._projects(query)
        if method == "GET" and path == "/v2/search":
            return 200, self._search(query)
        if method == "GET" and path == "/v2/tag/game_version":
            return 200, [
                {"version": item, "version_type": "release", "major": False, "date": ""}
                for item in self.tag_game_versions
            ]
        if method == "GET" and path.startswith("/v2/project/"):
            parts = path[len("/v2/project/"):].split("/")
            identifier = parts[0]
            remainder = parts[1:]
            if remainder == ["version"]:
                project = self._find_project(identifier)
                if project is None:
                    return 404, {"detail": "project not found"}
                return 200, self._project_versions(project, query)
            if not remainder:
                project = self._find_project(identifier)
                if project is None:
                    return 404, {"detail": "project not found"}
                return 200, project.to_dict()
        return None

    def _find_project(self, identifier: str) -> Optional[FakeProject]:
        if identifier in self.projects:
            return self.projects[identifier]
        for project in self.projects.values():
            if project.slug == identifier:
                return project
        return None

    def _hash_index(self) -> Dict[str, Tuple[FakeProject, FakeVersion, FakeFile]]:
        index: Dict[str, Tuple[FakeProject, FakeVersion, FakeFile]] = {}
        for project in self.projects.values():
            for version in project.versions:
                for item in version.files:
                    if item.sha1:
                        index[item.sha1] = (project, version, item)
        return index

    def _version_files(self, body: Any) -> Dict[str, Any]:
        if not isinstance(body, dict):
            return {}
        index = self._hash_index()
        out: Dict[str, Any] = {}
        for hash_value in body.get("hashes") or []:
            found = index.get(str(hash_value))
            if found is not None:
                out[str(hash_value)] = found[1].to_dict()
        return out

    def _version_files_update(self, body: Any) -> Dict[str, Any]:
        if not isinstance(body, dict):
            return {}
        loaders = set(body.get("loaders") or [])
        game_versions = set(body.get("game_versions") or [])
        index = self._hash_index()
        out: Dict[str, Any] = {}
        for hash_value in body.get("hashes") or []:
            found = index.get(str(hash_value))
            if found is None:
                continue
            project = found[0]
            candidates = []
            for version in project.versions_newest_first():
                if loaders and not (set(version.loaders) & loaders):
                    continue
                if game_versions and not (set(version.game_versions) & game_versions):
                    continue
                candidates.append(version)
            if candidates:
                out[str(hash_value)] = candidates[0].to_dict()
        return out

    def _projects(self, query: Dict[str, List[str]]) -> List[Dict[str, Any]]:
        raw = (query.get("ids") or ["[]"])[0]
        try:
            ids = json.loads(raw)
        except ValueError:
            return []
        return [self.projects[item].to_dict() for item in ids if item in self.projects]

    def _project_versions(
        self, project: FakeProject, query: Dict[str, List[str]]
    ) -> List[Dict[str, Any]]:
        def decoded(key: str) -> List[str]:
            raw = (query.get(key) or [""])[0]
            if not raw:
                return []
            try:
                value = json.loads(raw)
            except ValueError:
                return []
            return [str(item) for item in value] if isinstance(value, list) else []

        loaders = set(decoded("loaders"))
        game_versions = set(decoded("game_versions"))
        seen: set = set()
        out: List[Dict[str, Any]] = []
        for version in project.versions_newest_first():
            if loaders and not (set(version.loaders) & loaders):
                continue
            if game_versions and not (set(version.game_versions) & game_versions):
                continue
            if version.id in seen:
                continue
            seen.add(version.id)
            out.append(version.to_dict())
        return out

    def _search(self, query: Dict[str, List[str]]) -> Dict[str, Any]:
        text = (query.get("query") or [""])[0].lower()
        hits = []
        for project in self.projects.values():
            if text and text not in project.slug.lower() and text not in project.title.lower():
                continue
            hits.append(
                {
                    "project_id": project.id,
                    "slug": project.slug,
                    "title": project.title or project.slug,
                    "description": project.description,
                    "author": "tester",
                    "project_type": project.project_type,
                    "loaders": sorted({n for v in project.versions for n in v.loaders}),
                    "versions": sorted({v for vv in project.versions for v in vv.game_versions}),
                    "downloads": 1234,
                }
            )
        return {"hits": hits, "offset": 0, "limit": 10, "total_hits": len(hits)}
