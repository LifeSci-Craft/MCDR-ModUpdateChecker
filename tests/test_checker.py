"""The whole check run, against the local fake of both upstreams.

This is the test that matters. Everything before it checks a piece; this one checks that the
pieces add up to a report an admin can act on, and it is deliberately built around the
distinctions that are easy to lose:

* a mod whose project exists but publishes nothing for this loader must not look like a mod
  with no upstream project at all;
* a jar that is on neither platform must be reported as unresolved rather than guessed at;
* a name-based match must be labelled as such, because it is a guess;
* a jar that is only *slightly* out of date must be distinguishable from one that is a whole
  game version behind.

It also pins the request budget. A batched lookup that quietly degrades into one request per
mod is a change nobody notices until a 200-mod server hits a rate limit, so the counts are
asserted rather than assumed.
"""

import json

import pytest

from mod_update_checker.checker import CheckOptions, Checker, ResolveCache, normalise_name
from mod_update_checker.i18n import make_translator
from mod_update_checker.report import (
    STATUS_AWAITING_INSTALL,
    STATUS_ERROR,
    STATUS_IGNORED,
    STATUS_LOCAL_AHEAD,
    STATUS_NOT_A_MOD,
    STATUS_NO_COMPATIBLE_BUILD,
    STATUS_UNRESOLVED,
    STATUS_UP_TO_DATE,
    STATUS_UPDATE_AVAILABLE,
    Report,
    UpdateEntry,
    render_full,
    render_summary,
)
from mod_update_checker.scanner import scan_jar, scan_mods
from mod_update_checker.serverinfo import ServerContext

from fake_upstream import (
    FakeCfFile,
    FakeCfMod,
    FakeFile,
    FakeProject,
    FakeUpstream,
    FakeVersion,
)
from support import fabric_metadata, write_jar

SERVER = ServerContext(mc_version="26.3", mc_version_source="config", loader="fabric")

DELTA_CF_MOD_ID = 424242
DELTA_NEW_FILE_ID = 9001
DELTA_OLD_FILE_ID = 9000


@pytest.fixture
def upstream():
    server = FakeUpstream().start()
    try:
        yield server
    finally:
        server.stop()


def _version(project_id, version_id, number, sha1, game_versions=("26.3",), date="2026-01-01T00:00:00Z",
             filename="mod.jar", loaders=("fabric",)):
    return FakeVersion(
        id=version_id,
        project_id=project_id,
        version_number=number,
        loaders=loaders,
        game_versions=game_versions,
        date_published=date,
        files=[FakeFile(sha1=sha1, filename=filename, url="https://cdn.example/" + filename)],
    )


class Scenario:
    """A populated ``mods/`` folder plus the matching upstream catalogue."""

    def __init__(self, tmp_path, upstream):
        self.directory = tmp_path / "mods"
        self.upstream = upstream
        self.mods = {}

    def add_jar(self, file_name, **metadata):
        path = write_jar(self.directory / file_name, fabric=fabric_metadata(**metadata))
        mod = scan_jar(path)
        self.mods[metadata.get("id", file_name)] = mod
        return mod

    def add_plain_jar(self, file_name, payload=None):
        path = self.directory / file_name
        write_jar(path, fabric=None)
        mod = scan_jar(path)
        self.mods[file_name] = mod
        return mod

    def scan(self):
        return scan_mods(self.directory)

    def by_name(self, name):
        return self.mods[name]


