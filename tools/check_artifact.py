#!/usr/bin/env python3
"""Verify the packed ``.mcdr``: reproducible, minimal, and actually loadable.

Three separate things are checked, because each has a different way of going wrong quietly:

* **Reproducibility.** The packer pins zip timestamps and permission bits so that packing the
  same source twice yields byte-identical output. That is what lets anyone verify a release by
  rebuilding it from the tag and comparing a hash — a claim worth testing rather than
  believing, since a single stray ``mtime`` silently destroys it.
* **Contents.** Only the plugin payload, the metadata, the licence and the changelog ship. A
  deny-list packer would eventually sweep in ``tests/`` or a stray ``conftest.py``, and MCDR
  refuses to load an archive with a root-level module — so the absence is asserted.
* **Loadability.** The archive's ``mcdreforged.plugin.json`` parses, declares the expected id
  and version, and every ``.py`` inside still compiles after the comment/docstring stripper has
  been over it. The stripper uses ``tokenize`` and ``ast``; shipping a file it mangled would
  otherwise be discovered by a user.

Usage::

    python tools/check_artifact.py            # build twice into a temp dir and check
    python tools/check_artifact.py path.mcdr  # check an existing artifact
"""

import argparse
import hashlib
import json
import sys
import tempfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import pack  # noqa: E402

#: Everything that may appear in the archive. Anything else is a bug in the packer.
EXPECTED_ROOT_FILES = {"mcdreforged.plugin.json", "LICENSE", "CHANGELOG.md"}

#: Root-level entries that must never ship. MCDR rejects a packed plugin containing a
#: root-level module, so these are not merely untidy — they break loading.
FORBIDDEN_ROOTS = (
    "README.md",
    "README_en.md",
    "pack.py",
    "conftest.py",
    "pytest.ini",
    "tests/",
    "tools/",
    "docs/",
    "__pycache__/",
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_reproducible(workdir: Path) -> Path:
    """Pack twice and require identical bytes. Returns the first artifact."""
    first = workdir / "first.mcdr"
    second = workdir / "second.mcdr"
    pack.build(first)
    pack.build(second)

    digest_first = sha256(first)
    digest_second = sha256(second)
    assert digest_first == digest_second, (
        "packing the same source twice produced different bytes:\n"
        "  {}\n  {}\n"
        "Something in the zip is not pinned — a timestamp, a permission bit, or the order "
        "of the entries.".format(digest_first, digest_second)
    )
    print("reproducible    : {}".format(digest_first))
    return first


def check_contents(archive_path: Path) -> None:
    """Only the payload ships, and only from inside the plugin package."""
    with zipfile.ZipFile(archive_path) as archive:
        names = sorted(archive.namelist())
        assert names, "the archive is empty"

        found = set(names)
        for required in ("mcdreforged.plugin.json",):
            assert required in found, "{} is missing from the archive".format(required)

        for prefix in FORBIDDEN_ROOTS:
            offenders = [name for name in names if name.startswith(prefix)]
            assert not offenders, "{} should not be shipped: {}".format(prefix, offenders)

        roots = {name for name in names if "/" not in name}
        unexpected = roots - EXPECTED_ROOT_FILES
        assert not unexpected, "unexpected root entries: {}".format(sorted(unexpected))

        # Every .py must live inside the plugin package.
        stray = [name for name in names if name.endswith(".py") and not name.startswith("mod_update_checker/")]
        assert not stray, "python files outside the plugin package: {}".format(stray)

        # The language catalogues have to travel with the code, or every message degrades to
        # its raw key.
        catalogues = [name for name in names if name.startswith("mod_update_checker/lang/")]
        assert catalogues, "no language catalogues in the archive"

        print("files           : {}".format(len(names)))
        for name in names:
            print("  {:52} {:>7} B".format(name, archive.getinfo(name).file_size))


def check_loadable(archive_path: Path) -> None:
    """Metadata parses and every shipped module still compiles after stripping."""
    with zipfile.ZipFile(archive_path) as archive:
        metadata = json.loads(archive.read("mcdreforged.plugin.json").decode("utf-8"))
        assert metadata.get("id"), metadata
        assert metadata.get("version"), metadata
        assert metadata.get("dependencies", {}).get("mcdreforged"), (
            "no mcdreforged dependency declared: {}".format(metadata)
        )
        print("plugin id       : {} v{}".format(metadata["id"], metadata["version"]))
        print("mcdreforged dep : {}".format(metadata["dependencies"]["mcdreforged"]))

        for name in archive.namelist():
            if not name.endswith(".py"):
                continue
            source = archive.read(name).decode("utf-8")
            try:
                compile(source, name, "exec")
            except SyntaxError as error:
                raise AssertionError(
                    "{} does not compile inside the archive: {}".format(name, error)
                ) from error
        print("modules compile : all .py entries")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("artifact", nargs="?", help="an existing .mcdr; built here if omitted")
    args = parser.parse_args()

    if args.artifact:
        path = Path(args.artifact).resolve()
        if not path.is_file():
            raise SystemExit("no such file: {}".format(path))
        print("checking        : {}".format(path))
        check_contents(path)
        check_loadable(path)
    else:
        with tempfile.TemporaryDirectory(prefix="muc_artifact_") as folder:
            workdir = Path(folder)
            path = check_reproducible(workdir)
            check_contents(path)
            check_loadable(path)

    print()
    print("all artifact checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
