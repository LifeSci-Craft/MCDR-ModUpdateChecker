"""The Modrinth client, driven against the local fake.

These tests are about protocol shape rather than logic: which requests are sent, with what
bodies, and how the several "nothing matches" answers are told apart. That is where the
expensive mistakes live — a batch lookup that silently becomes one request per mod, or an
empty filter array that the server reads as "match nothing" and which would report every mod
as having no compatible build.
"""

import pytest

from mod_update_checker import modrinth as modrinth_module
from mod_update_checker.modrinth import ModrinthClient
from mod_update_checker.upstream import (
    HttpClient,
    RateLimiter,
    UpstreamError,
)

from fake_upstream import FakeFile, FakeProject, FakeUpstream, FakeVersion


@pytest.fixture
def upstream():
    server = FakeUpstream().start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def http():
    client = HttpClient(user_agent="test/1.0", timeout=10, retries=2, backoff_base=0.01)
    try:
        yield client
    finally:
        client.close()


def make_project(project_id="proj", slug="proj", game_versions=("26.3",), loaders=("fabric",)):
    """A project with two versions: an old one (whose file we "have") and a newer one."""
    files = [FakeFile(sha1="a" * 40, filename="mod-1.0.0.jar")]
    return FakeProject(
        id=project_id,
        slug=slug,
        title=slug.title(),
        versions=[
            FakeVersion(
                id="v-old",
                project_id=project_id,
                version_number="1.0.0",
                loaders=loaders,
                game_versions=game_versions,
                date_published="2026-01-01T00:00:00Z",
                files=files,
            ),
            FakeVersion(
                id="v-new",
                project_id=project_id,
                version_number="1.1.0",
                loaders=loaders,
                game_versions=game_versions,
                date_published="2026-02-01T00:00:00Z",
                files=[FakeFile(sha1="b" * 40, filename="mod-1.1.0.jar")],
            ),
        ],
    )


# --------------------------------------------------------------------------------------
# Modrinth
# --------------------------------------------------------------------------------------


def test_versions_from_hashes_returns_only_known_hashes(upstream, http):
    upstream.add_project(make_project())
    client = ModrinthClient(http, base_url=upstream.modrinth_base)

    found = client.versions_from_hashes(["a" * 40, "f" * 40])

    assert list(found) == ["a" * 40]
    assert found["a" * 40].version_number == "1.0.0"
    assert found["a" * 40].project_id == "proj"


def test_latest_from_hashes_picks_the_newest_matching_build(upstream, http):
    upstream.add_project(make_project())
    client = ModrinthClient(http, base_url=upstream.modrinth_base)

    found = client.latest_from_hashes(["a" * 40], loaders=["fabric"], game_versions=["26.3"])

    assert found["a" * 40].version_number == "1.1.0"


def test_latest_from_hashes_returns_nothing_when_no_build_matches(upstream, http):
    upstream.add_project(make_project())
    client = ModrinthClient(http, base_url=upstream.modrinth_base)

    assert client.latest_from_hashes(["a" * 40], loaders=["fabric"], game_versions=["1.7.10"]) == {}
    assert client.latest_from_hashes(["a" * 40], loaders=["forge"], game_versions=["26.3"]) == {}


def test_empty_filters_are_omitted_from_the_request_body(upstream, http):
    """An empty array is not sent at all.

    The real service tolerates it today, but "filter by an empty set" has exactly one other
    sensible reading — match nothing — and that reading would report every mod on the server
    as having no compatible build. Not sending the key removes the ambiguity.
    """
    upstream.add_project(make_project())
    client = ModrinthClient(http, base_url=upstream.modrinth_base)

    client.latest_from_hashes(["a" * 40], loaders=["fabric"], game_versions=[])

    bodies = [body for method, path, body in upstream.requests if path == "/v2/version_files/update"]
    assert len(bodies) == 1
    assert "game_versions" not in bodies[0]
    assert bodies[0]["loaders"] == ["fabric"]