def build_scenario(tmp_path, upstream):
    """One mod per interesting outcome. See the module docstring for why each exists."""
    scenario = Scenario(tmp_path, upstream)

    # 1. A project with a newer build for this loader and game version.
    alpha = scenario.add_jar("alpha.jar", id="alpha", version="1.0.0", name="Alpha")
    upstream.add_project(
        FakeProject(
            id="proj-alpha",
            slug="alpha",
            title="Alpha Mod",
            versions=[
                _version("proj-alpha", "alpha-100", "1.0.0", alpha.sha1, filename="alpha-1.0.0.jar"),
                _version("proj-alpha", "alpha-110", "1.1.0", "d" * 40, date="2026-02-01T00:00:00Z",
                         filename="alpha-1.1.0.jar"),
            ],
        )
    )

    # 2. A project whose newest build *is* the local file.
    beta = scenario.add_jar("beta.jar", id="beta", version="2.0.0", name="Beta")
    upstream.add_project(
        FakeProject(
            id="proj-beta",
            slug="beta",
            title="Beta Mod",
            versions=[_version("proj-beta", "beta-200", "2.0.0", beta.sha1, filename="beta-2.0.0.jar")],
        )
    )

    # 3. A project that only publishes for an older game version.
    gamma = scenario.add_jar("gamma.jar", id="gamma", version="1.0.0", name="Gamma")
    upstream.add_project(
        FakeProject(
            id="proj-gamma",
            slug="gamma",
            title="Gamma Mod",
            versions=[
                _version("proj-gamma", "gamma-old", "0.9.0", gamma.sha1,
                         game_versions=("1.20.1",), filename="gamma-0.9.0.jar"),
                _version("proj-gamma", "gamma-121", "1.0.0", gamma.sha1,
                         game_versions=("1.20.1",), date="2026-02-01T00:00:00Z",
                         filename="gamma-1.0.0.jar"),
            ],
        )
    )

    # 4. A project that publishes nothing for this loader at all.
    eta = scenario.add_jar("eta.jar", id="eta", version="1.0.0", name="Eta")
    upstream.add_project(
        FakeProject(
            id="proj-eta",
            slug="eta",
            title="Eta Mod",
            versions=[
                _version("proj-eta", "eta-forge", "1.0.0", eta.sha1, loaders=("forge",),
                         filename="eta-1.0.0.jar"),
            ],
        )
    )

    # 5. A CurseForge-only mod, identified by fingerprint rather than by hash.
    delta = scenario.add_jar("delta.jar", id="delta", version="1.0.0", name="Delta")
    upstream.add_cf_mod(
        FakeCfMod(
            id=DELTA_CF_MOD_ID,
            slug="delta",
            name="Delta Mod",
            files=[
                FakeCfFile(
                    id=DELTA_OLD_FILE_ID,
                    mod_id=DELTA_CF_MOD_ID,
                    file_name="delta-1.0.0.jar",
                    display_name="1.0.0 for Fabric 26.3",
                    fingerprint=delta.fingerprint,
                    game_versions=("26.3", "Fabric"),
                    file_date="2026-01-01T00:00:00Z",
                ),
                FakeCfFile(
                    id=DELTA_NEW_FILE_ID,
                    mod_id=DELTA_CF_MOD_ID,
                    file_name="delta-1.1.0.jar",
                    display_name="1.1.0 for Fabric 26.3",
                    fingerprint=987654,
                    game_versions=("26.3", "Fabric"),
                    file_date="2026-03-01T00:00:00Z",
                    download_url="https://edge.example/delta-1.1.0.jar",
                ),
            ],
        )
    )

    # 6. Only findable by name: not on Modrinth by hash, not on CurseForge at all.
    scenario.add_jar("epsilon.jar", id="epsilon", version="1.0.0", name="Epsilon")
    upstream.add_project(
        FakeProject(
            id="proj-epsilon",
            slug="epsilon",
            title="Epsilon Mod",
            versions=[
                _version("proj-epsilon", "eps-100", "1.0.0", "e" * 40),
                _version("proj-epsilon", "eps-120", "1.2.0", "f" * 40, date="2026-02-01T00:00:00Z"),
            ],
        )
    )

    # 7. Nowhere at all.
    scenario.add_jar("zeta.jar", id="zeta", version="1.0.0", name="Zeta")

    # 8. Listed in ignored_mods, even though a newer build exists.
    ignored = scenario.add_jar("ignored.jar", id="ignored", version="1.0.0", name="Ignored")
    upstream.add_project(
        FakeProject(
            id="proj-ignored",
            slug="ignored",
            title="Ignored Mod",
            versions=[
                _version("proj-ignored", "ign-100", "1.0.0", ignored.sha1),
                _version("proj-ignored", "ign-200", "2.0.0", "a" * 40, date="2026-02-01T00:00:00Z"),
            ],
        )
    )

    # 9. A jar that is not a mod.
    scenario.add_plain_jar("library.jar")

    # 10. A client-only mod installed twice, which is two advisories at once.
    scenario.add_jar("hud.jar", id="hud", version="1.0.0", name="Hud", environment="client")
    scenario.add_jar("hud-copy.jar", id="hud", version="1.1.0", name="Hud", environment="client")

    return scenario


def make_options(upstream, **overrides):
    values = {
        "loader": "fabric",
        "mc_version": "26.3",
        "modrinth_base": upstream.modrinth_base,
        "curseforge_base": upstream.curseforge_base,
        "curseforge_api_key": "test-key",
        "ignored_mods": ["ignored"],
        "workers": 4,
        "requests_per_minute": 0,
        "timeout": 10,
        "retries": 1,
        "use_cache": False,
    }
    values.update(overrides)
    return CheckOptions(**values)


def run_check(upstream, scenario, tmp_path, **overrides):
    options = make_options(upstream, **overrides)
    checker = Checker(options)
    try:
        return checker.run(scenario.scan(), SERVER, cache_path=None)
    finally:
        checker.close()


