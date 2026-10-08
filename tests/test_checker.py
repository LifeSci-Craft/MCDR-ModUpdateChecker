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
    ALL_STATUSES,
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
    UPDATE_ENTRY_ORDER,
    Report,
    SummarySection,
    UpdateEntry,
    action_row,
    display_width,
    entry_detail_rows,
    render_entry_lines,
    render_full,
    render_index,
    render_index_row,
    render_pager,
    render_summary,
    summarise,
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


def run_check(upstream, scenario, tmp_path, map_path=None, **overrides):
    options = make_options(upstream, **overrides)
    checker = Checker(options)
    try:
        return checker.run(
            scenario.scan(), SERVER, cache_path=None, map_path=map_path
        )
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
    """An entry carrying both URLs and the digest a download is verified against.

    All three, because that is what a real run produces — ``checker`` fills in the digest it
    read off the platform's version file, and a helper that left it out would make every
    download button in the suite silently disappear, since the button needs something to
    verify the fetched file against before it can honestly offer to fetch it.
    """
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
    entry.download_sha1 = "a" * 40
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


def test_the_link_column_is_measured_in_columns_not_characters():
    """Two names of equal length, one of them in Chinese, and the game draws those twice as
    wide as an ``A``.

    Padding by ``len`` gives both rows the same number of spaces, so the second row's link
    starts two columns further right — and this is not an edge case: every mod name and every
    status note this plugin prints in its shipped language is full-width, so the column came
    out ragged across the whole listing. Asserted on the display width of the text before the
    link, which is the column a reader sees.
    """
    tr = make_translator("zh_cn")
    entries = [
        _entry_with_links("a", STATUS_UPDATE_AVAILABLE, name="AB"),
        _entry_with_links("b", STATUS_UPDATE_AVAILABLE, name="\u6587\u672c"),
    ]
    lines = render_entry_lines(entries, tr, verbose=True)

    # Same character count, different display width — which is what makes this a test of the
    # measurement rather than of the padding arithmetic.
    assert len(entries[0].name) == len(entries[1].name)
    assert display_width(entries[0].name) != display_width(entries[1].name)

    columns = [display_width(line[: line.index("https://")]) for line in lines]
    assert len(set(columns)) == 1, columns


def test_a_row_with_no_number_prints_without_a_handle():
    """``[0]`` looks typeable and resolves to nothing, so it is not printed at all."""
    tr = make_translator("zh_cn")
    entry = _entry_with_links("alpha", STATUS_UPDATE_AVAILABLE)

    numbered = render_index_row(3, entry, tr)
    unnumbered = render_index_row(None, entry, tr)

    assert numbered.startswith("[3] ")
    assert not unnumbered.startswith("[")
    assert numbered.endswith(unnumbered), (numbered, unnumbered)


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


def _listing(report, tr, entries=None, budget=CHAT_PAGE_LINES, page=1):
    """Every line one page of the listing puts on screen, as plain text.

    ``render_index`` returns the rows, the tail and the page figures; the title bar, the server
    context and the pager belong to the screen and are counted here, because they are lines the
    reader scrolls past — a budget measured without them is not the budget the reader gets. The
    pager is built with the real :func:`render_pager`, because a placeholder would let the one
    line that was just added to the budget grow a second line without this noticing.

    The rows carry their number and no indentation any more: the colour says which of them need
    attention, so a leading pair of spaces would only spend width.
    """
    rows, tail, page_info = render_index(report, tr, entries=entries, page=page, budget=budget)
    lines = (
        ["(title bar)", "(server context)"]
        + [text for _number, _entry, text in rows]
        + tail
    )
    if page_info is not None and page_info[1] > 1:
        lines.append(
            render_pager(page_info[0], page_info[1], tr,
                         lambda target: "list {}".format(target))
        )
    return lines


def test_the_listing_fits_a_page_whatever_the_server_holds():
    """The property the whole change exists for.

    A reply that has to be scrolled to find the row you need is the reply that gets closed, so
    the budget holds at every size — including the two extremes where the actionable set alone
    overflows, and where nothing is actionable at all. Pagination is what makes that possible
    without dropping anything: the previous version cut the tail off and reported how many rows
    it had held back, which answered "what is here" but not "how do I see the rest".
    """
    tr = make_translator("zh_cn")
    for actionable, up_to_date in ((3, 4), (40, 160), (0, 200), (40, 0), (0, 0)):
        report = _index_report(_many(actionable, up_to_date))
        lines = _listing(report, tr)
        assert len(lines) <= CHAT_PAGE_LINES, (actionable, up_to_date, len(lines))


def test_every_mod_is_on_exactly_one_page():
    """Nothing is lost between the pages, and nothing is shown twice.

    Read by walking the whole listing page by page and collecting the numbers — the failure
    this catches is an off-by-one in the slice arithmetic, which no single-page assertion can
    see: page two starting one row too early shows a mod twice and would still fit the budget.
    """
    tr = make_translator("zh_cn")
    report = _index_report(_many(actionable=7, up_to_date=20))

    seen = []
    page = 1
    while True:
        rows, _tail, page_info = render_index(report, tr, page=page)
        assert page_info is not None
        page, pages = page_info
        seen.extend(number for number, _entry, _text in rows)
        if page >= pages:
            break
        page += 1

    assert sorted(seen) == [number for number, _entry in report.indexed_entries()]


def test_a_page_past_the_end_lands_on_the_last_one():
    """The buttons are stale the moment the listing shrinks, and refusing is the worse answer.

    A pager line carries a number; mods get installed, the listing gets shorter, and a click on
    "next" can land past the end. Clamping shows the last page — and the pager line says which
    page that is, so the reader is not misled about where they ended up.
    """
    tr = make_translator("zh_cn")
    report = _index_report(_many(up_to_date=25))

    rows, _tail, page_info = render_index(report, tr, page=99)

    assert page_info is not None and page_info[0] == page_info[1]
    assert rows, "clamping must not fall off the end into an empty page"
    # The last page is the tail of the listing, not its head.
    assert "Fresh Mod 000" not in "".join(text for _n, _e, text in rows)