def test_latest_from_hashes_falls_back_to_a_plain_lookup_without_filters(upstream, http):
    """With nothing to filter by there is no point in the `/update` endpoint."""
    upstream.add_project(make_project())
    client = ModrinthClient(http, base_url=upstream.modrinth_base)

    found = client.latest_from_hashes(["a" * 40])

    assert found["a" * 40].version_number == "1.0.0"
    assert "/v2/version_files" in upstream.request_paths()
    assert "/v2/version_files/update" not in upstream.request_paths()


def test_hashes_are_batched_not_sent_one_by_one(upstream, http, monkeypatch):
    """A 200-mod server must not produce 200 requests."""
    monkeypatch.setattr(modrinth_module, "HASH_CHUNK", 3)
    upstream.add_project(make_project())
    client = ModrinthClient(http, base_url=upstream.modrinth_base)

    hashes = ["{:040x}".format(index) for index in range(10)]
    client.versions_from_hashes(hashes)

    assert upstream.count_path("/v2/version_files") == 4  # ceil(10 / 3)


def test_duplicate_hashes_are_deduplicated(upstream, http):
    upstream.add_project(make_project())
    client = ModrinthClient(http, base_url=upstream.modrinth_base)

    client.versions_from_hashes(["a" * 40, "a" * 40, "a" * 40])

    body = upstream.requests[-1][2]
    assert body["hashes"] == ["a" * 40]


def test_projects_are_fetched_in_one_batch(upstream, http):
    for index in range(3):
        upstream.add_project(make_project(project_id="p{}".format(index), slug="p{}".format(index)))
    client = ModrinthClient(http, base_url=upstream.modrinth_base)

    found = client.projects(["p0", "p1", "p2", "missing"])

    assert sorted(found) == ["p0", "p1", "p2"]
    assert found["p0"].slug == "p0"
    assert upstream.count_path("/v2/projects") == 1


def test_project_and_project_versions_handle_404(upstream, http):
    upstream.add_project(make_project())
    client = ModrinthClient(http, base_url=upstream.modrinth_base)

    assert client.project("nope") is None
    assert client.project_versions("nope") == []


def test_project_versions_filters_server_side(upstream, http):
    upstream.add_project(make_project(game_versions=("26.3",), loaders=("fabric",)))
    client = ModrinthClient(http, base_url=upstream.modrinth_base)

    assert len(client.project_versions("proj", loaders=["fabric"])) == 2
    assert client.project_versions("proj", loaders=["fabric"], game_versions=["1.7.10"]) == []


def test_search_returns_hits(upstream, http):
    upstream.add_project(make_project(project_id="sodium", slug="sodium"))
    client = ModrinthClient(http, base_url=upstream.modrinth_base)

    hits = client.search("sodium", loaders=["fabric"])

    assert [hit.slug for hit in hits] == ["sodium"]
    assert hits[0].page_url.endswith("/sodium")


def test_release_game_versions_returns_releases_newest_first(upstream, http):
    client = ModrinthClient(http, base_url=upstream.modrinth_base)
    assert client.release_game_versions()[0] == "26.3"


def test_version_helpers():
    version = FakeVersion(
        id="v",
        project_id="p",
        version_number="2.0.0",
        files=[
            FakeFile(sha1="1" * 40, primary=False),
            FakeFile(sha1="2" * 40, primary=True),
        ],
    ).to_dict()
    from mod_update_checker.modrinth import ModrinthVersion

    parsed = ModrinthVersion.from_dict(version)
    assert parsed.primary_file.sha1 == "2" * 40
    # ``hashes()`` exists to answer "is our local file one of this version's files?", so it
    # returns every file's SHA-1 and SHA-512 in file order.
    assert set(parsed.hashes()) >= {"1" * 40, "2" * 40}
    assert set(parsed.hashes()) <= {"1" * 40, "2" * 40, "1" * 40, "2" * 40}
    assert parsed.supports("26.3") is True
    assert parsed.supports("1.7.10") is False
    assert parsed.page_url().endswith("/version/v")


# --------------------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------------------


def test_rate_limiter_allows_up_to_the_limit_then_blocks():
    clock_value = [0.0]
    limiter = RateLimiter(2, clock=lambda: clock_value[0])

    limiter.acquire()
    limiter.acquire()
    # The third call has to wait, so it can only return once the window has moved on.
    clock_value[0] = 61.0
    limiter.acquire()