def entry_for(report, mod_id):
    """The single entry for a mod id.

    Looked up by mod id rather than by display name on purpose: a name-based match replaces
    the local display name with the upstream project title, so the name is not a stable key.
    """
    matches = [entry for entry in report.entries if entry.mod_id == mod_id]
    if len(matches) != 1:
        raise AssertionError(
            "expected exactly one entry for {!r}, got {}".format(
                mod_id, [(item.mod_id, item.file_name) for item in report.entries]
            )
        )
    return matches[0]


def entry_for_file(report, file_name):
    matches = [entry for entry in report.entries if entry.file_name == file_name]
    assert len(matches) == 1, [item.file_name for item in report.entries]
    return matches[0]


# --------------------------------------------------------------------------------------
# The outcomes
# --------------------------------------------------------------------------------------


@pytest.fixture
def report(tmp_path, upstream):
    scenario = build_scenario(tmp_path, upstream)
    return run_check(upstream, scenario, tmp_path)


def test_update_available_when_the_project_has_a_newer_build(report):
    entry = entry_for(report, "alpha")
    assert entry.status == STATUS_UPDATE_AVAILABLE
    assert entry.local_version == "1.0.0"
    assert entry.latest_version == "1.1.0"
    assert entry.platform == "modrinth"
    assert entry.matched_by == "hash"
    assert entry.project_url.endswith("/alpha")
    assert entry.download_url.endswith("alpha-1.1.0.jar")


def test_up_to_date_when_the_local_file_is_the_newest_build(report):
    entry = entry_for(report, "beta")
    assert entry.status == STATUS_UP_TO_DATE
    assert entry.local_version == "2.0.0"


def test_a_differently_spelled_version_gets_an_explanatory_note(tmp_path, upstream):
    """The jar and the platform label the same release differently, which happens a lot.

    Lithium's jar says ``0.26.2+mc26.3`` while Modrinth says ``mc26.3-0.26.2-fabric``. The
    verdict is decided on the file's identity, so it is right either way — but a report that
    shows two spellings with no comment reads like a discrepancy.
    """
    scenario = Scenario(tmp_path, upstream)
    mod = scenario.add_jar("spelled.jar", id="spelled", version="0.26.2+mc26.3", name="Spelled")
    upstream.add_project(
        FakeProject(
            id="proj-spelled",
            slug="spelled",
            versions=[
                _version("proj-spelled", "s-1", "mc26.3-0.26.2-fabric", mod.sha1),
            ],
        )
    )

    report = run_check(upstream, scenario, tmp_path)

    entry = entry_for(report, "spelled")
    assert entry.status == STATUS_UP_TO_DATE
    note = dict(entry.notes)["note.same_build_other_spelling"]
    assert note["local"] == "0.26.2+mc26.3"
    assert note["upstream"] == "mc26.3-0.26.2-fabric"


def test_no_compatible_build_when_the_project_skips_this_game_version(report):
    entry = entry_for(report, "gamma")
    assert entry.status == STATUS_NO_COMPATIBLE_BUILD
    notes = dict(entry.notes)
    assert "note.no_build_for_game_version" in notes
    assert notes["note.no_build_for_game_version"]["version"] == "26.3"
    assert notes["note.no_build_for_game_version"]["newest"] == "1.0.0"


def test_no_compatible_build_when_the_project_has_no_build_for_this_loader(report):
    """Distinct from the case above, and worth a different message: this mod will never have
    a Fabric build, so "wait for an update" is the wrong advice."""
    entry = entry_for(report, "eta")
    assert entry.status == STATUS_NO_COMPATIBLE_BUILD
    notes = dict(entry.notes)
    assert "note.no_build_for_loader" in notes
    assert "note.no_build_for_game_version" not in notes


def test_curseforge_mod_is_identified_by_fingerprint_and_has_an_update(report):
    entry = entry_for(report, "delta")
    assert entry.status == STATUS_UPDATE_AVAILABLE
    assert entry.platform == "curseforge"
    assert entry.matched_by == "fingerprint"
    assert entry.download_url == "https://edge.example/delta-1.1.0.jar"
    assert "1.1.0" in entry.latest_version
    assert entry.project_url.endswith("/delta")


def test_name_match_is_labelled_as_a_guess(report):
    entry = entry_for(report, "epsilon")
    assert entry.status == STATUS_UPDATE_AVAILABLE
    assert entry.matched_by == "name"
    assert "note.matched_by_name" in dict(entry.notes)


def test_unresolved_when_no_platform_knows_the_jar(report):
    entry = entry_for(report, "zeta")
    assert entry.status == STATUS_UNRESOLVED
    assert entry.platform == ""


