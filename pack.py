#!/usr/bin/env python3
"""Build the distributable ``.mcdr`` package for Mod Update Checker.

Usage::

    python pack.py [output.mcdr]

The packer is deliberately **allowlist-based**. MCDR validates the root entries of a packed
plugin and refuses to load an archive that contains a root-level module such as
``conftest.py`` or ``pack.py``. A denylist-style ``rglob("*")`` packer therefore breaks the
release as soon as anyone adds a file to the repository root, and it also sweeps in
``tests/`` and ``tools/``.

Only these are shipped:

* ``mcdreforged.plugin.json`` — package metadata (required)
* ``mod_update_checker/**.py`` — the plugin code, recursively (submodules included)
* ``mod_update_checker/lang/*.json`` — the message catalogues (one file per language)
* ``LICENSE``, ``CHANGELOG.md`` — licence text and the shipped changelog, which carries only
  the latest release (older ones live on the release page)

The ``.py`` files are shipped with their comments and docstrings blanked out
(``packaged_source()``): the repository keeps them, the artifact does not need them. Line
numbers are preserved, so a traceback from the installed plugin still points at the right
line of the repository file.

``README.md`` is deliberately **excluded**: MCDR never reads it and the release page already
says everything it says.
"""

import ast
import io
import json
import sys
import tokenize
import zipfile
from pathlib import Path

SRC = Path(__file__).resolve().parent

# Zip entries carry a timestamp. Taking it from the file's mtime makes the artifact
# unreproducible: packing the same source twice gives two different hashes, so nobody can
# verify a release by rebuilding it from the tag and comparing sha256.
# 1980-01-01 00:00 is the earliest date a zip can store, and the usual choice for
# "this timestamp carries no information".
FIXED_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)

ROOT_FILES = {
    "mcdreforged.plugin.json",
    "LICENSE",
    "CHANGELOG.md",
}

# Code package: same name as the plugin id.
PACKAGE_NAME = "mod_update_checker"

# Data inside the package that ships alongside the code. The message catalogues must travel
# with the plugin — without them every message degrades to its raw key — and keeping them as
# separate .json files is what lets a translator add a language without touching Python.
PACKAGE_DATA_DIRS = {
    "lang": (".json",),
}


def _is_package_payload(rel: Path) -> bool:
    """True for files inside the plugin package that belong in the artifact."""
    if not rel.parts or rel.parts[0] != PACKAGE_NAME:
        return False
    if rel.suffix == ".py":
        # Every module, at any depth: a silently dropped submodule would produce an artifact
        # that imports fine here and explodes on the user's machine.
        return True
    # A data directory such as ``lang/``: only the extensions we asked for, and only
    # directly inside it.
    if len(rel.parts) == 3 and rel.parts[1] in PACKAGE_DATA_DIRS:
        return rel.suffix in PACKAGE_DATA_DIRS[rel.parts[1]]
    return False


def plugin_version() -> str:
    with open(SRC / "mcdreforged.plugin.json", encoding="utf-8") as handle:
        return json.load(handle)["version"]


def collect() -> list:
    """Return the sorted list of files to ship."""
    files = []
    for path in SRC.rglob("*"):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts or ".git" in path.parts:
            continue
        if path.suffix == ".pyc":
            continue
        rel = path.relative_to(SRC)
        if rel.parent == Path(".") and rel.name in ROOT_FILES:
            files.append(path)
        elif _is_package_payload(rel):
            files.append(path)
    return sorted(files)


def _blank_comments(source: str) -> str:
    """Replace every ``#`` comment with nothing, **keeping the line itself**.

    Uses :mod:`tokenize` rather than a regex, so a ``#`` inside a string literal (a URL, a
    regex, a colour code) is left alone. Blanking instead of deleting keeps line numbers
    identical to the repository file.
    """
    lines = source.splitlines(keepends=True)
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            row, col = token.start
            line = lines[row - 1]
            newline = "\n" if line.endswith("\n") else ""
            lines[row - 1] = line[:col].rstrip() + newline
    return "".join(lines)


def _blank_docstrings(source: str) -> str:
    """Same treatment for docstrings.

    A body whose only statement was the docstring gets ``pass`` instead, otherwise the
    result would not compile.
    """
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = node.body
        if not body:
            continue
        first = body[0]
        if not (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            continue
        # ``def f(): "doc"`` puts the docstring on the header line; blanking that line would
        # delete the definition. Leave such (rare) forms untouched.
        header_line = getattr(node, "lineno", None)
        if header_line is not None and first.lineno == header_line:
            continue
        lines[first.lineno - 1] = (
            " " * first.col_offset + "pass\n" if len(body) == 1 else "\n"
        )
        for index in range(first.lineno, first.end_lineno):
            lines[index] = "\n"
    return "".join(lines)


def packaged_source(path: Path) -> bytes:
    """The bytes to ship for one ``.py`` file: comments and docstrings blanked out.

    Line numbers are preserved on purpose — an exception from the installed plugin then
    reports the same line as the repository file it came from.

    The result is compiled once here: shipping a file that does not parse would only be
    discovered by a user, and this is the last place that can catch it.
    """
    source = path.read_text(encoding="utf-8")
    stripped = _blank_comments(_blank_docstrings(source))
    try:
        compile(stripped, str(path), "exec")
    except SyntaxError as error:  # pragma: no cover - a bug in the stripper
        raise SystemExit("stripping {} produced invalid code: {}".format(path, error))
    return stripped.encode("utf-8")


def build(out_path: Path) -> Path:
    files = collect()
    if not files:
        raise SystemExit("nothing to pack; is mcdreforged.plugin.json present?")
    mandatory = SRC / "mcdreforged.plugin.json"
    if mandatory not in files:
        raise SystemExit("mcdreforged.plugin.json must be in the package")
    if not any(p.parent == SRC / PACKAGE_NAME for p in files):
        raise SystemExit("{} contains no .py files".format(PACKAGE_NAME))
    if not any(
        p.suffix == ".json" and p.parent.name in PACKAGE_DATA_DIRS for p in files
    ):
        raise SystemExit("no language catalogues found; every message would be a raw key")

    # Compression level 9 only affects packing time. Deflate's *decompression* speed does
    # not depend on the level (the format is unchanged; only the encoder works harder), so
    # this costs the user nothing.
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            rel = path.relative_to(SRC).as_posix()
            data = packaged_source(path) if path.suffix == ".py" else path.read_bytes()
            # Hand-built ZipInfo rather than archive.write(): mtime, permission bits and
            # host system all have to be pinned, or the same source packs into different
            # bytes on a different day (or a different OS).
            info = zipfile.ZipInfo(rel, date_time=FIXED_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3              # unix
            info.external_attr = 0o644 << 16    # regular file, rw-r--r--
            archive.writestr(info, data)
    return out_path


def main() -> int:
    if len(sys.argv) > 1:
        out_path = Path(sys.argv[1]).resolve()
    else:
        out_path = SRC / "ModUpdateChecker-v{}.mcdr".format(plugin_version())

    build(out_path)
    size_kib = out_path.stat().st_size / 1024
    print("packed {} files -> {} ({:.1f} KiB)".format(len(collect()), out_path, size_kib))
    return 0


if __name__ == "__main__":
    sys.exit(main())
