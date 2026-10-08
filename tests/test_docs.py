"""The prose against the code: option names, counts, and which document says what.

Two documents, two readers. ``README.md`` is for whoever installs the plugin: what it does, how
to run it, every option, and how to read a verdict. ``README-dev.md`` is for whoever changes it:
how a verdict is reached, why the version comparison is the way it is, how each claim was
verified, and how to build and release. The split has a rule worth keeping — a user should be
able to read the whole of README.md without meeting an implementation detail, and a developer
should not have to reconstruct the design from the source.

Documentation also rots in a specific way. When the config was grouped into sections, every
option gained a prefix — and the prose kept naming them the old way, in the exact places a
reader goes to *check* the name (``check.ignored_mods`` was still written ``ignored_mods`` in
the troubleshooting section). Nothing failed, because prose has no imports to break.

Same story with the numbers: ``32 项检查`` and ``456 项`` stayed in the README long after both
had moved. A count in prose is a claim, so the ones that can be derived from code are derived
here rather than remembered.

What this file deliberately does *not* do is spell-check the prose. It checks the things that
have a machine-readable source of truth: option paths, counts, links between the two documents,
and the audience split itself.
"""

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PACKAGE = REPO / "mod_update_checker"

#: The user-facing document, written for whoever installs this.
USER_DOC = "README.md"

#: The developer-facing one. Both are scanned for the invariants below; the checks that are
#: about *what a user is told* name ``USER_DOC`` explicitly.
DEVELOPER_DOC = "README-dev.md"

DOCS = (USER_DOC, DEVELOPER_DOC, "CHANGELOG.md", "tests/README.md")

SECTIONS = ("server", "check", "report", "sources", "download", "network")