def test_ignored_mod_is_not_reported_even_though_an_update_exists(report):
    entry = entry_for(report, "ignored")
    assert entry.status == STATUS_IGNORED
    assert entry not in report.updates


def test_a_jar_that_is_not_a_mod_is_reported_as_such(report):
    entry = entry_for_file(report, "library.jar")
    assert entry.status == STATUS_NOT_A_MOD


def test_report_counts_and_lists(report):
    counts = report.counts()
    assert counts[STATUS_UPDATE_AVAILABLE] == 3  # alpha, delta, epsilon
    assert counts[STATUS_UP_TO_DATE] == 1        # beta
    assert counts[STATUS_NO_COMPATIBLE_BUILD] == 2  # gamma, eta
    assert counts[STATUS_UNRESOLVED] == 3        # zeta, hud, hud-copy
    assert counts[STATUS_NOT_A_MOD] == 1
    assert counts[STATUS_IGNORED] == 1
    assert len(report.entries) == 11
    assert report.total_jars == 11
    assert report.has_updates is True
    assert report.actionable_count == 5


def test_updates_are_sorted_before_everything_else(report):
    statuses = [entry.status for entry in report.sorted_entries()]
    assert statuses[0] == STATUS_UPDATE_AVAILABLE
    assert statuses[-1] in (STATUS_UP_TO_DATE, STATUS_IGNORED)


# --------------------------------------------------------------------------------------
# Advisories — the things that explain breakage better than a version number
# --------------------------------------------------------------------------------------


def test_duplicate_mod_ids_are_reported(report):
    assert "hud" in report.duplicate_ids
    assert report.duplicate_ids["hud"] == ["hud-copy.jar", "hud.jar"]
    assert "advisory.duplicates" in dict(report.advisories)


def test_client_only_mods_are_reported(report):
    keys = dict(report.advisories)
    assert "advisory.client_only" in keys
    assert keys["advisory.client_only"]["count"] == 2


def test_every_identified_mod_states_the_minecraft_range_it_was_built_for(report):
    entry = entry_for(report, "alpha")
    assert "note.declared_mc" in dict(entry.notes)


def test_mc_mismatch_is_flagged_when_a_mod_declares_the_wrong_range(tmp_path, upstream):
    scenario = Scenario(tmp_path, upstream)
    scenario.add_jar(
        "oldmod.jar",
        id="oldmod",
        version="1.0.0",
        name="Old Mod",
        depends={"minecraft": ">=1.20.1 <1.21"},
    )
    report = run_check(upstream, scenario, tmp_path)

    entry = entry_for(report, "oldmod")
    notes = dict(entry.notes)
    assert "note.mc_mismatch" in notes
    assert notes["note.mc_mismatch"]["server"] == "26.3"


def test_unknown_game_version_is_warned_about(tmp_path, upstream):
    scenario = Scenario(tmp_path, upstream)
    scenario.add_jar("solo.jar", id="solo", version="1.0.0", name="Solo")
    context = ServerContext(mc_version=None, mc_version_source="unknown", loader="fabric")

    checker = Checker(make_options(upstream))
    try:
        report = checker.run(scenario.scan(), context, cache_path=None)
    finally:
        checker.close()

    assert "advisory.unknown_mc_version" in dict(report.advisories)


# --------------------------------------------------------------------------------------
# Request budget
# --------------------------------------------------------------------------------------


def test_hashes_are_looked_up_in_a_single_batch(report, upstream):
    assert upstream.count_path("/v2/version_files") == 1
    assert upstream.count_path("/v2/version_files/update") == 1
    assert upstream.count_path("/v2/projects") == 1


def test_per_project_fallbacks_are_only_asked_for_the_mods_that_need_them(report, upstream):
    """Three projects are asked about individually, for two different reasons:

    * ``proj-gamma`` and ``proj-eta`` come from the batched lookup, which cannot say whether
      it found nothing because of the loader or because of the game version;
    * ``proj-epsilon`` comes from the name-match path, which has to fetch the version list to
      learn anything at all.

    What must *not* happen is a per-project request for ``proj-alpha`` or ``proj-beta`` — the
    batch already answered those, and re-asking would be the difference between a handful of
    requests and one per mod.
    """
    project_requests = [
        path for path in upstream.request_paths() if path.startswith("/v2/project/")
    ]
    assert sorted(project_requests) == [
        "/v2/project/proj-epsilon/version",
        "/v2/project/proj-eta/version",
        "/v2/project/proj-gamma/version",
    ]


def test_curseforge_is_queried_with_its_own_batches(report, upstream):
    assert upstream.count_path("/v1/fingerprints") == 1
    assert upstream.count_path("/v1/mods") == 1
    assert upstream.count_path("/v1/mods/{}/files".format(DELTA_CF_MOD_ID)) == 1


