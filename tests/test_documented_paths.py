"""The documents cite files; those files have to exist.

``README-dev.md`` is written as an evidence table: every performance and width number points at
the script that produced it. That is only worth anything if the pointer resolves. It did not
once — seven ``bench/*.py`` paths were cited for months while ``bench/`` lived outside the
repository, so nobody else could rerun a single one of those measurements, and the existing
documentation guards did not notice because they only check that a Markdown *link* lands on a
heading, never that a backticked *path* exists.

Two rules, both cheap and both about the same failure mode:

* every backticked repo-relative path in the documents resolves;
* every script in ``bench/`` is named somewhere, so adding one cannot quietly leave the
  documents describing an older set of tools.
"""

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parent.parent

#: Documents whose backticked paths are claims about this repository.
DOCUMENTS = ("README.md", "README-dev.md", "CHANGELOG.md", "tests/README.md", "bench/README.md")

#: Top-level directories a documented path may start with. A bare file name (``checker.py``)
#: or a runtime file name (``last_report.json``) is not a path claim and is not policed.
TOP_LEVEL = {"bench", "tests", "tools", "mod_update_checker", "docs", ".github"}

_BACKTICK = re.compile(r"`([^`\n]+)`")
_LINE_SUFFIX = re.compile(r":\d+(?:-\d+)?$")


def documented_paths(text):
    """Yield the backticked spans that are repo-relative paths."""
    for match in _BACKTICK.finditer(text):
        span = match.group(1).strip()
        if not span or any(character.isspace() for character in span):
            continue
        candidate = _LINE_SUFFIX.sub("", span).strip(".,;:")
        if candidate.startswith(("http", "!!", "-", "*", "$", "/")):
            continue
        # A glob is a pattern, not a claim about one file: ``bench/*.py`` says "the scripts in
        # that folder", and the test below already requires each of them to be named.
        if "*" in candidate or "?" in candidate:
            continue
        if "/" not in candidate or candidate.split("/")[0] not in TOP_LEVEL:
            continue
        yield span, candidate


def test_every_documented_repo_path_exists():
    missing = []
    checked = 0
    for name in DOCUMENTS:
        path = REPO / name
        assert path.is_file(), "{} is missing".format(name)
        for shown, candidate in documented_paths(path.read_text(encoding="utf-8")):
            checked += 1
            if not (REPO / candidate).exists():
                missing.append("{}: {} -> {}".format(name, shown, candidate))
    assert checked, "no documented paths were found, so this test proves nothing"
    assert missing == [], "documented paths that do not exist:\n  " + "\n  ".join(missing)


def test_every_bench_script_is_named_somewhere():
    """A tool nobody documents is a tool nobody runs, and these are one-off measurements."""
    documented = ""
    for name in ("README-dev.md", "bench/README.md"):
        path = REPO / name
        if path.is_file():
            documented += path.read_text(encoding="utf-8")
    undocumented = [
        path.name
        for path in sorted((REPO / "bench").glob("*.py"))
        if path.name not in documented
    ]
    assert undocumented == [], "bench scripts nobody mentions: {}".format(undocumented)