def test_rate_limiter_of_zero_is_a_no_op():
    limiter = RateLimiter(0)
    limiter.acquire()
    limiter.acquire()


def test_429_is_retried_and_can_succeed(upstream, http):
    upstream.add_project(make_project())
    upstream.scripted_responses = [429]
    client = ModrinthClient(http, base_url=upstream.modrinth_base)

    found = client.versions_from_hashes(["a" * 40])

    assert found["a" * 40].version_number == "1.0.0"
    assert upstream.count_path("/v2/version_files") == 2


def test_transient_500_is_retried(upstream, http):
    upstream.add_project(make_project())
    upstream.scripted_responses = [500, 500]
    client = ModrinthClient(http, base_url=upstream.modrinth_base)

    assert client.versions_from_hashes(["a" * 40])["a" * 40].version_number == "1.0.0"
    assert upstream.count_path("/v2/version_files") == 3


def test_allow_404_returns_none_and_strict_raises(upstream, http):
    assert http.get_json(upstream.modrinth_base + "/project/nope", allow_404=True) is None
    with pytest.raises(UpstreamError):
        http.get_json(upstream.modrinth_base + "/project/nope")


def test_a_non_json_response_is_reported_as_an_upstream_error(upstream, http):
    with pytest.raises(UpstreamError):
        http.get_json(upstream.base + "/plain")


def test_the_user_agent_header_is_sent(upstream):
    client = HttpClient(user_agent="MCDR-ModUpdateChecker-test/9.9")
    try:
        client.get_json(upstream.modrinth_base + "/projects", params={"ids": "[]"})
    finally:
        client.close()

    assert upstream.request_headers[-1]["User-Agent"] == "MCDR-ModUpdateChecker-test/9.9"


# --------------------------------------------------------------------------------------
# The bounds on a response
#
# The download path has always had two size limits — the declared length and the bytes that
# arrive. The JSON path had none, and ``project_versions`` without filters can return a large
# project's entire history, fetched up to eight at a time. These are the same two limits, for
# the other half of the client.
# --------------------------------------------------------------------------------------


def test_a_declared_oversized_json_body_is_refused_before_it_is_read(upstream, http):
    class Huge:
        status_code = 200
        headers = {"Content-Length": "999999999"}
        content = b""

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def iter_content(self, chunk_size=1):
            raise AssertionError("the body was read despite the declared length")
            yield b""  # pragma: no cover - unreachable, keeps this a generator

        def close(self):
            pass

    http._thread_local.session = _StubSession(Huge())

    with pytest.raises(UpstreamError) as raised:
        http.get_json(upstream.modrinth_base + "/projects")

    assert "limit" in str(raised.value)


def test_a_json_body_that_lies_about_its_length_is_still_bounded(upstream, http):
    """A chunked or lying response has no declared length to pre-check, so the bytes are counted."""
    blob = b"[" + b"1," * 200000 + b"1]"

    class Chunky:
        status_code = 200
        headers = {}
        content = blob

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def iter_content(self, chunk_size=1):
            for start in range(0, len(blob), 1024):
                yield blob[start:start + 1024]

        def close(self):
            pass

    http.json_max_bytes = 1024
    http._thread_local.session = _StubSession(Chunky())

    with pytest.raises(UpstreamError) as raised:
        http.get_json(upstream.modrinth_base + "/projects")

    assert "limit" in str(raised.value)


class _StubSession:
    """A session whose ``request`` returns a prepared response object.

    Deliberately not a ``Mock``: the client touches ``status_code``, ``headers``,
    ``iter_content`` and ``close``, and a stub that states exactly those keeps the test honest
    about which parts of the interface the code actually depends on.
    """

    def __init__(self, response):
        self._response = response
        self.headers = {}
        self.max_redirects = 30

    def request(self, *_args, **_kwargs):
        return self._response

    def get(self, *_args, **_kwargs):
        return self._response

    def close(self):
        pass


# --------------------------------------------------------------------------------------
# Not one session, shared
# --------------------------------------------------------------------------------------