#: A backticked token: ``like.this``. Only simple identifiers, so a path (``lang/zh_cn.json``)
#: or a file with an extension (``report.py``) is never mistaken for an option.
_BACKTICKED = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*)`")

#: Extensions that turn a dotted token into a file name rather than an option path.
_FILE_SUFFIXES = (".py", ".json", ".md", ".jar", ".txt", ".toml", ".yml", ".yaml", ".log")

#: A block that is *about* wording this project has removed is allowed to quote it. A changelog
#: entry that says "the README used to claim X" has to write X down, and refusing to let it
#: would mean the record of the fix could not be kept.
#:
#: Blank-line separated, not line-by-line: the justification and the quotation it covers are
#: usually on different lines of the same paragraph, and a per-line rule would either miss the
#: quotation or force the marker onto every line.
_SUPERSEDED_CONTEXT = ("旧版", "旧名字", "旧名", "不成立", "过时", "曾经")


def _doc(name: str) -> str:
    return (REPO / name).read_text(encoding="utf-8")


def _asserting_blocks(name: str):
    """``(block, line number)`` for paragraphs that are not explaining a removed claim."""
    text = _doc(name)
    block: list = []
    start = 1
    for number, line in enumerate(text.splitlines() + [""], start=1):
        if line.strip():
            block.append(line)
            continue
        body = "\n".join(block)
        if body and not any(marker in body for marker in _SUPERSEDED_CONTEXT):
            yield body, start
        block = []
        start = number + 1


def _config_paths() -> set:
    from support import option_paths

    import mod_update_checker as plugin

    return option_paths(plugin.Config)


def _legacy_names() -> set:
    import mod_update_checker as plugin

    return set(plugin._LEGACY_FLAT_OPTIONS)


@pytest.mark.parametrize("name", DOCS)
def test_no_doc_names_a_flat_option_that_the_config_no_longer_has(name):
    """``ignored_mods`` and friends only belong where the rename itself is being explained.

    The failure this prevents is not cosmetic: a reader who copies the name out of the
    troubleshooting section gets a key MCDR silently ignores, and their setting reverts without
    a word — the plugin even warns about exactly this, in the config file, to nobody's benefit
    because the docs had told them the old name.
    """
    legacy = _legacy_names()
    offenders = []
    for block, number in _asserting_blocks(name):
        for line in block.splitlines():
            for token in _BACKTICKED.findall(line):
                if token in legacy:
                    offenders.append((token, "line {}".format(number)))
    assert offenders == [], "{} names removed options:\n{}".format(
        name, "\n".join("  {} — {}".format(*item) for item in offenders)
    )


@pytest.mark.parametrize("name", DOCS)
def test_every_option_path_in_the_docs_exists(name):
    """The other direction: a dotted path written in the prose must be a real option.

    Only tokens whose first segment is one of the config's sections are considered, so
    ``fabric.mod.json`` and ``latest.log`` are not swept in.
    """
    known = _config_paths()
    offenders = []
    for block, number in _asserting_blocks(name):
        for line in block.splitlines():
            for token in _BACKTICKED.findall(line):
                if token.endswith(_FILE_SUFFIXES) or "." not in token:
                    continue
                if token.split(".")[0] not in SECTIONS or token in known:
                    continue
                offenders.append((token, "line {}".format(number)))
    assert offenders == [], "{} names options that do not exist:\n{}".format(
        name, "\n".join("  {} — {}".format(*item) for item in offenders)
    )


def test_the_documented_check_count_is_the_real_one():
    """The developer README quotes how many checks the matrix compares across MCDR versions.

    ``required_keys`` is where that number lives, and it changes whenever a check is added — so
    the sentence is derived from it rather than maintained beside it. (It sat at "32" for two
    releases after the download-mode split made it 36.)
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "mcdr_matrix", REPO / "tools" / "mcdr_matrix.py"
    )
    assert spec is not None and spec.loader is not None
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)

    download_checks = len(tool.required_keys(False))
    assert download_checks > 0
    assert "{} 项检查逐项一致".format(download_checks) in _doc(DEVELOPER_DOC), (
        "{} quotes a different check count than required_keys() reports ({})".format(
            DEVELOPER_DOC, download_checks
        )
    )


def test_the_docs_do_not_promise_a_read_only_plugin():
    """``mods/`` is writable now, and three separate places used to say it never would be.

    ``install_on_stop`` and ``!!muc install`` both replace jars in ``mods/`` — carefully, and
    only after the server has stopped, but the prose said "从不动 mods/" and "绝不会装进 mods/"
    flatly. A claim like that is worse than no claim: an admin who reads it will not go looking
    for the switch that does exactly what they wanted, and one who finds it anyway will not
    trust the rest of the page.

    The words are asserted to be absent rather than reworded here, because there is no phrasing
    of "it never writes mods/" that is true. A paragraph that is *explaining* the removal may
    still quote the old wording — see ``_SUPERSEDED_CONTEXT`` — but a feature section asserting
    it may not.
    """
    forbidden = ("从不动 `mods/`", "绝不会装进 `mods/`", "它从不改动 `mods/`", "只下载，不安装")
    for name in DOCS:
        for block, number in _asserting_blocks(name):
            for phrase in forbidden:
                assert phrase not in block, "{} (line {}) still claims {!r}".format(
                    name, number, phrase
                )


def test_the_readme_lists_every_command_the_tree_registers():
    """A command nobody documents is a command nobody uses.

    Read off the tree, so adding one without adding it to the README table fails here. The
    help page has the same invariant from the other side (``test_mcdr_entry``), and the two
    together mean a new subcommand has to be discoverable in both places.
    """
    import mod_update_checker as plugin

    registered = set()
    for child in plugin._command_tree("!!muc").get_children():
        registered.update(child.literals)

    readme = _doc(USER_DOC)
    missing = sorted(
        name for name in registered
        if "!!modupdate {}".format(name) not in readme
    )
    assert missing == [], "not documented in {}: {}".format(USER_DOC, missing)


def test_the_user_readme_points_at_the_developer_readme():
    """A document nobody can find is a document nobody reads.

    The split only works if each side names the other: someone who installs the plugin and then
    wonders how a verdict was reached has to be able to get from one to the other without
    guessing a file name. Asserted on the link *target*, not on the label — whether the label
    is code-formatted is not the point.
    """
    assert "]({})".format(DEVELOPER_DOC) in _doc(USER_DOC), (
        "{} never links to {}".format(USER_DOC, DEVELOPER_DOC)
    )
    assert "]({})".format(USER_DOC) in _doc(DEVELOPER_DOC), (
        "{} never links back to {}".format(DEVELOPER_DOC, USER_DOC)
    )


def test_the_user_readme_carries_no_implementation_detail():
    """The audience split, asserted on its most mechanical symptom.

    A user reads ``README.md`` to install the plugin and act on a report. Sections about how
    the comparison algorithm works, how each claim was verified, and how to build the artifact
    belong in the other document — they are the reason the other document exists, and having
    them in both is how the two drift apart again. Their headings are listed here so that
    pasting one back fails.
    """
    forbidden_headings = (
        "它是怎么判断的",
        "版本比对的取舍",
        "这些结论是怎么核验的",
        "开发",
        "发布一个新版本",
    )
    headings = [
        line.lstrip("#").strip()
        for line in _doc(USER_DOC).splitlines()
        if line.startswith("#")
    ]
    intruders = [h for h in headings if any(f in h for f in forbidden_headings)]
    assert intruders == [], "{} has developer sections: {}".format(USER_DOC, intruders)

    developer = _doc(DEVELOPER_DOC)
    for heading in forbidden_headings:
        assert heading in developer, "{} lost the {} section".format(DEVELOPER_DOC, heading)


_REPO_URL = re.compile(r"https://github\.com/([^/\"')\s]+)/([^/\"')\s#]+)")


def test_every_repository_url_names_the_same_repository():
    """The metadata, the user agent and the changelog must agree on one owner/repo.

    Four places name this project's repository, and one of them is what a user clicks when
    something goes wrong. They had drifted apart from reality in a way nothing could catch:
    all four said ``Pau1am/MCDR-ModUpdateChecker`` while the repository actually lives at
    ``LifeSci-Craft/MCDR-ModUpdateChecker``, so the homepage MCDR showed, the "issues" link
    and the identifier sent to Modrinth's API were **all 404**.

    That the owner is the right one is a fact only the live remote knows, so it cannot be
    asserted here; what is asserted is that these four cannot disagree, which is the part that
    rots silently. Changing the owner is then a single edit plus a grep.
    """
    metadata = json.loads((REPO / "mcdreforged.plugin.json").read_text(encoding="utf-8"))
    sources = {
        "plugin.json homepage": metadata["links"]["homepage"],
        "plugin.json source": metadata["links"]["source"],
        "plugin.json issues": metadata["links"]["issues"],
        "checker.py USER_AGENT": (
            PACKAGE / "checker.py"
        ).read_text(encoding="utf-8").split("USER_AGENT = ", 1)[1].split("\n", 1)[0],
        "CHANGELOG.md releases link": next(
            line for line in _doc("CHANGELOG.md").splitlines() if "/releases" in line
        ),
    }
    found = {}
    for label, text in sources.items():
        match = _REPO_URL.search(text)
        assert match is not None, "{} names no github repository: {}".format(label, text)
        found[label] = (match.group(1), match.group(2))

    assert len(set(found.values())) == 1, "the repository urls disagree:\n{}".format(
        "\n".join("  {}: {}/{}".format(label, *value) for label, value in sorted(found.items()))
    )

    # And the author is a person, not a repository: the metadata keeps ``Pau1am`` even though
    # the repo lives under the org.
    authors = {entry["name"] for entry in metadata["authors"]}
    assert authors, "no author is declared"


# --------------------------------------------------------------------------------------
# The version, which is written down in three places by hand
# --------------------------------------------------------------------------------------
#
# `mcdreforged.plugin.json` is what MCDR reads, `CHANGELOG.md`'s top section is what the release
# notes are built from, and the sample status screen in the user README shows a title bar with
# the version in it. All three are edited during a release, none of them imports the others, and
# getting one wrong produces documentation that describes a plugin nobody can install.

#: ``===============  Mod Update Checker v1.5.0  ===============``
_TITLE_BAR_VERSION = re.compile(r"Mod Update Checker v([0-9][\w.\-]*)")

#: The whole line, both sides of ``=`` included — so the sample can be compared to the real one.
_TITLE_BAR_LINE = re.compile(r"^=+  Mod Update Checker v\S+  =+$", re.MULTILINE)


def _shipped_version() -> str:
    return json.loads((REPO / "mcdreforged.plugin.json").read_text(encoding="utf-8"))["version"]


def test_the_readme_sample_shows_the_shipped_version():
    """The sample is a screenshot in text form; a wrong number there is a broken screenshot."""
    found = set(_TITLE_BAR_VERSION.findall(_doc(USER_DOC)))

    assert found == {_shipped_version()}, (
        "the README shows {} but the plugin is {}".format(sorted(found), _shipped_version())
    )


class _SampleMetadata:
    """A plugin metadata object with the shipped version, for the title bar to read."""

    name = "Mod Update Checker"

    def __init__(self, version: str):
        self.version = version


class _SampleServer:
    def get_self_metadata(self):
        return _SampleMetadata(_shipped_version())


def test_the_readme_sample_screens_start_with_the_bar_the_plugin_draws():
    """The samples are screenshots in text form, and the bar around the version is *drawn*.

    ``_title_line`` sizes the ``=`` from ``_TITLE_WIDTH`` and the length of the name and version,
    so a sample with a hand-counted run of ``=`` is a picture of a screen this plugin does not
    produce. Three of them were exactly that: the width constant grew and the samples kept the
    old bar, which nothing noticed because they were only ever checked for the version number.

    Built from the plugin rather than from the constant, so there is no second copy of the
    arithmetic to fall out of step with the first.
    """
    import mod_update_checker as plugin

    expected = str(plugin._title_line(_SampleServer()))
    found = _TITLE_BAR_LINE.findall(_doc(USER_DOC))

    assert found, "the README has no sample screen left"
    assert set(found) == {expected}, "samples show {}, the plugin draws {}".format(
        sorted(set(found)), expected
    )


def test_the_changelog_leads_with_the_shipped_version():
    """``tools/release.py`` builds the release body from the top ``## `` section.

    So a changelog whose first heading is a version that was never shipped publishes release
    notes for a version that does not exist — and the release itself is still cut from the tag,
    which means nothing else would notice.
    """
    headings = [line for line in _doc("CHANGELOG.md").splitlines() if line.startswith("## ")]

    assert headings, "the changelog has no version sections"
    assert headings[0] == "## v{}".format(_shipped_version()), headings[0]


def test_the_changelog_keeps_only_the_latest_release():
    """The project's rule, stated at the top of the file and enforced here.

    Older entries live on the Releases page because the file ships inside the plugin, and a
    ``.mcdr`` carrying five versions of history is a file every user downloads and nobody reads.
    """
    headings = [line for line in _doc("CHANGELOG.md").splitlines() if line.startswith("## ")]

    assert len(headings) == 1, headings


def test_every_option_the_config_has_is_written_down_somewhere():
    """The other direction from ``test_every_option_path_in_the_docs_exists``.

    That one catches a name in the prose that the config does not have. This catches the
    opposite and quieter problem: an option that exists, and works, and that nobody can find —
    because the only way to learn about it would be to read the source. The failure looks like
    "the feature is missing", which is how a user reports it.
    """
    from support import option_paths

    import mod_update_checker as plugin

    user_doc = _doc(USER_DOC)
    developer_doc = _doc(DEVELOPER_DOC)
    unexplained = sorted(
        path
        for path in option_paths(plugin.Config)
        if path not in user_doc and path not in developer_doc
    )

    assert unexplained == [], "options nobody documents: {}".format(unexplained)


#: Extensions that are not text and therefore not expected to be LF. ``.mcdr`` is a zip with a
#: different name; the rest are assets.
_BINARY_SUFFIXES = frozenset({".mcdr", ".jar", ".png", ".zip", ".pyc", ".pyo"})

#: Directories that are not part of the source: the object store, the unpacked test
#: dependencies, and the caches.
_NOT_SOURCE = frozenset({".git", ".testlibs", "__pycache__", ".pytest_cache"})


def test_no_source_file_carries_windows_line_endings():
    """``.gitattributes`` declares ``eol=lf``; the working tree has to agree with it.

    Git normalises on commit, so a CRLF file is invisible in the history — and that is exactly
    why this needs asserting rather than trusting. The damage is elsewhere: a script that reads
    a file and writes it back (the injection helpers in ``bench/`` do precisely that) flips the
    whole file to CRLF on Windows without changing one character of its content, and the next
    diff is a wall of warnings over a file nobody edited. It also cost a round of confusing
    ``git status`` noise once: the files were listed as modified while ``git diff`` printed
    nothing, because only the stat cache had noticed.

    Scanned rather than derived from ``git ls-files --eol``, because the failure this guards
    against happens *before* a commit — at the moment the file is on disk and wrong.
    """
    import os

    offenders = []
    for root, dirnames, filenames in os.walk(REPO):
        dirnames[:] = [name for name in dirnames if name not in _NOT_SOURCE]
        for name in filenames:
            path = Path(root) / name
            if path.suffix.lower() in _BINARY_SUFFIXES:
                continue
            try:
                if b"\r\n" in path.read_bytes():
                    offenders.append(str(path.relative_to(REPO)))
            except OSError:
                continue

    assert offenders == [], "CRLF line endings in: {}".format(offenders)