def test_the_pager_line_names_the_commands_a_console_can_type():
    """The console's form of the pager has to be typeable; the buttons are for players only.

    ``[上一页]`` in a terminal is decoration, so the console form spells the commands out.
    Both ends are checked: the first page must not offer a previous page, and a middle page
    must name both neighbours.
    """
    tr = make_translator("zh_cn")

    def command_for(target):
        return "!!muc list {}".format(target)

    first = render_pager(1, 3, tr, command_for)
    assert "第 1/3 页" in first
    assert "上一页" not in first
    assert "下一页：!!muc list 2" in first

    middle = render_pager(2, 3, tr, command_for)
    assert "第 2/3 页" in middle
    assert "上一页：!!muc list" in middle
    assert "下一页：!!muc list 3" in middle


def test_the_first_page_is_the_mods_that_need_attention():
    """Order, not a cap: the mods that need doing are the ones page one shows.

    The listing's own order puts them first, and this is what stops a future reordering — say,
    alphabetical — from quietly pushing the one mod that needs attention onto page fifteen.
    """
    tr = make_translator("zh_cn")
    report = _index_report(_many(actionable=40, up_to_date=160))
    body = "\n".join(_listing(report, tr))

    assert "Action Mod 000" in body
    # 11 rows fit at this budget; every one of them is an actionable mod.
    assert "Action Mod 010" in body
    assert "Action Mod 011" not in body
    assert "Fresh Mod" not in body
    # And the rest is one page away, with the page count on the line.
    assert "第 1/" in body, body


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
    body = "\n".join(_listing(report, tr, entries=filtered))

    for number, entry in report.indexed_entries():
        if entry in filtered:
            assert "[{}] {}".format(number, entry.name) in body


def test_the_listing_never_pads_the_number():
    """``[1]``, not ``[ 1]`` — reported twice, so it is pinned twice.

    Two answers were tried before this one: a constant width of two, which put a visible gap
    after the bracket on a small server, and a width derived from the largest number on the
    page, which still padded every single-digit row on a server with twenty mods. The column is
    simply not aligned now: ``[9]`` and ``[10]`` start a character apart, and that is the trade
    the reader asked for.
    """
    tr = make_translator("zh_cn")

    short = "\n".join(_listing(_index_report(_many(actionable=2, up_to_date=3)), tr))
    assert "[1] " in short
    assert "[ 1]" not in short

    # The case the second attempt got wrong: a listing long enough to reach double digits.
    long_list = "\n".join(_listing(_index_report(_many(up_to_date=12)), tr))
    assert "[1] " in long_list
    assert "[ 1]" not in long_list
    assert "[10] " in long_list


def test_downloading_a_mod_does_not_move_it_in_the_listing():
    """The bug this fix exists for, stated as the sequence that produced it.

    ``!!muc download 1`` followed by ``!!muc install 1`` is the two-step the plugin itself
    prints. Between them the mod becomes ``awaiting_install`` — a different status — and while
    that status had its own rank in the listing order, the whole list renumbered: the ``1`` the
    plugin had just told the admin to type now meant a different mod, and ``install 1``
    answered "that one has not been downloaded".

    The listing's number is a handle, so it has to survive the state change it is used to cause.
    """
    report = _index_report(_many(actionable=3))
    before = {number: entry.file_name for number, entry in report.indexed_entries()}

    # Exactly what ``_reconcile_downloads`` does to the entry once its build is on disk.
    report.entries[0].status = STATUS_AWAITING_INSTALL

    after = {number: entry.file_name for number, entry in report.indexed_entries()}

    assert after == before
    assert report.entry_by_handle("1") is report.entries[0]


def test_the_sort_order_covers_every_status():
    """A status missing from the table sorts last (``.get(status, 99)``) instead of erroring.

    Which means a newly added status would quietly sink below "up to date" — the listing's
    whole purpose is to put what needs doing at the top. Cheaper to assert than to notice.
    """
    assert set(UPDATE_ENTRY_ORDER) == set(ALL_STATUSES)


def test_a_mod_can_be_looked_up_by_the_name_the_listing_shows():
    """The row says ``Lithium``; typing ``Lithium`` has to work.

    It is the spelling in front of the reader, and it was the one form the lookup did not
    accept — only the mod id, the file name and the number were.

    The fixture's mod id and file name are deliberately *unlike* the display name. A mod whose
    id happens to equal its name would resolve through the id and the test would pass without
    the name ever being consulted — which is what an earlier version of this test did.
    """
    report = _index_report(
        [_entry_with_links("lithium_mod", STATUS_UPDATE_AVAILABLE, name="Lithium")]
    )
    entry = report.entries[0]

    for handle in ("Lithium", "lithium", "LITHIUM"):
        assert report.resolve_handle(handle) == (entry, ""), handle
    # The handles that always worked still do.
    for handle in ("lithium_mod", "lithium_mod.jar", "1"):
        assert report.resolve_handle(handle) == (entry, ""), handle


def test_a_handle_forgives_case_spaces_and_punctuation():
    """The same leniency ``check.ignored_mods`` has, because the reason is the same.

    An admin looking at a mod listed as ``Applied Energistics 2`` should not have to reproduce
    the exact spacing, and the id here is ``ae2`` so nothing but the name can answer.
    """
    report = _index_report(
        [_entry_with_links("ae2", STATUS_UPDATE_AVAILABLE, name="Applied Energistics 2")]
    )
    entry = report.entries[0]

    for handle in ("applied energistics 2", "applied-energistics-2", "AppliedEnergistics2"):
        assert report.resolve_handle(handle) == (entry, ""), handle


def test_an_ambiguous_name_is_refused_rather_than_guessed():
    """Two mods that normalise to the same string is not a lookup to guess at.

    Guessing wrong means downloading and installing the wrong jar, so the ambiguity is reported
    and the caller can say so. Note that the *exact* spellings still resolve — the lenient
    comparison only runs when nothing matched literally, so a precise handle never loses.
    """
    report = _index_report(
        [
            _entry_with_links("alpha_one", STATUS_UPDATE_AVAILABLE, name="Alpha Mod"),
            _entry_with_links("alpha_two", STATUS_UPDATE_AVAILABLE, name="Alpha-Mod"),
        ]
    )

    assert report.resolve_handle("Alpha Mod")[0] is report.entries[0]
    assert report.resolve_handle("Alpha-Mod")[0] is report.entries[1]

    entry, reason = report.resolve_handle("alphamod")
    assert entry is None and reason == "ambiguous"