def test_curseforge_is_skipped_entirely_without_a_key(tmp_path, upstream):
    scenario = build_scenario(tmp_path, upstream)
    report = run_check(upstream, scenario, tmp_path, curseforge_api_key="")

    assert entry_for(report, "delta").status == STATUS_UNRESOLVED
    assert upstream.count_path("/v1/fingerprints") == 0
    assert "note.curseforge_no_key" in dict(report.upstream_notes)


def test_modrinth_can_be_switched_off(tmp_path, upstream):
    scenario = build_scenario(tmp_path, upstream)
    report = run_check(upstream, scenario, tmp_path, use_modrinth=False)

    assert upstream.count_path("/v2/version_files") == 0
    assert "note.modrinth_disabled_by_config" in dict(report.upstream_notes)
    # CurseForge still resolves what it can.
    assert entry_for(report, "delta").status == STATUS_UPDATE_AVAILABLE


def test_an_unreachable_upstream_is_reported_and_not_fatal(tmp_path, upstream):
    scenario = build_scenario(tmp_path, upstream)
    upstream.fail_all = True

    # retries=0 keeps this test quick: every call would otherwise sit through a backoff and
    # then fail anyway. The retry path itself is covered by its own test.
    report = run_check(upstream, scenario, tmp_path, retries=0)

    keys = dict(report.upstream_notes)
    assert "note.modrinth_unavailable" in keys
    # Every mod is still accounted for, and nothing claims to have an update.
    assert len(report.entries) == 11
    assert report.updates == []


def test_a_rate_limited_request_is_retried(tmp_path, upstream):
    scenario = build_scenario(tmp_path, upstream)
    upstream.scripted_responses = [429]

    report = run_check(upstream, scenario, tmp_path, retries=2)

    assert entry_for(report, "alpha").status == STATUS_UPDATE_AVAILABLE


# --------------------------------------------------------------------------------------
# Upstream failure must not masquerade as a statement about a project
#
# "This project publishes no Fabric build" and "the request failed" would otherwise reach the
# admin as the same sentence, and the first one is an instruction to go and disable a mod.
# --------------------------------------------------------------------------------------


def test_a_failed_batch_lookup_does_not_claim_no_compatible_build(tmp_path, upstream):
    """The worst case: one 503 during the batched lookup used to make the plugin say, about
    every modridth mod at once, that its project has no build for this loader."""
    scenario = build_scenario(tmp_path, upstream)
    upstream.fail_paths = {"/v2/version_files/update"}

    report = run_check(upstream, scenario, tmp_path)

    assert report.by_status(STATUS_NO_COMPATIBLE_BUILD) == [], [
        entry.name for entry in report.by_status(STATUS_NO_COMPATIBLE_BUILD)
    ]
    for mod_id in ("alpha", "beta", "gamma", "eta"):
        entry = entry_for(report, mod_id)
        assert entry.status == STATUS_ERROR, entry.name
        assert "note.upstream_failed" in dict(entry.notes)
        assert entry.error
    assert "note.modrinth_unavailable" in dict(report.upstream_notes)


def test_a_failed_project_lookup_does_not_claim_no_build_for_this_loader(tmp_path, upstream):
    """Same distinction, one level down: the batched call answered, the follow-up did not."""
    scenario = build_scenario(tmp_path, upstream)
    upstream.fail_paths = {
        "/v2/project/proj-gamma/version",
        "/v2/project/proj-eta/version",
    }

    report = run_check(upstream, scenario, tmp_path)

    for mod_id in ("gamma", "eta"):
        entry = entry_for(report, mod_id)
        assert entry.status == STATUS_ERROR, entry.name
        assert "note.upstream_failed" in dict(entry.notes)
    # The mods whose batched answer was fine are untouched.
    assert entry_for(report, "alpha").status == STATUS_UPDATE_AVAILABLE
    assert entry_for(report, "beta").status == STATUS_UP_TO_DATE


def test_a_failed_search_is_not_cached_as_a_negative_verdict(tmp_path, upstream):
    """A transient outage must not become a day-long blind spot.

    Caching "neither platform knows this jar" is only sound when both platforms actually
    answered. When one could not be reached, the record would hide the mod from every
    subsequent check until the TTL expired, long after the outage ended.
    """
    scenario = build_scenario(tmp_path, upstream)
    upstream.fail_paths = {"/v2/search", "/v1/mods/search"}
    cache_path = tmp_path / "resolve-cache.json"

    checker = Checker(make_options(upstream, use_cache=True))
    try:
        report = checker.run(scenario.scan(), SERVER, cache_path=cache_path)
    finally:
        checker.close()

    entry = entry_for(report, "zeta")
    assert entry.status == STATUS_UNRESOLVED
    assert "note.search_incomplete" in dict(entry.notes)

    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    assert payload["records"] == {}, "an unproven verdict was written to the cache"

    # And the same run with the search reachable again resolves the mod.
    upstream.fail_paths = set()
    checker = Checker(make_options(upstream, use_cache=True))
    try:
        second = checker.run(scenario.scan(), SERVER, cache_path=cache_path)
    finally:
        checker.close()
    assert entry_for(second, "epsilon").status == STATUS_UPDATE_AVAILABLE


