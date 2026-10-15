#!/usr/bin/env python3
"""Every mutation of a guard must be caught by at least one test.

A passing suite proves nothing on its own: a test that exercises a function without depending on
its result keeps passing when the result is wrong. This script introduces one deliberate defect at
a time into a **temporary copy** of the repository, runs the tests that are supposed to notice,
and fails if any of them does not. It never writes to the working tree, so an interrupted run
cannot leave the repository in an injected state — the accident ``tests/test_docs.py`` still guards
against, and the reason its docstring used to point at two helper scripts that were never in the
repository.

Usage::

    python tools/mutation_check.py

It refuses to run with e2e skipped, and the honest reason is narrower than the old text claimed.
That text said "at least one mutation is only caught by the end-to-end suite"; measured, **every
one of the twelve selectors is satisfied by a unit test**, and none of them references
``tests/test_e2e.py``. So the guard is not protecting a particular mutation. It is protecting the
*baseline*: the run reports one summary over the suite, and a suite that quietly lost its
end-to-end half would make that summary a weaker claim than it reads as. For a tool whose whole
argument is "this suite has teeth", that is worth refusing over — but it is a statement about the
baseline, not about any single mutation, and writing it as the latter is what made it unverifiable.

A mutation that changes nothing is reported as a **stale anchor** rather than a pass: the code
moved and the mutation has to be rewritten, which is a different problem from a test without teeth.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

SRC = "mod_update_checker/__init__.py"
CHECKER = "mod_update_checker/checker.py"
CLEANUP = "mod_update_checker/cleanup.py"
DOWNLOADS = "mod_update_checker/downloads.py"
INSTALLER = "mod_update_checker/installer.py"
JSONFILE = "mod_update_checker/jsonfile.py"
PROJECTMAP = "mod_update_checker/projectmap.py"
SCANNER = "mod_update_checker/scanner.py"
SERVERINFO = "mod_update_checker/serverinfo.py"
UPSTREAM = "mod_update_checker/upstream.py"
VERSIONING = "mod_update_checker/versioning.py"

#: The union of the per-mutation selectors below: the baseline has to be green over everything
#: any mutation will run, or a red baseline would be misread as a caught mutation.
BASELINE_FILES = [
    "tests/test_versioning.py",
    "tests/test_cleanup.py",
    "tests/test_jsonfile.py",
    "tests/test_downloads.py",
    "tests/test_scanner.py",
    "tests/test_projectmap.py",
    "tests/test_serverinfo.py",
    "tests/test_checker.py",
    "tests/test_clients.py",
    "tests/test_mcdr_entry.py",
]


def replace_once(text, anchor, replacement):
    """Replace ``anchor`` exactly once.

    Returning the input unchanged is the signal for a stale anchor, so an anchor that appears
    twice is deliberately not touched either: replacing both would test something else entirely.
    """
    if text.count(anchor) != 1:
        return text
    return text.replace(anchor, replacement)


def mutations():
    """(name, file, mutate, selector). ``mutate`` returns the source, or it unchanged."""
    table = []

    def add(name, path, anchor, replacement, selector):
        def mutate(source, anchor=anchor, replacement=replacement):
            return replace_once(source, anchor, replacement)

        table.append((name, path, mutate, selector))

    add("version comparison always answers 'older'",
        VERSIONING,
        "def compare(left: str, right: str) -> int:",
        "def compare(left: str, right: str) -> int:\n    return -1  # mutation",
        ["tests/test_versioning.py"])

    add("any file name counts as one of our backups",
        CLEANUP,
        '    return bool(_JAR_BACKUP.fullmatch(str(name or "")))',
        "    return True  # mutation",
        ["tests/test_cleanup.py"])

    add("JSON writes to one target are no longer serialised",
        JSONFILE,
        "    payload_text = json_text(payload, sort_keys)\n    with _write_lock_for(target):",
        "    payload_text = json_text(payload, sort_keys)\n"
        "    if True:  # mutation: no lock, so two renames race for one destination",
        ["tests/test_jsonfile.py"])

    add("the ledger writes only what one instance remembers",
        DOWNLOADS,
        "            merged = self._read_records()",
        "            merged = dict(self._records)  # mutation: skip the merge",
        ["tests/test_downloads.py", "-k", "concurrent_instances"])

    add("unsafe download names are accepted",
        DOWNLOADS,
        '    text = str(name or "").strip()\n    if not text:\n        return False, "empty"',
        '    return True, ""  # mutation\n    text = str(name or "").strip()\n'
        '    if not text:\n        return False, "empty"',
        ["tests/test_downloads.py"])

    add("'provides' no longer satisfies a requirement",
        SCANNER,
        "        for provided in mod.metadata.provides:",
        "        for provided in ():  # mutation",
        ["tests/test_scanner.py"])

    add("an unreadable project map escapes instead of being reported",
        PROJECTMAP,
        "        except (OSError, ValueError) as error:",
        "        except OSError as error:  # mutation: UnicodeDecodeError now escapes",
        ["tests/test_projectmap.py"])

    add("the MC-version vote counts upper bounds again",
        SERVERINFO,
        "def _lower_bounds(expression: str) -> List[str]:",
        "def _lower_bounds(expression: str) -> List[str]:\n    return _versions_of(expression)  # mutation",
        ["tests/test_serverinfo.py"])

    add("the backup keeps its old timestamp",
        INSTALLER,
        "        os.utime(str(backup), None)",
        "        pass  # mutation: no stamp",
        ["tests/test_mcdr_entry.py", "-k", "stamps_the_backup"])

    add("a batch install authorises only the first build",
        SRC,
        "    for entry in entries:\n        (approved if ledger.approve(entry_key(entry)) else missing).append(entry)",
        "    for entry in entries[:1]:  # mutation\n"
        "        (approved if ledger.approve(entry_key(entry)) else missing).append(entry)",
        ["tests/test_mcdr_entry.py", "-k", "install_all_authorises_every_downloaded_build"])

    add("release channels are no longer filtered",
        CHECKER,
        "def _in_channels(version: ModrinthVersion, channels: Sequence[str]) -> bool:",
        "def _in_channels(version: ModrinthVersion, channels: Sequence[str]) -> bool:\n    return True  # mutation",
        ["tests/test_checker.py"])

    add("downloads bypass the rate limiter",
        UPSTREAM,
        "            self.rate_limiter.acquire()\n        try:",
        "            pass  # mutation: no rate limiting\n        try:",
        ["tests/test_clients.py"])

    return table


MUTATIONS = mutations()


def run_pytest(workdir, args):
    """Run pytest in the copy. ``PYTHONDONTWRITEBYTECODE`` keeps a stale ``.pyc`` from winning."""
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("MCDR_SKIP_E2E", None)
    # The copy holds the sources; the interpreter's copy of MCDR and pytest stays in the real
    # checkout, so the package cannot be imported from the wrong side by accident.
    env["PYTHONPATH"] = os.pathsep.join(
        [str(workdir), str(REPO / ".testlibs"), str(REPO / "tests")]
    )
    proc = subprocess.run(
        [sys.executable, "-m", "pytest"] + args
        + ["-q", "--no-header", "-p", "no:cacheprovider", "-o", "addopts="],
        cwd=str(workdir), env=env, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=900,
    )
    lines = [line.strip() for line in (proc.stdout + proc.stderr).splitlines() if line.strip()]
    summary = next(
        (line for line in reversed(lines)
         if "passed" in line or "failed" in line or "error" in line),
        "(no summary)",
    )
    return proc.returncode, summary


def main() -> int:
    if os.environ.get("MCDR_SKIP_E2E"):
        print("MCDR_SKIP_E2E is set: the end-to-end suite would be skipped, so the baseline")
        print("summary below would cover less than the suite this repository claims to run.")
        print("Unset it and run again.")
        return 2
    if not (REPO / ".testlibs").is_dir():
        print("run the tests/README.md setup first (.testlibs is missing)")
        return 1

    workdir = Path(tempfile.mkdtemp(prefix="muc_mutation_"))
    repo = workdir / "repo"
    shutil.copytree(
        REPO, repo,
        ignore=shutil.ignore_patterns(".git", ".testlibs", ".pytest_cache", ".ruff_cache", "__pycache__"),
    )
    baselines = {
        relative: (repo / relative).read_text(encoding="utf-8")
        for _, relative, _, _ in MUTATIONS
    }

    code, summary = run_pytest(repo, BASELINE_FILES)
    print("baseline (unmutated): exit={} {}".format(code, summary))
    if code != 0:
        print("baseline is not green — fix the tests first")
        return 1
    print()

    caught, survived, broken = 0, [], []
    for name, relative, mutate, selector in MUTATIONS:
        baseline = baselines[relative]
        mutated = mutate(baseline)
        if mutated == baseline:
            broken.append(name)
            print("  !!       {:<54} anchor not found (did the code move?)".format(name))
            continue
        try:
            compile(mutated, relative, "exec")
        except SyntaxError as error:
            broken.append(name)
            print("  !!       {:<54} mutated code does not compile: line {}".format(
                name, error.lineno))
            continue
        (repo / relative).write_text(mutated, encoding="utf-8")
        try:
            code, summary = run_pytest(repo, selector)
        finally:
            (repo / relative).write_text(baseline, encoding="utf-8")

        failed = " failed" in summary
        errored = " error" in summary and not failed
        if code == 1 and failed:
            caught += 1
            print("  caught   {:<54} {}".format(name, summary))
        elif code in (4, 5) or errored:
            broken.append(name)
            print("  ERROR    {:<54} exit={} {} — the suite broke, this proves nothing".format(
                name, code, summary))
        else:
            survived.append(name)
            print("  SURVIVED {:<54} exit={} {}".format(name, code, summary))

    print()
    print("caught {}/{}".format(caught, len(MUTATIONS)))
    for label, items in (("survived — the corresponding tests are decorative", survived),
                         ("could not be applied cleanly", broken)):
        if items:
            print("{} mutation(s) {}:".format(len(items), label))
            for name in items:
                print("   - {}".format(name))
    if survived or broken:
        print("the temporary copy is kept for inspection: {}".format(workdir))
        return 1
    shutil.rmtree(workdir, ignore_errors=True)
    print("every mutation was caught — the tests have teeth")
    return 0


if __name__ == "__main__":
    sys.exit(main())