def test_a_unique_prefix_of_a_name_is_enough():
    """打一半就能对上，这是游戏内最接近 Tab 补全的东西。

    ``!!`` commands are chat messages and vanilla completes only its own ``/`` commands, so no
    MCDR plugin can offer real completion in game. Accepting a unique prefix is the practical
    version of it: type the beginning of the name, press enter, done.
    """
    report = _index_report(
        [
            _entry_with_links("sodium", STATUS_UPDATE_AVAILABLE, name="Sodium"),
            _entry_with_links("lithium", STATUS_UPDATE_AVAILABLE, name="Lithium"),
        ]
    )

    entry, reason = report.resolve_handle("lith")

    assert reason == ""
    assert entry is report.entries[1]


def test_a_prefix_that_matches_two_mods_is_refused_and_lists_them():
    """The other half of the same feature: the candidates are what the caller shows.

    A prefix that matches several mods is not narrowed down yet, and guessing would mean
    downloading the wrong jar — so the refusal comes with the list of what it could have been,
    which ``ambiguity_candidates`` gives the caller to turn into clickable completions.
    """
    report = _index_report(
        [
            _entry_with_links("sodium", STATUS_UPDATE_AVAILABLE, name="Sodium"),
            _entry_with_links("sodium_extra", STATUS_UPDATE_AVAILABLE, name="Sodium Extra"),
        ]
    )

    entry, reason = report.resolve_handle("sod")

    assert entry is None and reason == "ambiguous"
    assert [item.name for item in report.ambiguity_candidates("sod")] == [
        "Sodium", "Sodium Extra"
    ]


def test_an_exact_match_is_never_widened_into_a_prefix_search():
    """The two searches are never mixed: a hit found exactly *is* the answer.

    A mod actually named ``Sod`` must win over ``Sodium`` when the reader typed ``sod`` —
    otherwise the prefix search would turn a precise handle into an ambiguous one, and the
    reader could no longer name the shorter mod at all.
    """
    report = _index_report(
        [
            _entry_with_links("sod", STATUS_UPDATE_AVAILABLE, name="Sod"),
            _entry_with_links("sodium", STATUS_UPDATE_AVAILABLE, name="Sodium"),
        ]
    )

    entry, reason = report.resolve_handle("sod")

    assert entry is report.entries[0] and reason == ""


def test_a_prefix_is_matched_through_the_same_normalisation_as_everything_else():
    """Punctuation does not hide a prefix any more than it hides an exact name.

    ``Fabric-API`` is one normalised word, so ``fabric ap`` — with the space exactly where the
    reader's memory puts it — has to reach it, same as the exact-match path already allows.
    """
    report = _index_report(
        [_entry_with_links("fabric_api", STATUS_UPDATE_AVAILABLE, name="Fabric-API")]
    )

    for handle in ("fab", "fabric ap", "fabricap"):
        assert report.resolve_handle(handle) == (report.entries[0], ""), handle


def test_a_handle_is_matched_exactly_before_leniently():
    """``sodium.jar`` must reach the jar, even when a mod is *named* "sodium.jar".

    The loose comparison would happily match either, so the order of the two passes is the
    thing being asserted — not the comparison itself.
    """
    report = _index_report(
        [
            _entry_with_links("first", STATUS_UPDATE_AVAILABLE, name="sodium.jar"),
            _entry_with_links("second", STATUS_UPDATE_AVAILABLE, name="Second"),
        ]
    )
    report.entries[1].file_name = "sodium.jar"

    assert report.resolve_handle("sodium.jar")[0] is report.entries[0]


def test_the_reason_a_handle_failed_is_told_apart():
    """Four failures, four sentences, because they lead to four different next actions."""
    report = _index_report(_many(actionable=2))

    assert report.resolve_handle("") == (None, "empty")
    assert report.resolve_handle("99") == (None, "out-of-range")
    assert report.resolve_handle("no such mod") == (None, "unknown")


def test_an_entry_can_be_looked_up_by_number_or_by_name():
    """A player copying an id out of the listing should not have to translate it to a number."""
    report = _index_report([_entry_with_links("sodium", STATUS_UPDATE_AVAILABLE)])
    entry = report.entries[0]

    for handle in ("1", "sodium", "sodium.jar", "SODIUM"):
        assert report.entry_by_handle(handle) is entry, handle
    assert report.entry_by_handle("999") is None
    assert report.entry_by_handle("") is None
    assert report.entry_by_handle("no such mod") is None


def test_the_detail_view_shows_one_way_to_download():
    """The link and the command both downloaded, and the reader had to choose between them.

    The raw link went: it bypassed the hash check the command performs, it wrapped in the chat
    box, and the button that performs the check is right where the link used to be.
    """
    tr = make_translator("zh_cn")
    entry = _entry_with_links("sodium", STATUS_UPDATE_AVAILABLE)
    rows = entry_detail_rows(entry, tr, action=action_row(entry, 3, "!!muc", tr))

    assert entry.project_url in [row.url for row in rows]
    assert entry.download_url not in [row.url for row in rows], "the raw cdn link is back"
    assert entry.download_url not in [row.value for row in rows]

    # The action sits where that link sat: after the project page, before the notes.
    action_row_index = [index for index, row in enumerate(rows) if row.command]
    project_row_index = [index for index, row in enumerate(rows) if row.url]
    assert len(action_row_index) == 1 and len(project_row_index) == 1
    assert action_row_index[0] == project_row_index[0] + 1
    button = rows[action_row_index[0]]
    assert button.value == tr("command.detail.download")
    assert button.command == "!!muc download 3"

    # And the version change the reader came for, with both versions named.
    body = "\n".join(row.label + row.value for row in rows)
    assert "1.0.0" in body and "1.1.0" in body