# --------------------------------------------------------------------------------------
# Result caching
# --------------------------------------------------------------------------------------


def test_unresolved_verdicts_are_cached_between_runs(tmp_path, upstream):
    """Proving a jar is on neither platform is the most expensive answer to reach, and the
    least likely to have changed by the next restart."""
    scenario = build_scenario(tmp_path, upstream)
    cache_path = tmp_path / "config" / "resolve-cache.json"
    options = make_options(upstream, use_cache=True)

    def run():
        checker = Checker(options)
        try:
            return checker.run(scenario.scan(), SERVER, cache_path=cache_path)
        finally:
            checker.close()

    first = run()
    first_searches = upstream.count_path("/v2/search")
    upstream.clear_requests()

    second = run()
    second_searches = upstream.count_path("/v2/search")

    assert first_searches == 4  # epsilon, zeta, hud, hud-copy
    assert second_searches == 1  # only epsilon, which resolves and is therefore not cached
    assert entry_for(second, "zeta").status == STATUS_UNRESOLVED
    assert "note.cached_unresolved" in dict(entry_for(second, "zeta").notes)
    assert entry_for(first, "zeta").status == STATUS_UNRESOLVED

    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    assert payload["version"] == ResolveCache.VERSION
    assert payload["records"]


def test_the_cache_entry_expires_after_the_ttl(tmp_path):
    path = tmp_path / "resolve-cache.json"
    cache = ResolveCache(path, ttl_hours=1.0)
    cache.put("a" * 40, {"resolved": False})
    cache.save()

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["records"]["a" * 40]["at"] > 0

    # Age the record by two hours and reload.
    payload["records"]["a" * 40]["at"] -= 2 * 3600
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert ResolveCache(path, ttl_hours=1.0).get("a" * 40) is None
    assert ResolveCache(path, ttl_hours=72.0).get("a" * 40) is not None


def test_zero_ttl_means_never_expire(tmp_path):
    """``use_resolve_cache`` is what switches the cache off; a zero TTL must not silently do
    the same thing while looking like it does something."""
    path = tmp_path / "resolve-cache.json"
    cache = ResolveCache(path, ttl_hours=24.0)
    cache.put("a" * 40, {"resolved": False})
    cache.save()

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["records"]["a" * 40]["at"] = 0.0
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert ResolveCache(path, ttl_hours=0.0).get("a" * 40) is not None


def test_cache_is_disabled_cleanly(tmp_path):
    cache = ResolveCache(tmp_path / "unused.json", ttl_hours=24, enabled=False)
    cache.put("a" * 40, {"resolved": False})
    assert cache.get("a" * 40) is None
    cache.save()
    assert not (tmp_path / "unused.json").exists()


@pytest.mark.parametrize("as_string", [False, True])
def test_resolve_cache_accepts_a_string_path(tmp_path, as_string):
    """The plugin hands the cache an ``os.path.join`` result, i.e. a ``str``.

    This is a regression test with a specific history: the unit tests only ever passed a
    ``Path``, so the ``str`` form went untested, and every method on the cache uses the
    pathlib API — ``is_file``, ``parent``, ``with_suffix``. A real first check therefore died
    with ``AttributeError: 'str' object has no attribute 'is_file'`` on the shipped default
    config, which enables the cache. Both forms are now valid and both are exercised.
    """
    path = tmp_path / "resolve-cache.json"
    argument = str(path) if as_string else path

    cache = ResolveCache(argument, ttl_hours=24)
    assert cache.enabled
    cache.put("b" * 40, {"resolved": False})
    cache.save()

    assert path.is_file(), "the cache did not reach the filesystem"
    reloaded = ResolveCache(argument, ttl_hours=24)
    record = reloaded.get("b" * 40)
    assert record is not None and record["resolved"] is False


def test_resolve_cache_with_none_is_disabled(tmp_path):
    cache = ResolveCache(None, ttl_hours=24)
    assert cache.enabled is False
    assert cache.path is None
    cache.save()  # must not raise
    cache.put("c" * 40, {"resolved": False})
    assert cache.get("c" * 40) is None


