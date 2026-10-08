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
import re
import threading
import time

import pytest

from mod_update_checker.checker import (
    _MAX_WORKERS,
    _pool_size,
    CheckOptions,
    Checker,
    ResolveCache,
    normalise_name,
)
from mod_update_checker.i18n import make_translator
from mod_update_checker.report import (
    CHAT_PAGE_LINES,
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
    entry_detail_rows,
    render_detail,
    render_entry_lines,
    render_full,
    render_index_body,
    render_summary,
)
from mod_update_checker.scanner import scan_jar, scan_mods
from mod_update_checker.serverinfo import ServerContext

from fake_upstream import (
    FakeFile,
    FakeProject,
    FakeUpstream,
    FakeVersion,
)
from support import fabric_metadata, write_jar

SERVER = ServerContext(mc_version="26.3", mc_version_source="config", loader="fabric")



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

    # 5. Only findable by name: its bytes are not on Modrinth at all.
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


def test_an_ignored_mod_costs_no_requests_at_all(tmp_path, upstream):
    """Excluding a mod must mean "do not look it up", not "hide the answer".

    This is the difference that matters to someone who maintains a mod they never want updated,
    or who has pinned one on purpose. Looking it up anyway would still spend the request, still
    count against the budget a 200-mod server is trying to stay inside, and still depend on the
    resolve cache being right — none of which was asked for.

    Set up so the answer is unambiguous: one jar, excluded, whose bytes are *not* registered on
    Modrinth but whose project exists there by name. Left to itself it would take a hash lookup
    and then a name search, so any request at all means the exclusion leaked.
    """
    scenario = Scenario(tmp_path, upstream)
    scenario.add_jar("pinned.jar", id="pinned", version="1.0.0", name="Pinned Mod")
    upstream.add_project(
        FakeProject(
            id="proj-pinned",
            slug="pinned",
            title="Pinned Mod",
            versions=[
                # Deliberately not the jar's own sha1: a hash match would resolve it without a
                # search, and then the search could not be used as the signal.
                _version("proj-pinned", "p-1", "1.0.0", "a" * 40),
                _version("proj-pinned", "p-2", "2.0.0", "b" * 40, date="2026-02-01T00:00:00Z"),
            ],
        )
    )

    report = run_check(upstream, scenario, tmp_path, ignored_mods=["pinned"])

    assert entry_for(report, "pinned").status == STATUS_IGNORED
    assert upstream.request_paths() == [], upstream.request_paths()


def test_ignoring_matches_the_mod_id_the_file_name_and_the_stem(tmp_path, upstream):
    """All three spellings an admin might have in front of them, matched loosely.

    Written so a lookup that only checked one of the three would fail: one jar is named for its
    id, one has a file name that differs from its id, and the third is matched on its stem. The
    configured values are also spelled differently from the files (``BY-ID`` against
    ``by-id.jar``), because the normalisation is the part that is easy to get wrong.
    """
    scenario = Scenario(tmp_path, upstream)
    by_id = scenario.add_jar("by-id.jar", id="by-id", version="1.0.0", name="By Id")
    by_file = scenario.add_jar("Different-File.jar", id="byfile", version="1.0.0", name="By File")
    by_stem = scenario.add_jar("by-stem.jar", id="bystem", version="1.0.0", name="By Stem")
    for jar in (by_id, by_file, by_stem):
        upstream.add_project(
            FakeProject(
                id="proj-" + jar.file_name,
                slug=jar.file_name,
                title=jar.file_name,
                versions=[
                    _version("proj-" + jar.file_name, "x-1", "1.0.0", jar.sha1),
                    _version("proj-" + jar.file_name, "x-2", "2.0.0", jar.sha1 + "0",
                             date="2026-02-01T00:00:00Z"),
                ],
            )
        )

    report = run_check(
        upstream, scenario, tmp_path,
        ignored_mods=["BY-ID", "different-file", "by stem"],
    )

    statuses = {(entry.mod_id or entry.file_name): entry.status for entry in report.entries}
    assert statuses.get("by-id") == STATUS_IGNORED, "the mod id spelling did not match"
    assert statuses.get("byfile") == STATUS_IGNORED, "the file name spelling did not match"
    assert statuses.get("bystem") == STATUS_IGNORED, "the stem spelling did not match"
    assert upstream.request_paths() == [], "an ignored mod was still looked up"


