"""The prose against the code: option names, and the counts the docs quote.

Documentation rots in a specific way here. When the config was grouped into sections, every
option gained a prefix — and the prose kept naming them the old way for two releases, in the
exact places a reader goes to *check* the name (`check.ignored_mods` was still written
``ignored_mods`` in the troubleshooting section). Nothing failed, because prose has no imports
to break.

Same story with the numbers: ``32 项检查`` and ``456 项`` stayed in the README long after both
had moved. A count in prose is a claim, so the ones that can be derived from code are derived
here rather than remembered.

What this file deliberately does *not* do is spell-check the prose. It checks the two things
that have a machine-readable source of truth: option paths, and counts.
"""

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PACKAGE = REPO / "mod_update_checker"

DOCS = ("README.md", "CHANGELOG.md", "tests/README.md")

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
    """The README quotes how many checks the matrix compares across MCDR versions.

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
    assert "{} 项检查逐项一致".format(download_checks) in _doc("README.md"), (
        "the README quotes a different check count than required_keys() reports "
        "({})".format(download_checks)
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

    readme = _doc("README.md")
    missing = sorted(
        name for name in registered
        if "!!modupdate {}".format(name) not in readme
    )
    assert missing == [], "not documented in README.md: {}".format(missing)


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