def test_a_corrupt_cache_file_is_ignored(tmp_path):
    path = tmp_path / "resolve-cache.json"
    path.write_text("{ not json", encoding="utf-8")
    assert ResolveCache(path, ttl_hours=24).get("a" * 40) is None


# --------------------------------------------------------------------------------------
# Serialisation and rendering
# --------------------------------------------------------------------------------------


def test_report_serialises_to_json(report):
    payload = json.loads(report.to_json())

    assert payload["server"]["mc_version"] == "26.3"
    assert payload["counts"][STATUS_UPDATE_AVAILABLE] == 3
    assert payload["actionable_count"] == 5
    assert payload["mods_directory"].endswith("mods")
    assert payload["duplicate_ids"] == {"hud": ["hud-copy.jar", "hud.jar"]}

    alpha = next(item for item in payload["entries"] if item["mod_id"] == "alpha")
    assert alpha["status"] == STATUS_UPDATE_AVAILABLE
    assert alpha["local_version"] == "1.0.0"
    assert alpha["download_url"].endswith("alpha-1.1.0.jar")
    assert any(note["key"] == "note.declared_mc" for note in alpha["notes"])


@pytest.mark.parametrize("language", ["en_us", "zh_cn"])
def test_summary_and_full_listing_render_in_both_languages(report, language):
    tr = make_translator(language)
    summary = render_summary(report, tr)
    full = render_full(report, tr)

    assert summary
    assert any("Alpha" in line for line in summary)
    assert any("1.0.0" in line and "1.1.0" in line for line in summary)
    assert any("Alpha" in line for line in full)
    assert any("library.jar" in line for line in full)
    # No raw keys leaked through, which is what a missing translation looks like.
    assert not [line for line in full if line.strip().startswith(("note.", "report.", "line."))]


def test_summary_says_so_when_there_is_nothing_to_do(tmp_path, upstream):
    scenario = Scenario(tmp_path, upstream)
    beta = scenario.add_jar("beta.jar", id="beta", version="2.0.0", name="Beta")
    upstream.add_project(
        FakeProject(
            id="proj-beta",
            slug="beta",
            versions=[_version("proj-beta", "b", "2.0.0", beta.sha1)],
        )
    )
    report = run_check(upstream, scenario, tmp_path)

    summary = render_summary(report, make_translator("en_us"))
    assert any("No updates found" in line for line in summary)
    assert report.has_updates is False


@pytest.mark.parametrize("language", ["en_us", "zh_cn"])
def test_the_summary_separates_not_downloaded_from_waiting_to_install(language):
    """Two groups, and a mod is in one or the other — never both, never neither.

    The distinction is the whole point: the first group needs fetching, the second needs
    copying into ``mods/``. Merged into a single "has an update" list, an admin re-reads mods
    they fetched yesterday and cannot tell whether the download worked.
    """
    tr = make_translator(language)
    report = Report(
        generated_at="2026-01-01T00:00:00+00:00",
        server=SERVER,
        mods_directory="server/mods",
        download_folder="config/mod_update_checker/downloads",
        entries=[
            UpdateEntry(mod_id="alpha", name="Alpha", file_name="alpha.jar",
                        local_version="1.0.0", latest_version="1.1.0",
                        status=STATUS_UPDATE_AVAILABLE),
            UpdateEntry(mod_id="beta", name="Beta", file_name="beta.jar",
                        local_version="1.0.0", latest_version="1.1.0",
                        status=STATUS_AWAITING_INSTALL),
            UpdateEntry(mod_id="gamma", name="Gamma", file_name="gamma.jar",
                        local_version="2.0.0", status=STATUS_UP_TO_DATE),
        ],
    )

    summary = render_summary(report, tr)
    body = "\n".join(summary)

    assert "Alpha" in body, "the mod still needing a download is listed"
    assert "Beta" in body, "the fetched mod is listed too"
    assert "downloads" in body, "the folder is named, or 'ready to install' is a dead end"

    # Each mod appears once, under its own heading.
    assert body.count("Beta") == 1
    assert body.count("Alpha") == 1

    # Ordering: the mod that still needs fetching comes before the one that is ready.
    assert body.index("Alpha") < body.index("Beta")

    # The tally accounts for both, so the numbers add up to the number of jars.
    tally = next(line for line in summary if "1.1.0" not in line and "jar" in line.lower()
                 and "Alpha" not in line and "Beta" not in line)
    assert "1" in tally

    # And the two are different statuses in the machine-readable report.
    counts = report.counts()
    assert counts[STATUS_UPDATE_AVAILABLE] == 1
    assert counts[STATUS_AWAITING_INSTALL] == 1
    assert report.actionable_count == 2