def test_an_ignored_mod_is_still_listed_so_the_admin_can_see_the_setting_worked(tmp_path, upstream):
    """Silently dropping it would make a typo in the config invisible.

    "I excluded it and it vanished" and "I excluded it and I mistyped the name" have to look
    different, so the entry survives in the report with an explicit status.
    """
    scenario = Scenario(tmp_path, upstream)
    scenario.add_jar("kept.jar", id="kept", version="1.0.0", name="Kept")

    report = run_check(upstream, scenario, tmp_path, ignored_mods=["kept"])

    assert [entry.mod_id for entry in report.entries] == ["kept"]
    assert report.entries[0].status == STATUS_IGNORED
    assert report.actionable_count == 0


def test_an_ignored_mod_is_not_downloaded(tmp_path, upstream, monkeypatch):
    """The exclusion has to hold for the download stage too.

    Downloading is a different code path from checking, and a mod someone pinned on purpose is
    exactly the one that must not have a new jar fetched for it.
    """
    scenario = Scenario(tmp_path, upstream)
    jar = scenario.add_jar("pinned.jar", id="pinned", version="1.0.0", name="Pinned")
    upstream.add_project(
        FakeProject(
            id="proj-pinned",
            slug="pinned",
            title="Pinned",
            versions=[
                _version("proj-pinned", "p-1", "1.0.0", jar.sha1),
                _version("proj-pinned", "p-2", "2.0.0", "c" * 40, date="2026-02-01T00:00:00Z",
                         filename="pinned-2.0.0.jar"),
            ],
        )
    )

    report = run_check(upstream, scenario, tmp_path, ignored_mods=["pinned"])

    from mod_update_checker.downloads import classify_downloaded

    assert report.updates == [], "an ignored mod was queued for download"
    # And it is not reported as "waiting to be installed" either — it is simply out of scope.
    moved = classify_downloaded(report.entries, tmp_path / "downloads", ledger=None)
    assert moved == []


def test_report_counts_and_lists(report):
    counts = report.counts()
    assert counts[STATUS_UPDATE_AVAILABLE] == 2  # alpha, epsilon
    assert counts[STATUS_UP_TO_DATE] == 1        # beta
    assert counts[STATUS_NO_COMPATIBLE_BUILD] == 2  # gamma, eta
    assert counts[STATUS_UNRESOLVED] == 3        # zeta, hud, hud-copy
    assert counts[STATUS_NOT_A_MOD] == 1
    assert counts[STATUS_IGNORED] == 1
    assert len(report.entries) == 10
    assert report.total_jars == 10
    assert report.has_updates is True
    assert report.actionable_count == 4


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


def test_an_unreachable_upstream_is_reported_and_not_fatal(tmp_path, upstream):
    scenario = build_scenario(tmp_path, upstream)
    upstream.fail_all = True

    # retries=0 keeps this test quick: every call would otherwise sit through a backoff and
    # then fail anyway. The retry path itself is covered by its own test.
    report = run_check(upstream, scenario, tmp_path, retries=0)

    keys = dict(report.upstream_notes)
    assert "note.modrinth_unavailable" in keys
    # Every mod is still accounted for, and nothing claims to have an update.
    assert len(report.entries) == 10
    assert report.updates == []


def test_a_rate_limited_request_is_retried(tmp_path, upstream):
    scenario = build_scenario(tmp_path, upstream)
    upstream.scripted_responses = [429]

    report = run_check(upstream, scenario, tmp_path, retries=2)

    assert entry_for(report, "alpha").status == STATUS_UPDATE_AVAILABLE


# --------------------------------------------------------------------------------------
# The lookups that scale with the mod count are overlapped
#
# Two stages talk to the upstream once per mod, and both are latency-bound rather than
# bandwidth-bound: a jar whose bytes are not published anywhere costs a search plus a version
# list, and a jar that is published but has no build for this loader costs a version list. On a
# server whose mods were built from source, that is most of the folder, so asking one at a time
# is the difference between a check measured in seconds and one measured in minutes.
#
# These tests watch how many lookups are in flight rather than how long the run took, so they
# assert the property that is actually wanted and do not depend on how loaded the machine is.
# --------------------------------------------------------------------------------------