def test_each_thread_gets_its_own_session(upstream):
    """``requests`` documents ``Session`` as not thread-safe, and this client is pooled.

    The sessions carry cookies, redirect history and adapter state; the connection pools under
    them are thread-safe, which is exactly why the failure this prevents is rare and timing
    dependent — the kind of thing that gets blamed on the network. So the structure is asserted
    rather than the symptom: eight workers, eight sessions, none shared.
    """
    import threading

    client = HttpClient(user_agent="test/1.0", timeout=10, retries=0)
    seen = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)
    errors = []

    def worker():
        try:
            barrier.wait(timeout=10)
            session = client._session
            with lock:
                seen.append(id(session))
            client.get_json("{}/projects".format(upstream.modrinth_base), params={"ids": "[]"})
        except Exception as error:  # noqa: BLE001 - reported through ``errors``
            errors.append("{}: {}".format(type(error).__name__, error))

    threads = [threading.Thread(target=worker) for _index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    client.close()

    assert errors == []
    assert len(seen) == 8
    assert len(set(seen)) == 8, "two threads shared one Session"


def test_a_session_carries_the_configured_redirect_budget(upstream):
    """``requests`` defaults to 30 redirects and will follow a chain to another host.

    A download URL comes out of an upstream JSON document, so the target is upstream data and
    deserves a budget somebody chose.
    """
    from mod_update_checker.upstream import _MAX_REDIRECTS

    client = HttpClient(user_agent="test/1.0")
    try:
        client.get_json("{}/projects".format(upstream.modrinth_base), params={"ids": "[]"})
        assert client._session.max_redirects == _MAX_REDIRECTS
        assert _MAX_REDIRECTS < 30
    finally:
        client.close()


# --------------------------------------------------------------------------------------
# The download path: rate limiting, scheme check, total deadline
# --------------------------------------------------------------------------------------


def _publish(upstream, name, blob):
    upstream.serve_file(name, blob)
    return upstream.file_url(name)


def test_a_download_consumes_a_rate_limit_grant(upstream, http):
    """``network.requests_per_minute`` is about every request the plugin makes, downloads too.

    A download that does not count can double the real rate against the upstream — most visibly
    when ``download.retries`` turns one failure into a burst.
    """
    limiter = _CountingLimiter(0)
    http.rate_limiter = limiter

    total = http.download(_publish(upstream, "one.jar", b"x" * 16), lambda chunk: None,
                          max_bytes=1024)

    assert total == 16
    assert limiter.grants == 1


def test_a_download_url_must_be_http(upstream, http):
    """The URL comes from upstream JSON, so the scheme is upstream data too."""
    with pytest.raises(UpstreamError) as raised:
        http.download("file:///etc/passwd", lambda chunk: None, max_bytes=1024)

    assert "refusing" in str(raised.value)


def test_a_dripping_download_is_stopped_by_the_total_deadline():
    """The ``timeout`` is a limit on one *read*, so a slow drip can hold a thread open forever.

    The response below never pauses longer than the read timeout, which is exactly the shape the
    per-read limit cannot catch: every individual read succeeds, and the transfer as a whole runs
    on until the deadline stops it. The drip is simulated rather than socketed so the test is
    deterministic and fast; what is being asserted is that the loop consults a clock of its own.
    """
    import time

    class Dripping:
        status_code = 200
        headers = {"Content-Length": "10485760"}
        content = b""

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def iter_content(self, chunk_size=1):
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                time.sleep(0.02)
                yield b"x"

        def close(self):
            pass

    received = []

    client = HttpClient(user_agent="test/1.0", timeout=0.2, retries=0)
    client._thread_local.session = _StubSession(Dripping())
    try:
        with pytest.raises(UpstreamError) as raised:
            client.download(
                "http://example.invalid/drip.jar", received.append, max_bytes=10 * 1024 * 1024
            )
    finally:
        client.close()

    assert "did not finish" in str(raised.value)
    # What arrived before the deadline was handed to the sink in order, and nothing was written
    # after it: the caller is responsible for discarding an incomplete file, and ``_transfer_once``
    # does exactly that.
    assert b"".join(received) == b"x" * len(received)
    assert 0 < len(received) < 200, len(received)


class _CountingLimiter:
    """A ``RateLimiter`` that only counts, so a test can assert on the grants consumed."""

    def __init__(self, per_minute):
        self._per_minute = per_minute
        self.grants = 0

    def acquire(self):
        self.grants += 1