def test_a_report_with_only_a_pending_install_is_still_worth_reporting(tmp_path, upstream):
    """No new updates, but the admin has an outstanding step — that is not "nothing to do"."""
    tr = make_translator("en_us")
    report = Report(
        generated_at="2026-01-01T00:00:00+00:00",
        server=SERVER,
        mods_directory="server/mods",
        entries=[
            UpdateEntry(mod_id="beta", name="Beta", file_name="beta.jar",
                        local_version="1.0.0", latest_version="1.1.0",
                        status=STATUS_AWAITING_INSTALL),
        ],
    )

    summary = "\n".join(render_summary(report, tr))
    assert "Beta" in summary
    assert "No updates found" not in summary, (
        "a pending install is not the same as nothing to do"
    )
    assert report.has_updates is True
    assert report.actionable_count == 1


# --------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Fabric API", "fabricapi"),
        ("fabric-api", "fabricapi"),
        ("fabric_api", "fabricapi"),
        ("FABRIC.API", "fabricapi"),
        ("", ""),
    ],
)
def test_normalise_name(text, expected):
    assert normalise_name(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("2026-01-01T00:00:00Z", 1767225600.0),
        ("2026-01-01T00:00:00.000Z", 1767225600.0),
        ("2026-01-01T08:00:00+08:00", 1767225600.0),
        # Different offsets for the same instant must compare equal, which is the whole point
        # of parsing instead of comparing the strings.
        ("2026-03-01T00:00:00Z", 1772323200.0),
        ("", None),
        ("not a date", None),
        ("2026-13-45T99:99:99Z", None),
    ],
)
def test_parse_timestamp(text, expected):
    from mod_update_checker.checker import _parse_timestamp

    result = _parse_timestamp(text)
    if expected is None:
        assert result is None
    else:
        assert result == expected


def test_timestamps_with_different_offsets_compare_correctly():
    """A lexical comparison would order these wrongly; a parsed one does not."""
    from mod_update_checker.checker import _parse_timestamp

    earlier = _parse_timestamp("2026-01-01T23:00:00-05:00")   # 2026-01-02T04:00Z
    later = _parse_timestamp("2026-01-02T01:00:00Z")          # 2026-01-02T01:00Z
    assert earlier > later, "the -05:00 stamp is the later instant"
    # The raw strings would have said the opposite.
    assert "2026-01-01T23:00:00-05:00" < "2026-01-02T01:00:00Z"


def test_local_ahead_is_reported_as_such(tmp_path, upstream):
    """A locally built jar newer than anything published is not an error.

    The local file's bytes are deliberately *not* registered upstream: if they were, the
    honest answer would be "up to date", because there is nothing newer to install. The
    local-ahead verdict only exists for a jar that had to be identified some other way and
    whose version number is ahead of everything on the platform.
    """
    scenario = Scenario(tmp_path, upstream)
    scenario.add_jar("dev.jar", id="dev", version="9.9.9", name="Dev")
    upstream.add_project(
        FakeProject(
            id="proj-dev",
            slug="dev",
            title="Dev Mod",
            versions=[_version("proj-dev", "dev-1", "1.0.0", "c" * 40)],
        )
    )

    report = run_check(upstream, scenario, tmp_path)

    entry = entry_for(report, "dev")
    assert entry.status == STATUS_LOCAL_AHEAD
    assert entry.matched_by == "name"
    assert entry.local_version == "9.9.9"
    assert entry.latest_version == "1.0.0"


def test_a_broken_jar_does_not_abort_the_run(tmp_path, upstream):
    scenario = Scenario(tmp_path, upstream)
    good = scenario.add_jar("good.jar", id="good", version="1.0.0", name="Good")
    (scenario.directory / "broken.jar").write_bytes(b"definitely not a zip")
    upstream.add_project(
        FakeProject(
            id="proj-good",
            slug="good",
            versions=[_version("proj-good", "g1", "1.0.0", good.sha1)],
        )
    )

    report = run_check(upstream, scenario, tmp_path)

    assert entry_for(report, "good").status == STATUS_UP_TO_DATE
    broken = [entry for entry in report.entries if entry.file_name == "broken.jar"]
    assert len(broken) == 1
    assert broken[0].status == STATUS_ERROR


def test_report_entries_are_always_one_per_jar(tmp_path, upstream):
    """Two jars of one mod must produce two entries, or the duplicate could never be fixed."""
    scenario = Scenario(tmp_path, upstream)
    scenario.add_jar("sodium-0.5.8.jar", id="sodium", version="0.5.8", name="Sodium")
    scenario.add_jar("sodium-0.5.9.jar", id="sodium", version="0.5.9", name="Sodium")

    report = run_check(upstream, scenario, tmp_path)

    assert len(report.entries) == 2
    assert len(report.unidentified) == 2