def _leftovers(scenario, count, prefix="leftover"):
    """Jars that no hash lookup can answer, so every one of them reaches the name-search stage."""
    for index in range(count):
        scenario.add_jar(
            "{}{}.jar".format(prefix, index),
            id="{}{}".format(prefix, index),
            version="1.0.0",
            name="{} {} ".format(prefix.capitalize(), index).strip(),
        )


def _watch_concurrency(monkeypatch, attribute):
    """Wrap ``Checker.<attribute>`` so the highest number of simultaneous calls is recorded.

    The sleep stands in for a round trip. Without it the calls would be over before the next
    one started and there would be nothing to observe, whatever the threading did.
    """
    state = {"in_flight": 0, "peak": 0}
    lock = threading.Lock()
    real = getattr(Checker, attribute)

    def watching(self, *args, **kwargs):
        with lock:
            state["in_flight"] += 1
            state["peak"] = max(state["peak"], state["in_flight"])
        try:
            time.sleep(0.05)
            return real(self, *args, **kwargs)
        finally:
            with lock:
                state["in_flight"] -= 1

    monkeypatch.setattr(Checker, attribute, watching)
    return state


def test_the_name_search_stage_asks_its_lookups_concurrently(tmp_path, upstream, monkeypatch):
    """Eight mods that only a name search can identify, with four workers to spend on them."""
    scenario = Scenario(tmp_path, upstream)
    _leftovers(scenario, 8)
    state = _watch_concurrency(monkeypatch, "_match_on_modrinth")

    run_check(upstream, scenario, tmp_path, workers=4)

    assert state["peak"] > 1, "the name searches were run one after another"


def test_the_name_search_does_not_open_more_threads_than_it_has_work(tmp_path, upstream,
                                                                    monkeypatch):
    """Two mods and eight workers must not open eight threads to make two calls each."""
    scenario = Scenario(tmp_path, upstream)
    _leftovers(scenario, 2)
    state = _watch_concurrency(monkeypatch, "_match_on_modrinth")

    run_check(upstream, scenario, tmp_path, workers=8)

    assert state["peak"] <= 2, state


def test_overlapping_the_name_search_changes_no_verdict(tmp_path, upstream):
    """The stage writes one entry per mod and a lock-protected cache, so order must not matter.

    Worth asserting by running it both ways rather than trusting the argument: a shared mutable
    default or a stray instance attribute would show up here as a difference and nowhere else.
    """
    scenario = Scenario(tmp_path, upstream)
    _leftovers(scenario, 6)
    # Half of them are knowable by name, so the stage produces both a match and a miss.
    for index in range(0, 6, 2):
        upstream.add_project(
            FakeProject(
                id="proj-leftover{}".format(index),
                slug="leftover{}".format(index),
                title="Leftover {}".format(index),
                versions=[_version("proj-leftover{}".format(index),
                                   "lv{}".format(index), "2.0.0", "e" * 40)],
            )
        )

    def summary(workers):
        report = run_check(upstream, scenario, tmp_path, workers=workers)
        return sorted(
            (entry.file_name, entry.status, entry.latest_version, entry.matched_by)
            for entry in report.entries
        )

    assert summary(1) == summary(8)


def test_the_worker_pool_is_capped_and_never_empty():
    """Both bounds protect something different, so both are pinned.

    The cap keeps a mistyped ``network.concurrent_requests`` from opening hundreds of
    connections to a public API; the ``count`` bound keeps a three-mod server from starting
    eight threads to make three calls; and the floor keeps a nonsense zero from deadlocking
    the run on a pool that can never execute anything.
    """
    assert _pool_size(0, 10) == 1
    assert _pool_size(-5, 10) == 1
    assert _pool_size(4, 100) == 4
    assert _pool_size(4, 2) == 2
    assert _pool_size(99, 100) == _MAX_WORKERS
    assert _pool_size(1, 100) == 1


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
    assert payload["counts"][STATUS_UPDATE_AVAILABLE] == 2
    assert payload["actionable_count"] == 4
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