def test_the_action_offered_matches_what_the_mod_is_ready_for():
    """Never both, and never a step the mod cannot take.

    A button that can only answer with its own error message is worse than no button, so an
    entry with nothing to offer gets no row at all.
    """
    tr = make_translator("zh_cn")

    fresh = _entry_with_links("alpha", STATUS_UPDATE_AVAILABLE)
    assert action_row(fresh, 1, "!!muc", tr).command == "!!muc download 1"
    # The alias the reader typed is the one the button carries, like every other command it
    # prints — the two roots are interchangeable, but a reply that mixes them reads as a bug.
    assert action_row(fresh, 1, "!!modupdate", tr).command == "!!modupdate download 1"

    fetched = _entry_with_links("beta", STATUS_AWAITING_INSTALL)
    assert action_row(fetched, 2, "!!muc", tr).command == "!!muc install 2"

    current = UpdateEntry(mod_id="c", name="C", file_name="c.jar", status=STATUS_UP_TO_DATE)
    assert action_row(current, 3, "!!muc", tr) is None
    # No number means no command can be spelled, so there is no honest button to offer.
    assert action_row(fresh, None, "!!muc", tr) is None


def test_the_detail_view_does_not_draw_an_arrow_between_equal_versions():
    """``1.0.0 -> 1.0.0`` reads like a change that did not happen.

    Seen by rendering the screen rather than by reading the code: the arrow branch keyed off
    "is there a latest version", which is true for a mod that is already current — and most
    mods on a healthy server are.
    """
    tr = make_translator("zh_cn")

    current = _entry_with_links("alpha", STATUS_UP_TO_DATE, local="1.0.0", latest="1.0.0")
    body = "\n".join(row.label + row.value
                     for row in entry_detail_rows(current, tr))
    assert "1.0.0 -> 1.0.0" not in body, body
    assert "版本: 1.0.0" in body, body

    # And the two ends are still both named when they really are different.
    pending = _entry_with_links("beta", STATUS_UPDATE_AVAILABLE, local="1.0.0", latest="1.1.0")
    body = "\n".join(row.label + row.value for row in entry_detail_rows(pending, tr))
    assert "1.0.0 -> 1.1.0" in body, body


def test_the_detail_view_stays_short():
    """One mod's detail must still fit, notes and all."""
    tr = make_translator("zh_cn")
    entry = _entry_with_links("sodium", STATUS_UPDATE_AVAILABLE)
    entry.add_note("note.declared_mc", range=">=26.1 <27")
    entry.add_note("note.bundled_jars", count=16)
    entry.add_note("note.client_only")

    rows = entry_detail_rows(entry, tr, action=action_row(entry, 1, "!!muc", tr))
    # Plus the title bar the screen draws above the rows.
    assert len(rows) + 1 <= 10, rows


def test_the_summary_and_the_screen_agree_about_the_sections():
    """The log gets flat text and the screen gets buttons; the sections come from one place.

    ``summarise`` is what both walk, so this asserts they really are reading one structure: the
    flat form opens with the same context line, prints the same headings, and every actionable
    mod appears in exactly one section.
    """
    tr = make_translator("zh_cn")
    entry = _entry_with_links("alpha", STATUS_UPDATE_AVAILABLE)
    blocked = _entry_with_links("blocked", STATUS_NO_COMPATIBLE_BUILD)
    report = Report(
        generated_at="2026-01-01T00:00:00+00:00",
        server=SERVER,
        mods_directory="server/mods",
        entries=[entry, blocked],
    )

    context, blocks, closing = summarise(report, tr)
    flat = render_summary(report, tr)
    sections = [block for block in blocks if isinstance(block, SummarySection)]

    assert flat[0] == context
    assert [section.heading for section in sections] == [block.heading for block in sections]
    for section in sections:
        assert section.heading in flat
    assert closing and closing[-1] in flat

    listed = [item for section in sections for item in section.entries]
    assert len(listed) == len({id(item) for item in listed}), "a mod appears in two sections"
    assert {id(item) for item in listed} == {
        id(item) for item in report.entries if item.actionable
    }


def test_a_summary_section_is_ordered_like_the_listing():
    """The sections are cut out of the same set of entries the numbers come from.

    Left in scan order, a section printed ``[2]`` above ``[1]`` — and a reader who noticed had
    no way to tell that from a bug in the numbering itself. This is the assertion that keeps
    the two orders the same, because the numbers are only trustworthy while they are.
    """
    tr = make_translator("zh_cn")
    zulu = _entry_with_links("zulu", STATUS_UPDATE_AVAILABLE, name="Zulu")
    alpha = _entry_with_links("alpha", STATUS_UPDATE_AVAILABLE, name="Alpha")
    report = Report(
        generated_at="2026-01-01T00:00:00+00:00",
        server=SERVER,
        mods_directory="server/mods",
        entries=[zulu, alpha],
    )

    _context, blocks, _closing = summarise(report, tr)
    section = [block for block in blocks if isinstance(block, SummarySection)][0]

    assert section.entries == [alpha, zulu]
    numbers = {id(entry): number for number, entry in report.indexed_entries()}
    assert [numbers[id(item)] for item in section.entries] == [1, 2]


def test_a_section_that_was_truncated_says_so_in_both_forms():
    """The "and N more" line is arithmetic, so it lives in the shared structure, not per
    renderer: one of them would eventually be the one that forgot to count."""
    tr = make_translator("zh_cn")
    entries = [
        _entry_with_links("mod{:02d}".format(index), STATUS_UPDATE_AVAILABLE)
        for index in range(20)
    ]
    report = Report(
        generated_at="2026-01-01T00:00:00+00:00",
        server=SERVER,
        mods_directory="server/mods",
        entries=entries,
    )

    _context, blocks, _closing = summarise(report, tr, max_updates=3)
    section = [block for block in blocks if isinstance(block, SummarySection)][0]

    assert len(section.entries) == 3
    assert any("还有 17 个" in line for line in section.trailing), section.trailing
    assert "还有 17 个" in "\n".join(render_summary(report, tr, max_updates=3))


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


# --------------------------------------------------------------------------------------
# The admin's own map
#
# The documented limitation this exists for: a jar built from source, forked or re-signed has
# bytes Modrinth has never seen and a mod id that may match no slug, so neither automatic
# stage can place it. Everything asserted here is about the *order* — the admin's statement
# has to beat the plugin's guess — and about a bad entry being reported rather than obeyed.
# --------------------------------------------------------------------------------------


