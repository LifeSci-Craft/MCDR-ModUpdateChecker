"""The two upstream clients, driven against the local fake.

These tests are about protocol shape rather than logic: which requests are sent, with what
bodies, and how the several "nothing matches" answers are told apart. That is where the
expensive mistakes live — a batch lookup that silently becomes one request per mod, or an
empty filter array that the server reads as "match nothing" and which would report every mod
as having no compatible build.
"""

import pytest

from mod_update_checker import modrinth as modrinth_module
from mod_update_checker.curseforge import CurseForgeClient
from mod_update_checker.modrinth import ModrinthClient
from mod_update_checker.upstream import (
    HttpClient,
    RateLimiter,
    Unauthorised,
    UpstreamError,
)

from fake_upstream import FakeCfFile, FakeCfMod, FakeFile, FakeProject, FakeUpstream, FakeVersion


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
# CurseForge
# --------------------------------------------------------------------------------------


def cf_mod_with_files():
    return FakeCfMod(
        id=238222,
        slug="jei",
        name="Just Enough Items",
        files=[
            FakeCfFile(
                id=1,
                mod_id=238222,
                file_name="jei-1.20.1-fabric-15.2.0.jar",
                display_name="15.2.0 for Fabric 1.20.1",
                fingerprint=111,
                game_versions=("1.20.1", "Fabric"),
                file_date="2026-01-01T00:00:00Z",
            ),
            FakeCfFile(
                id=2,
                mod_id=238222,
                file_name="jei-26.3-fabric-19.0.0.jar",
                display_name="19.0.0 for Fabric 26.3",
                fingerprint=222,
                game_versions=("26.3", "Fabric"),
                file_date="2026-03-01T00:00:00Z",
            ),
        ],
    )


def test_curseforge_is_disabled_without_a_key_and_sends_nothing(upstream, http):
    upstream.add_cf_mod(cf_mod_with_files())
    client = CurseForgeClient(http, api_key="", base_url=upstream.curseforge_base)

    assert client.enabled is False
    assert client.files_by_fingerprints([111]) == {}
    assert client.mods([238222]) == {}
    assert client.search("jei") == []
    assert client.mod_files(238222) == []
    assert upstream.requests == []


def test_files_by_fingerprints_maps_only_the_hashes_we_sent(upstream, http):
    upstream.add_cf_mod(cf_mod_with_files())
    client = CurseForgeClient(http, api_key="test-key", base_url=upstream.curseforge_base)

    found = client.files_by_fingerprints([111, 999999])

    assert list(found) == [111]
    assert found[111].id == 1
    assert found[111].mod_id == 238222
    assert found[111].file_name == "jei-1.20.1-fabric-15.2.0.jar"


def test_files_by_fingerprints_drops_a_match_that_does_not_echo_our_hash(upstream, http):
    """A match whose ``fileFingerprint`` is not one we asked about is discarded rather than
    attributed, because attributing the wrong project gives a confidently wrong report."""
    upstream.add_cf_mod(cf_mod_with_files())

    original = FakeUpstream._fingerprints

    def tampered(self, body):
        payload = original(self, body)
        for match in payload["data"]["exactMatches"]:
            match["file"]["fileFingerprint"] = 12345
        return payload

    FakeUpstream._fingerprints = tampered
    try:
        client = CurseForgeClient(http, api_key="test-key", base_url=upstream.curseforge_base)
        assert client.files_by_fingerprints([111]) == {}
    finally:
        FakeUpstream._fingerprints = original


def test_mod_files_filters_by_game_version_and_loader(upstream, http):
    upstream.add_cf_mod(cf_mod_with_files())
    client = CurseForgeClient(http, api_key="test-key", base_url=upstream.curseforge_base)

    newest = client.mod_files(238222, game_version="26.3", loader_type=4)

    assert [item.id for item in newest] == [2]
    assert newest[0].download_url is not None

    everything = client.mod_files(238222)
    assert [item.id for item in everything] == [2, 1]  # newest first


def test_mod_files_respects_release_channel_filters(upstream, http):
    mod = cf_mod_with_files()
    mod.files[1].release_type = 2  # beta
    upstream.add_cf_mod(mod)
    client = CurseForgeClient(http, api_key="test-key", base_url=upstream.curseforge_base)

    assert [item.id for item in client.mod_files(238222, release_types=[1])] == [1]
    assert [item.id for item in client.mod_files(238222, release_types=[1, 2])] == [2, 1]


def test_curseforge_mods_and_search(upstream, http):
    upstream.add_cf_mod(cf_mod_with_files())
    client = CurseForgeClient(http, api_key="test-key", base_url=upstream.curseforge_base)

    assert client.mods([238222])[238222].slug == "jei"
    assert [item.slug for item in client.search("jei")] == ["jei"]
    assert client.search("somethingelse") == []
    assert client.mod(238222).name == "Just Enough Items"
    assert client.mod(999) is None


def test_a_rejected_key_raises_unauthorised(upstream, http):
    upstream.add_cf_mod(cf_mod_with_files())
    client = CurseForgeClient(http, api_key="wrong-key", base_url=upstream.curseforge_base)

    with pytest.raises(Unauthorised):
        client.files_by_fingerprints([111])
    with pytest.raises(Unauthorised):
        client.mods([238222])
    with pytest.raises(Unauthorised):
        client.search("jei")


def test_download_url_none_is_preserved(upstream, http):
    """An author can opt out of third-party downloads; the client must not invent a link."""
    mod = cf_mod_with_files()
    mod.files[1].download_url = None
    upstream.add_cf_mod(mod)
    client = CurseForgeClient(http, api_key="test-key", base_url=upstream.curseforge_base)

    newest = client.mod_files(238222, game_version="26.3", loader_type=4)

    assert newest[0].download_url is None


def test_loader_type_mapping():
    client = CurseForgeClient(HttpClient("test/1.0"), api_key="k")
    assert client.loader_type_for("fabric") == 4
    assert client.loader_type_for("quilt") == 5
    assert client.loader_type_for("neoforge") == 6
    assert client.loader_type_for("forge") == 1
    assert client.loader_type_for("nonsense") is None
    assert client.loader_label("fabric") == "Fabric"


def test_cf_file_helpers():
    file = FakeCfFile(
        id=2,
        mod_id=1,
        file_name="x.jar",
        game_versions=("26.3", "Fabric"),
    ).to_dict()
    from mod_update_checker.curseforge import CfFile

    parsed = CfFile.from_dict(file)
    assert parsed.release_channel == "release"
    assert parsed.supports_game_version("26.3") is True
    assert parsed.supports_game_version("1.7.10") is False
    assert parsed.supports_loader("fabric") is True
    assert parsed.supports_loader("quilt") is False
    assert parsed.page_url("jei").endswith("/jei/files/2")


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