def test_the_full_listing_does_not_claim_rows_were_held_back():
    """The summary is capped and says how many rows it left out; the full one is not capped.

    ``render_full`` asks for the summary's headings with ``max_updates=0``, and the cap used
    to be applied as "more than zero entries were held back" — so the text report opened with
    ``... and 13 more`` directly above the section listing all thirteen, with none of them
    shown above it. The number was the whole list, presented as a remainder.
    """
    tr = make_translator("en_us")
    entries = [
        UpdateEntry(mod_id="mod{}".format(index), name="Mod {}".format(index),
                    file_name="mod{}.jar".format(index), local_version="1.0",
                    latest_version="2.0", status=STATUS_UPDATE_AVAILABLE)
        for index in range(13)
    ]
    report = Report(
        generated_at="2026-01-01T00:00:00+00:00",
        server=SERVER,
        mods_directory="server/mods",
        entries=entries,
    )

    summary = "\n".join(render_summary(report, tr))
    full = "\n".join(render_full(report, tr))

    hidden_line = tr("report.and_more", count=len(entries) - 12)
    assert hidden_line in summary, "the summary must say what it left out"
    assert hidden_line not in full, "nothing is left out of the full listing"
    assert tr("report.and_more", count=len(entries)) not in full
    # And every mod is there, which is what makes the line above a lie rather than a warning.
    assert all("Mod {}".format(index) in full for index in range(13))


# --------------------------------------------------------------------------------------
# What a listing looks like
#
# These are not cosmetic tests. The console is where a check is read, and the first version of
# this listing put two URLs on every line: a hundred and seventy characters, wrapping several
# times in a terminal and unreadable in the game's chat box. Readability is the feature; these
# are the assertions that keep it.
# --------------------------------------------------------------------------------------

#: A CDN url as Modrinth actually publishes them — long, and made of ids.
_CDN_URL = ("https://cdn.modrinth.com/data/AABBCCDD/versions/ZZYYXXWW/"
            "some-mod-1.1.0%2Bmc26.3-fabric.jar")

#: Longest line a listing may produce. The previous layout ran to 170+ characters and wrapped
#: three times in a normal terminal; a typical row now measures about 66. This is the ceiling,
#: not the target — it is deliberately loose enough for a long mod name, a version string like
#: ``1.19.2-0.5.3+build.31``, and a long project slug all at once.
_LINE_BUDGET = 100


def _entry_with_links(mod_id, status, *, name=None, local="1.0.0", latest="1.1.0", by="hash"):
    """An entry carrying both URLs, which is what a real run produces."""
    entry = UpdateEntry(
        mod_id=mod_id,
        name=name or mod_id.title(),
        file_name=mod_id + ".jar",
        local_version=local,
        latest_version=latest,
        status=status,
        matched_by=by,
    )
    entry.project_url = "https://modrinth.com/mod/" + mod_id
    entry.download_url = _CDN_URL
    return entry


@pytest.mark.parametrize("render", ["summary", "full"])
def test_no_rendering_carries_the_download_url(render):
    """The download link belongs in the JSON, not in a line somebody has to read.

    It is ~85 characters of opaque ids, it is not clickable in a console, and the project page
    answers the same question ("where is the new version") in 37. Anything automating the fetch
    reads ``last_report.json``, which still carries it.

    Asserted against the *entry's own* download url rather than a pattern, so it cannot be
    fooled by a file name that happens to look like one — ``sodium.jar`` is how the mod is
    identified and belongs on the line.
    """
    tr = make_translator("zh_cn")
    linked = _entry_with_links("alpha", STATUS_UPDATE_AVAILABLE)
    # An unresolved jar is the case whose line shows the file name, so it is the one that
    # proves the two are not being confused with each other.
    unresolved = UpdateEntry(mod_id="", name="Mystery", file_name="mystery.jar",
                             status=STATUS_UNRESOLVED)
    report = Report(
        generated_at="2026-01-01T00:00:00+00:00",
        server=SERVER,
        mods_directory="server/mods",
        entries=[linked, unresolved],
    )

    lines = render_summary(report, tr) if render == "summary" else render_full(report, tr)
    body = "\n".join(lines)

    assert linked.project_url in body, "the project page is the replacement, so it has to be there"
    assert linked.download_url not in body
    assert not [line for line in lines if "cdn." in line], body
    # The file name is not the url and must survive in the full listing: it is what the jar is
    # called on disk, and it is the only identifier an unresolved jar has.
    if render == "full":
        assert "mystery.jar" in body