def write_map(tmp_path, by_sha1=None, by_mod_id=None):
    path = tmp_path / "project-map.json"
    payload = {
        "version": 1,
        "by_sha1": by_sha1 or {},
        "by_mod_id": by_mod_id or {},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_a_jar_the_admin_mapped_is_resolved_by_the_map(tmp_path, upstream):
    """The unnamed jar is unresolvable by both automatic stages; one line of JSON fixes it."""
    scenario = Scenario(tmp_path, upstream)
    mod = scenario.add_jar("mystery.jar", id="mystery", version="1.0.0", name="Mystery")
    upstream.add_project(
        FakeProject(
            id="proj-custom",
            slug="custom-thing",
            title="Custom Thing",
            versions=[
                _version("proj-custom", "c-100", "1.0.0", "1" * 40),
                _version("proj-custom", "c-200", "2.0.0", "2" * 40,
                         date="2026-02-01T00:00:00Z"),
            ],
        )
    )
    path = write_map(tmp_path, by_sha1={mod.sha1: "custom-thing"})

    report = run_check(upstream, scenario, tmp_path, map_path=path)

    entry = entry_for(report, "mystery")
    assert entry.matched_by == "manual"
    assert entry.status == STATUS_UPDATE_AVAILABLE
    assert (entry.local_version, entry.latest_version) == ("1.0.0", "2.0.0")
    assert entry.project_url.endswith("/custom-thing")
    assert dict(entry.notes)["note.matched_by_manual"]["project"] == "custom-thing"


def test_the_map_beats_the_name_search(tmp_path, upstream):
    """Otherwise an exact-slug coincidence could silently overrule the admin's own answer.

    The jar's mod id *does* match a project slug here, so the name search would happily
    resolve it — to the wrong project. The map has to be consulted first for the admin's
    explicit statement to mean anything.
    """
    scenario = Scenario(tmp_path, upstream)
    scenario.add_jar("fork.jar", id="shared", version="1.0.0", name="Shared")
    upstream.add_project(
        FakeProject(
            id="proj-upstream",
            slug="shared",
            title="Upstream",
            versions=[_version("proj-upstream", "u-1", "9.9.9", "3" * 40)],
        )
    )
    upstream.add_project(
        FakeProject(
            id="proj-fork",
            slug="the-fork",
            title="The Fork",
            versions=[_version("proj-fork", "f-1", "1.5.0", "4" * 40)],
        )
    )
    path = write_map(tmp_path, by_mod_id={"shared": "the-fork"})

    report = run_check(upstream, scenario, tmp_path, map_path=path)

    entry = entry_for(report, "shared")
    assert entry.matched_by == "manual"
    assert entry.latest_version == "1.5.0", "the name search's project was used instead"


def test_the_map_can_be_keyed_by_mod_id_when_the_bytes_change(tmp_path, upstream):
    """Recompiling changes the hash but not the id, which is the case this key exists for."""
    scenario = Scenario(tmp_path, upstream)
    scenario.add_jar("rebuilt.jar", id="rebuilt", version="3.0.0", name="Rebuilt")
    upstream.add_project(
        FakeProject(
            id="proj-rebuilt",
            slug="rebuilt-upstream",
            versions=[_version("proj-rebuilt", "r-1", "3.1.0", "5" * 40)],
        )
    )
    path = write_map(tmp_path, by_mod_id={"rebuilt": "rebuilt-upstream"})

    entry = entry_for(
        run_check(upstream, scenario, tmp_path, map_path=path), "rebuilt"
    )

    assert entry.matched_by == "manual"
    assert entry.status == STATUS_UPDATE_AVAILABLE


def test_a_map_entry_pointing_nowhere_is_reported_rather_than_guessed(tmp_path, upstream):
    """A typo has to surface. Falling through to the name search would hide a broken mapping
    behind a guess that happened to work, and the admin would never learn to fix it."""
    scenario = Scenario(tmp_path, upstream)
    scenario.add_jar("typo.jar", id="typo", version="1.0.0", name="Typo")
    upstream.add_project(
        FakeProject(
            id="proj-typo",
            slug="typo",
            versions=[_version("proj-typo", "t-1", "2.0.0", "6" * 40)],
        )
    )
    path = write_map(tmp_path, by_mod_id={"typo": "wrong-spelling"})

    entry = entry_for(run_check(upstream, scenario, tmp_path, map_path=path), "typo")

    assert entry.status == STATUS_UNRESOLVED
    assert entry.matched_by == ""
    assert dict(entry.notes)["note.manual_map_unknown_project"]["project"] == "wrong-spelling"


def test_a_jar_with_no_metadata_is_not_looked_up_in_the_map(tmp_path, upstream):
    """Nothing to compare a version against, so the entry could only ever be a bare link."""
    scenario = Scenario(tmp_path, upstream)
    mod = scenario.add_plain_jar("library.jar")
    upstream.add_project(
        FakeProject(
            id="proj-lib",
            slug="lib",
            versions=[_version("proj-lib", "l-1", "1.0.0", "7" * 40)],
        )
    )
    path = write_map(tmp_path, by_sha1={mod.sha1: "lib"})

    entry = entry_for_file(
        run_check(upstream, scenario, tmp_path, map_path=path), "library.jar"
    )

    assert entry.status == STATUS_NOT_A_MOD


def test_an_ignored_mod_is_not_rescued_by_the_map(tmp_path, upstream):
    """``ignored_mods`` means "no lookups at all", and a map entry is still a lookup."""
    scenario = Scenario(tmp_path, upstream)
    mod = scenario.add_jar("quiet.jar", id="quiet", version="1.0.0", name="Quiet")
    upstream.add_project(
        FakeProject(
            id="proj-quiet",
            slug="quiet",
            versions=[_version("proj-quiet", "q-1", "2.0.0", "8" * 40)],
        )
    )
    path = write_map(tmp_path, by_sha1={mod.sha1: "quiet"})

    report = run_check(
        upstream, scenario, tmp_path, map_path=path, ignored_mods=["quiet"]
    )

    assert entry_for(report, "quiet").status == STATUS_IGNORED


def test_no_map_file_means_the_old_behaviour(tmp_path, upstream):
    """The feature is opt-in: absent, nothing about the run changes."""
    scenario = Scenario(tmp_path, upstream)
    scenario.add_jar("plain.jar", id="plain", version="1.0.0", name="Plain")

    entry = entry_for(
        run_check(upstream, scenario, tmp_path, map_path=tmp_path / "absent.json"),
        "plain",
    )

    assert entry.status == STATUS_UNRESOLVED
    assert entry.matched_by == ""


# --------------------------------------------------------------------------------------
# Advisories added for breakage that is not an update
# --------------------------------------------------------------------------------------


def project_with_side(project_id, slug, sha1, **flags):
    return FakeProject(
        id=project_id,
        slug=slug,
        versions=[_version(project_id, project_id + "-1", "1.0.0", sha1)],
        **flags,
    )


def test_a_project_modrinth_marks_server_unsupported_is_reported(tmp_path, upstream):
    """Modrinth's flag, not the jar's: this is the half the scanner cannot see.

    A mod whose ``fabric.mod.json`` says ``environment: *`` can still be flagged
    ``server_side: unsupported`` on the platform, and that is the one that will break a server.
    """
    scenario = Scenario(tmp_path, upstream)
    mod = scenario.add_jar("hudonly.jar", id="hudonly", version="1.0.0", name="Hud Only")
    upstream.add_project(
        project_with_side("proj-hudonly", "hudonly", mod.sha1, server_side="unsupported")
    )

    report = run_check(upstream, scenario, tmp_path)

    keys = dict(report.advisories)
    assert "advisory.server_side_unsupported" in keys
    assert keys["advisory.server_side_unsupported"]["files"] == "hudonly.jar"


@pytest.mark.parametrize("side", ["required", "optional", "unknown"])
def test_a_server_compatible_project_is_not_reported(tmp_path, upstream, side):
    """``optional`` means it works on a server. Warning about it would be noise.

    ``unknown`` too: it is what Modrinth reports for a project it has not classified, and
    turning "we do not know" into "this is broken" is the failure this test exists to prevent.
    """
    scenario = Scenario(tmp_path, upstream)
    mod = scenario.add_jar("fine.jar", id="fine", version="1.0.0", name="Fine")
    upstream.add_project(project_with_side("proj-fine", "fine", mod.sha1, server_side=side))

    report = run_check(upstream, scenario, tmp_path)

    assert "advisory.server_side_unsupported" not in dict(report.advisories)


def test_a_missing_dependency_is_reported(tmp_path, upstream):
    """A missing library explains a crash; a stale jar explains almost nothing."""
    scenario = Scenario(tmp_path, upstream)
    scenario.add_jar(
        "needs.jar",
        id="needs",
        version="1.0.0",
        name="Needs",
        depends={"minecraft": ">=26.3", "cloth-config": "*"},
    )

    report = run_check(upstream, scenario, tmp_path)

    keys = dict(report.advisories)
    assert "advisory.missing_dependencies" in keys
    assert keys["advisory.missing_dependencies"]["files"] == "cloth-config"


def test_a_satisfied_dependency_is_not_reported(tmp_path, upstream):
    """The false-positive direction, which is what would make the line ignorable."""
    scenario = Scenario(tmp_path, upstream)
    scenario.add_jar(
        "needs.jar",
        id="needs",
        version="1.0.0",
        name="Needs",
        depends={"minecraft": ">=26.3", "cloth-config": "*"},
    )
    scenario.add_jar("cloth.jar", id="cloth-config", version="1.0.0", name="Cloth Config")

    report = run_check(upstream, scenario, tmp_path)

    assert "advisory.missing_dependencies" not in dict(report.advisories)


def test_a_missing_dependency_that_cannot_run_on_a_server_is_not_reported(tmp_path, upstream):
    """A client-only library is not a missing dependency — it is a dependency of the client.

    A jar that runs on both sides can legitimately require something that only ever exists on
    the client, and listing that as missing sends the reader looking for a file that would do
    nothing if they found it. Modrinth's ``server_side: unsupported`` is exactly the statement
    that the project cannot run on a server, so it is what this is keyed on.
    """
    scenario = Scenario(tmp_path, upstream)
    scenario.add_jar(
        "needs.jar",
        id="needs",
        version="1.0.0",
        name="Needs",
        depends={"minecraft": ">=26.3", "clientlib": "*"},
    )
    upstream.add_project(
        FakeProject(id="proj-clientlib", slug="clientlib", title="Client Lib",
                    server_side="unsupported")
    )

    report = run_check(upstream, scenario, tmp_path)

    advisories = dict(report.advisories)
    assert "advisory.missing_dependencies" not in advisories, advisories
    assert "advisory.missing_dependencies_excluded" not in advisories, advisories
    # And it is still said out loud, on the mod that declared it: an absence that explains
    # itself beats an absence nobody mentions.
    entry = entry_for(report, "needs")
    assert ("note.client_only_dependency", {"dep": "clientlib"}) in entry.notes


def test_only_the_client_only_dependencies_are_taken_out_of_the_list(tmp_path, upstream):
    """The exclusion is per dependency, not per mod.

    One jar declaring two missing dependencies — one client-only, one whose id resolves to
    nothing at all — must come out as "one is still missing, one was excluded". Dropping the
    whole jar's list would hide the real one behind the harmless one.
    """
    scenario = Scenario(tmp_path, upstream)
    scenario.add_jar(
        "needs.jar",
        id="needs",
        version="1.0.0",
        name="Needs",
        depends={"minecraft": ">=26.3", "clientlib": "*", "ghostlib": "*"},
    )
    upstream.add_project(
        FakeProject(id="proj-clientlib", slug="clientlib", title="Client Lib",
                    server_side="unsupported")
    )

    report = run_check(upstream, scenario, tmp_path)

    advisories = dict(report.advisories)
    assert "advisory.missing_dependencies_excluded" in advisories, advisories
    assert advisories["advisory.missing_dependencies_excluded"]["files"] == "ghostlib"
    assert advisories["advisory.missing_dependencies_excluded"]["count"] == 1
    assert advisories["advisory.missing_dependencies_excluded"]["excluded"] == 1


def test_a_dependency_on_a_server_capable_project_is_still_reported(tmp_path, upstream):
    """The false-negative direction: only ``unsupported`` may silence the advisory.

    A project that *can* run on a server — required or optional — is one the loader may need
    here, and treating "we found it but it is optional" as "it is fine to be missing" would
    turn a real finding into silence.
    """
    scenario = Scenario(tmp_path, upstream)
    scenario.add_jar(
        "needs.jar",
        id="needs",
        version="1.0.0",
        name="Needs",
        depends={"minecraft": ">=26.3", "real-lib": "*"},
    )
    upstream.add_project(
        FakeProject(id="proj-real", slug="real-lib", title="Real Lib", server_side="optional")
    )

    report = run_check(upstream, scenario, tmp_path)

    advisories = dict(report.advisories)
    assert "advisory.missing_dependencies" in advisories, advisories
    assert advisories["advisory.missing_dependencies"]["files"] == "real-lib"


# --------------------------------------------------------------------------------------
# Reading a stored report back
#
# ``last_report.json`` is read after a restart so the reuse window survives one, and compared
# against the previous run so "new update" can be told from "still waiting". Both of those
# depend on the file surviving a round trip, so the round trip is asserted rather than assumed.
# --------------------------------------------------------------------------------------


def test_a_report_survives_a_json_round_trip(report):
    restored = Report.from_json(report.to_json())

    assert restored is not None
    assert restored.generated_at == report.generated_at
    assert restored.mods_directory == report.mods_directory
    assert restored.download_folder == report.download_folder
    assert restored.server.describe() == report.server.describe()
    assert restored.server.mc_version_source == report.server.mc_version_source
    assert [entry.file_name for entry in restored.entries] == [
        entry.file_name for entry in report.sorted_entries()
    ]
    assert restored.counts() == report.counts()
    assert restored.actionable_count == report.actionable_count
    assert restored.advisories == report.advisories
    assert restored.upstream_notes == report.upstream_notes
    assert restored.duplicate_ids == report.duplicate_ids
    assert restored.disabled_jars == report.disabled_jars
    assert restored.unidentified == report.unidentified


def test_every_entry_field_survives_including_its_notes(report):
    original = entry_for(report, "alpha")
    restored = [entry for entry in Report.from_json(report.to_json()).entries
                if entry.file_name == original.file_name][0]

    assert restored.status == original.status
    assert restored.matched_by == original.matched_by
    assert restored.download_sha512 == original.download_sha512
    assert restored.download_url == original.download_url
    assert restored.notes == original.notes
    assert restored.release_channel == original.release_channel


@pytest.mark.parametrize(
    "payload",
    [
        "not json at all",
        "[]",
        "{}",
        '{"generated_at": "2026-01-01T00:00:00+00:00"}',
        # A format this version does not know: refuse rather than interpret optimistically.
        '{"format": 99, "generated_at": "2026-01-01T00:00:00+00:00"}',
        # No timestamp, so there is no way to judge the age — which is the only thing a
        # stored report is ever used for.
        '{"format": 1, "generated_at": ""}',
    ],
)
def test_an_unusable_stored_report_is_refused(payload):
    assert Report.from_json(payload) is None


def test_one_unreadable_entry_does_not_lose_the_rest():
    """A whole report is not thrown away over one bad row — the others are still true."""
    stored = Report(generated_at="2026-01-01T00:00:00+00:00", server=SERVER, mods_directory="m")
    good = UpdateEntry(mod_id="a", name="A", file_name="a.jar", status=STATUS_UP_TO_DATE)
    payload = stored.to_dict()
    payload["entries"] = [good.to_dict(), {"no_file_name": True}, "nonsense"]

    restored = Report.from_dict(payload)

    assert [entry.file_name for entry in restored.entries] == ["a.jar"]


def test_an_unknown_status_becomes_unresolved_rather_than_being_printed_raw():
    """A file written by a newer version must not put ``status.something_new`` in the log."""
    payload = {
        "format": 1,
        "generated_at": "2026-01-01T00:00:00+00:00",
        "entries": [
            {"file_name": "a.jar", "mod_id": "a", "name": "A", "status": "quantum_superposition"}
        ],
    }

    restored = Report.from_dict(payload)

    assert restored.entries[0].status == STATUS_UNRESOLVED


def test_the_extra_sections_of_the_payload_are_ignored():
    """``counts`` and ``actionable_count`` are written for scripts and recomputed here."""
    report = Report(
        generated_at="2026-01-01T00:00:00+00:00",
        server=SERVER,
        mods_directory="m",
        entries=[
            UpdateEntry(mod_id="a", name="A", file_name="a.jar", status=STATUS_UPDATE_AVAILABLE)
        ],
    )
    payload = report.to_dict()
    payload["counts"] = {"update_available": 999}
    payload["actionable_count"] = 999

    restored = Report.from_dict(payload)

    assert restored.actionable_count == 1


def test_new_since_last_names_only_the_updates_that_appeared():
    """The whole point: a mod that has needed updating for a week is not news.

    Without this, every server start re-announces the same list identically, and the admin has
    no way to tell "still waiting" from "just published".
    """
    previous = Report(
        generated_at="2026-01-01T00:00:00+00:00",
        server=SERVER,
        mods_directory="m",
        entries=[
            UpdateEntry(mod_id="old", name="Old", file_name="old.jar",
                        status=STATUS_UPDATE_AVAILABLE),
            UpdateEntry(mod_id="settled", name="Settled", file_name="settled.jar",
                        status=STATUS_UP_TO_DATE),
        ],
    )
    current = Report(
        generated_at="2026-01-02T00:00:00+00:00",
        server=SERVER,
        mods_directory="m",
        entries=[
            UpdateEntry(mod_id="old", name="Old", file_name="old.jar",
                        status=STATUS_UPDATE_AVAILABLE),
            UpdateEntry(mod_id="settled", name="Settled", file_name="settled.jar",
                        status=STATUS_UPDATE_AVAILABLE),
            UpdateEntry(mod_id="brand", name="Brand", file_name="brand.jar",
                        status=STATUS_UPDATE_AVAILABLE),
        ],
    )

    current.record_new_since(previous)

    assert current.new_since_last == ["brand.jar", "settled.jar"]


def test_nothing_is_called_new_without_a_previous_report():
    """Empty is the only honest answer on a first run, and the renderer says nothing then."""
    report = Report(
        generated_at="2026-01-01T00:00:00+00:00",
        server=SERVER,
        mods_directory="m",
        entries=[
            UpdateEntry(mod_id="a", name="A", file_name="a.jar", status=STATUS_UPDATE_AVAILABLE)
        ],
    )

    report.record_new_since(None)

    assert report.new_since_last == []


def test_a_build_that_has_since_been_downloaded_is_not_new():
    """Moving to "waiting to be installed" is the admin's own doing, not news."""
    previous = Report(
        generated_at="2026-01-01T00:00:00+00:00",
        server=SERVER,
        mods_directory="m",
        entries=[
            UpdateEntry(mod_id="a", name="A", file_name="a.jar",
                        status=STATUS_UPDATE_AVAILABLE)
        ],
    )
    current = Report(
        generated_at="2026-01-02T00:00:00+00:00",
        server=SERVER,
        mods_directory="m",
        entries=[
            UpdateEntry(mod_id="a", name="A", file_name="a.jar",
                        status=STATUS_AWAITING_INSTALL)
        ],
    )

    current.record_new_since(previous)

    assert current.new_since_last == []


def test_the_new_since_line_is_rendered_and_absent_when_empty():
    """Rendered inside the update section, so "there are five" is read before "two are new"."""
    report = Report(
        generated_at="2026-01-01T00:00:00+00:00",
        server=SERVER,
        mods_directory="m",
        entries=[
            UpdateEntry(mod_id="a", name="A", file_name="a.jar", local_version="1.0",
                        latest_version="2.0", status=STATUS_UPDATE_AVAILABLE)
        ],
    )
    tr = make_translator("zh_cn")

    assert "新出现" not in "\n".join(render_summary(report, tr))

    report.new_since_last = ["a.jar"]
    rendered = "\n".join(render_summary(report, tr))

    assert "新出现" in rendered and "a.jar" in rendered
    assert rendered.index("存在更新") < rendered.index("新出现")


def test_a_stored_report_keeps_the_download_folder_so_the_hint_still_works():
    """The JSON used to omit it, and the notification then named a folder it could not say."""
    report = Report(
        generated_at="2026-01-01T00:00:00+00:00",
        server=SERVER,
        mods_directory="m",
        download_folder="/plugins/mod_update_checker/downloads",
    )

    assert Report.from_json(report.to_json()).download_folder == report.download_folder


def test_the_two_batched_lookups_share_one_round_trip(tmp_path, upstream, monkeypatch):
    """1b (newest build per hash) and 1c (project titles) need only the identities from 1a.

    Neither needs the other, so asking them one after the other is two round trips where one
    will do. On a server whose whole folder resolves by hash — the normal case — those two are
    the last two requests of the check, so this is the difference between a check that ends in
    one round trip and one that ends in two.

    Watched by counting simultaneous calls rather than by timing the run: that is the property
    actually wanted, and it does not depend on how loaded the machine is.
    """
    from mod_update_checker.modrinth import ModrinthClient

    scenario = Scenario(tmp_path, upstream)
    mod = scenario.add_jar("alpha.jar", id="alpha", version="1.0.0", name="Alpha")
    upstream.add_project(
        FakeProject(
            id="proj-alpha",
            slug="alpha",
            title="Alpha Mod",
            versions=[_version("proj-alpha", "a-100", "1.0.0", mod.sha1)],
        )
    )

    state = {"in_flight": 0, "peak": 0}
    lock = threading.Lock()

    def watching(attribute):
        real = getattr(ModrinthClient, attribute)

        def wrapper(self, *args, **kwargs):
            with lock:
                state["in_flight"] += 1
                state["peak"] = max(state["peak"], state["in_flight"])
            try:
                # Stands in for the round trip. Without it both calls would be over before the
                # other started and there would be nothing to observe, whatever the threading did.
                time.sleep(0.05)
                return real(self, *args, **kwargs)
            finally:
                with lock:
                    state["in_flight"] -= 1

        return wrapper

    monkeypatch.setattr(ModrinthClient, "latest_from_hashes", watching("latest_from_hashes"))
    monkeypatch.setattr(ModrinthClient, "projects", watching("projects"))

    report = run_check(upstream, scenario, tmp_path)

    assert state["peak"] > 1, "the two batched lookups were made one after the other"
    # And the verdicts are unaffected by the overlap.
    assert entry_for(report, "alpha").status == STATUS_UP_TO_DATE


def test_overlapping_the_batched_lookups_still_reports_both_kinds_of_failure(
    tmp_path, upstream, monkeypatch
):
    """A failure in 1b means "we could not judge this mod", and one in 1c is only a missing title.

    They are collected from the workers as values rather than appended to the report from
    inside them, so this asserts the two are still told apart after the restructure: 1b failing
    must not lose the mod, and 1c failing must not turn a verdict into an error.
    """
    from mod_update_checker.modrinth import ModrinthClient
    from mod_update_checker.upstream import UpstreamError

    scenario = Scenario(tmp_path, upstream)
    mod = scenario.add_jar("alpha.jar", id="alpha", version="1.0.0", name="Alpha")
    upstream.add_project(
        FakeProject(
            id="proj-alpha",
            slug="alpha",
            title="Alpha Mod",
            versions=[
                _version("proj-alpha", "a-100", "1.0.0", mod.sha1),
                _version("proj-alpha", "a-110", "1.1.0", "9" * 40,
                         date="2026-02-01T00:00:00Z"),
            ],
        )
    )

    def refuse_titles(self, *_args, **_kwargs):
        raise UpstreamError("titles unavailable")

    monkeypatch.setattr(ModrinthClient, "projects", refuse_titles)

    report = run_check(upstream, scenario, tmp_path)

    entry = entry_for(report, "alpha")
    assert entry.status == STATUS_UPDATE_AVAILABLE, "a missing title must not change the verdict"
    assert "note.modrinth_unavailable" in dict(report.upstream_notes)