@pytest.mark.parametrize("render", ["summary", "full"])
def test_a_line_carries_at_most_one_link(render):
    """The structural guarantee, and the one that keeps the format from creeping back.

    Whatever gets added to a row later, it may not be another url: one link per line is what
    makes a list scannable, and a width budget alone would still allow two short ones.
    """
    tr = make_translator("zh_cn")
    report = Report(
        generated_at="2026-01-01T00:00:00+00:00",
        server=SERVER,
        mods_directory="server/mods",
        download_folder="config/mod_update_checker/downloads",
        entries=[
            _entry_with_links("sodium", STATUS_UPDATE_AVAILABLE),
            _entry_with_links("ferrite-core", STATUS_UPDATE_AVAILABLE,
                              name="FerriteCore", latest="1.19.2-0.5.3+build.31"),
            _entry_with_links("fabric-api", STATUS_AWAITING_INSTALL, name="Fabric API"),
            _entry_with_links("blocked", STATUS_NO_COMPATIBLE_BUILD, name="Blocked Mod"),
            _entry_with_links("iris", STATUS_UP_TO_DATE, name="Iris"),
        ],
    )

    lines = render_summary(report, tr) if render == "summary" else render_full(report, tr)
    crowded = [line for line in lines if line.count("http") > 1]
    assert crowded == [], "more than one link on a line: {}".format(crowded)


@pytest.mark.parametrize("render", ["summary", "full"])
def test_every_rendered_line_fits_in_a_console_line(render):
    """The width budget, checked against entries carrying both urls and a long version.

    Uses the worst case on purpose: a long mod name, a version string the length of
    ``1.19.2-0.5.3+build.31``, and both urls populated. If a url is ever added back to the
    rendering, this is what fails.
    """
    tr = make_translator("zh_cn")
    report = Report(
        generated_at="2026-01-01T00:00:00+00:00",
        server=SERVER,
        mods_directory="server/mods",
        download_folder="config/mod_update_checker/downloads",
        entries=[
            _entry_with_links("sodium", STATUS_UPDATE_AVAILABLE),
            _entry_with_links("ferrite-core", STATUS_UPDATE_AVAILABLE,
                              name="FerriteCore", latest="1.19.2-0.5.3+build.31"),
            _entry_with_links("fabric-api", STATUS_AWAITING_INSTALL,
                              name="Fabric API", latest="0.120.0"),
            _entry_with_links("blocked", STATUS_NO_COMPATIBLE_BUILD, name="Blocked Mod"),
            _entry_with_links("iris", STATUS_UP_TO_DATE, name="Iris"),
            _entry_with_links("local-dev", STATUS_LOCAL_AHEAD, name="Local Dev Build"),
        ],
    )

    lines = render_summary(report, tr) if render == "summary" else render_full(report, tr)
    over = ["{} ({})".format(line, len(line)) for line in lines if len(line) > _LINE_BUDGET]
    assert over == [], "lines past the budget://n" + "\n".join(over)


def test_project_links_are_offered_only_where_visiting_the_page_is_the_next_step(report):
    """A link on every row is the same as a link on none: neither tells you what to do.

    ``update_available`` and ``no_compatible_build`` end with somebody opening a web page.
    ``awaiting_install`` does not — the file is already on disk — and leaving it out is what
    makes the two groups distinguishable at a glance.
    """
    tr = make_translator("zh_cn")
    entries = [
        _entry_with_links("alpha", STATUS_UPDATE_AVAILABLE),
        _entry_with_links("beta", STATUS_AWAITING_INSTALL),
        _entry_with_links("blocked", STATUS_NO_COMPATIBLE_BUILD),
    ]
    rendered = "\n".join(render_entry_lines(entries, tr, verbose=False))

    for mod_id in ("alpha", "blocked"):
        assert "modrinth.com/mod/" + mod_id in rendered
    assert "modrinth.com/mod/beta" not in rendered, "a fetched build needs no link"


def test_a_listing_of_links_starts_them_in_one_column():
    """Ragged links are the thing that makes a list of urls hard to scan."""
    tr = make_translator("zh_cn")
    entries = [
        _entry_with_links("a", STATUS_UPDATE_AVAILABLE, name="A"),
        _entry_with_links("longer-mod-id", STATUS_UPDATE_AVAILABLE, name="A Considerably Longer Name"),
        _entry_with_links("mid", STATUS_UPDATE_AVAILABLE, name="Mid Length"),
    ]
    lines = render_entry_lines(entries, tr, verbose=False)

    columns = [line.index("https://") for line in lines]
    assert len(set(columns)) == 1, columns


def test_a_full_listing_still_says_which_status_each_row_is():
    """The link might be gone, but the rows here are of all kinds and must stay tellable apart."""
    tr = make_translator("zh_cn")
    entries = [
        _entry_with_links("alpha", STATUS_UPDATE_AVAILABLE),
        _entry_with_links("local-dev", STATUS_LOCAL_AHEAD, name="Local Dev Build"),
        _entry_with_links("mystery", STATUS_UNRESOLVED, name="Mystery"),
    ]
    body = "\n".join(render_entry_lines(entries, tr, verbose=True))

    for label in ("可更新", "本地版本更新", "无法定位上游"):
        assert label in body, label


def test_a_name_match_is_flagged_but_the_normal_case_is_not():
    """The caveat has to stand out, which it cannot do if every row carries boilerplate."""
    tr = make_translator("zh_cn")
    entries = [
        _entry_with_links("exact", STATUS_UPDATE_AVAILABLE, by="hash"),
        _entry_with_links("guessed", STATUS_UPDATE_AVAILABLE, by="name"),
    ]
    lines = render_entry_lines(entries, tr, verbose=True)

    assert "按名称匹配" in lines[1], lines[1]
    assert "按文件哈希" not in lines[0], lines[0]


# --------------------------------------------------------------------------------------
# The listing, the numbering, and the detail view
#
# The listing used to print each mod's project page, download url and notes inline — five to
# seven lines per mod, which ran past a page of chat on a server with a handful of mods. It is
# a numbered index now, with the detail one click away, and the properties below are what make
# that safe: the number has to identify a mod, and the reply has to fit.
# --------------------------------------------------------------------------------------


def _index_report(entries):
    return Report(
        generated_at="2026-01-01T00:00:00+00:00",
        server=SERVER,
        mods_directory="server/mods",
        download_folder="config/mod_update_checker/downloads",
        entries=list(entries),
    )


def _many(actionable=0, up_to_date=0):
    entries = [
        _entry_with_links("act{:03d}".format(i), STATUS_UPDATE_AVAILABLE,
                          name="Action Mod {:03d}".format(i))
        for i in range(actionable)
    ]
    entries += [
        _entry_with_links("up{:03d}".format(i), STATUS_UP_TO_DATE, name="Fresh Mod {:03d}".format(i))
        for i in range(up_to_date)
    ]
    return entries


def test_the_listing_fits_a_page_whatever_the_server_holds():
    """The property the whole change exists for.

    A reply that has to be scrolled to find the row you need is the reply that gets closed, so
    the budget holds at every size — including the two extremes where the actionable set alone
    overflows, and where nothing is actionable at all.
    """
    tr = make_translator("zh_cn")
    for actionable, up_to_date in ((3, 4), (40, 160), (0, 200), (40, 0), (0, 0)):
        report = _index_report(_many(actionable, up_to_date))
        lines = render_index_body(report, tr)
        assert len(lines) <= CHAT_PAGE_LINES, (actionable, up_to_date, len(lines))


def test_the_actionable_mods_are_never_the_ones_left_out():
    """Priority, not just a cap: what needs doing survives the truncation.

    The sort order already puts them first, and this is what stops a future reordering — say,
    alphabetical — from quietly pushing the one mod that needs attention off the page.
    """
    tr = make_translator("zh_cn")
    report = _index_report(_many(actionable=40, up_to_date=160))
    body = "\n".join(render_index_body(report, tr))

    assert "Action Mod 000" in body
    # 13 rows fit at this budget; every one of them is an actionable mod.
    assert "Action Mod 012" in body
    assert "Fresh Mod" not in body


def test_a_truncated_listing_says_how_many_it_held_back():
    """A silent cap is worse than no cap: the reader cannot tell a short list from a cut one.

    The count is read out of the line and compared with the number of mods that are actually
    missing from the body. Asserting on the *text* would not do: an earlier version printed
    "另有 -9 个未显示", and the loose check written for it ("no ``-`` in the body") also
    matched the version arrows in every row, so it could only ever fail — or, worse, pass for
    the wrong reason.
    """
    tr = make_translator("zh_cn")
    report = _index_report(_many(actionable=40, up_to_date=160))
    body = "\n".join(render_index_body(report, tr))

    assert "另有" in body and "未显示" in body
    match = re.search(r"另有\s+(\d+)\s+个未显示", body)
    assert match is not None, body
    omitted = int(match.group(1))
    # Every mod is either printed as a row or counted in that number, never both and never
    # neither — which is the arithmetic the line is claiming. The row shape is matched rather
    # than "starts with a bracket": the header opens with the ``[Mod Update Checker]`` badge,
    # so a looser test counted it as a row.
    printed = [line for line in body.splitlines() if re.match(r"^  \[\s*\d+\] ", line)]
    assert omitted > 0
    assert len(printed) + omitted == len(report.entries)


def test_the_number_in_the_listing_identifies_the_mod_it_looks_up():
    """The click carries the number, so a mismatch would silently open the wrong mod.

    Asserted by looking up every number the listing prints and checking it resolves to the mod
    on that row — the failure mode is not a crash, it is the wrong page.
    """
    report = _index_report(_many(actionable=2, up_to_date=3))
    for number, entry in report.indexed_entries():
        assert report.entry_by_handle(str(number)) is entry


def test_a_filtered_listing_keeps_the_numbers_the_full_one_used():
    """Otherwise the same number would mean two different mods in two replies."""
    report = _index_report(_many(actionable=2, up_to_date=3))
    filtered = report.by_status(STATUS_UP_TO_DATE)

    tr = make_translator("zh_cn")
    body = "\n".join(render_index_body(report, tr, entries=filtered))

    for number, entry in report.indexed_entries():
        if entry in filtered:
            assert "[{}] {}".format(str(number).rjust(2), entry.name) in body


def test_an_entry_can_be_looked_up_by_number_or_by_name():
    """A player copying an id out of the listing should not have to translate it to a number."""
    report = _index_report([_entry_with_links("sodium", STATUS_UPDATE_AVAILABLE)])
    entry = report.entries[0]

    for handle in ("1", "sodium", "sodium.jar", "SODIUM"):
        assert report.entry_by_handle(handle) is entry, handle
    assert report.entry_by_handle("999") is None
    assert report.entry_by_handle("") is None
    assert report.entry_by_handle("no such mod") is None


def test_the_detail_view_is_where_the_links_live():
    """The listing cannot afford a url; one mod's detail can afford two."""
    entry = _entry_with_links("sodium", STATUS_UPDATE_AVAILABLE)
    rows = entry_detail_rows(entry, make_translator("zh_cn"))

    urls = [url for _label, _value, url in rows if url]
    assert entry.project_url in urls
    assert entry.download_url in urls
    # And the version change the reader came for, with both versions named.
    body = "\n".join(render_detail(entry, make_translator("zh_cn")))
    assert "1.0.0" in body and "1.1.0" in body


def test_the_detail_view_stays_short():
    """One mod's detail must still fit, notes and all."""
    entry = _entry_with_links("sodium", STATUS_UPDATE_AVAILABLE)
    entry.add_note("note.declared_mc", range=">=26.1 <27")
    entry.add_note("note.bundled_jars", count=16)
    entry.add_note("note.client_only")

    body = render_detail(entry, make_translator("zh_cn"))
    assert len(body) <= 10, body


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
