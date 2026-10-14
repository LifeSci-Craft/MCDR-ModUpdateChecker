#!/usr/bin/env python3
"""Check that every shipped or tested file still runs on the Python the plugin claims.

``ruff.toml`` sets ``target-version = "py38"`` because that is MCDR 2.13.0's floor, and the
README points at it. A configuration file is not a check, though: this repository's CI unit
matrix starts at 3.10, so nothing there would notice a ``list[str]`` annotation, a ``dict | None``
union or a ``str.removeprefix`` call slipping in. Those are exactly the changes that look
harmless on a modern interpreter and break the plugin for the people who are still on the floor.

Usage::

    python tools/check_python_floor.py

Exits non-zero and prints one line per offending file/line. It is a **static** check: it proves
the syntax parses as 3.8 and that no annotation or standard-library name from a newer version is
used, not that the code was executed on 3.8. Running the suite on a real 3.8 interpreter is what
``mcdr-matrix`` does for MCDR instead of Python; both are needed, and only the cheap half can be
in CI on every push.
"""

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FLOOR = (3, 8)

# Directories that are not source. ``.testlibs`` is a pip-installed copy of MCDR and pytest;
# the caches hold binaries with no suffix.
SKIP_DIRS = {".git", ".testlibs", "__pycache__", ".pytest_cache", ".ruff_cache", "dist"}

#: Standard-library modules that did not exist (or were not importable) on the floor, mapped to
#: the version that introduced them. A module in this set is only acceptable behind an
#: ``ImportError`` guard, which this script cannot see through — so it is reported and the
#: report is expected to name the guard. ``tomllib`` is the known case in this repository.
NEWER_MODULES = {
    "zoneinfo": "3.9",
    "graphlib": "3.9",
    "tomllib": "3.11",
    "exceptiongroup": "3.11",
}

#: Builtin generics (PEP 585) and the union operator (PEP 604) are only legal in annotations
#: whose evaluation is deferred. Collected at runtime they raise on 3.8, so they are flagged.
BUILTIN_GENERICS = {"list", "dict", "set", "tuple", "frozenset", "type"}

#: ``str`` / ``bytes`` / ``int`` methods younger than the floor.
NEWER_ATTRIBUTES = {
    "removeprefix": "3.9",
    "removesuffix": "3.9",
    "bit_count": "3.10",
    "is_integer": "3.12",
}


def iter_sources():
    for path in sorted(REPO.rglob("*.py")):
        rel = path.relative_to(REPO)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        yield path, rel


def annotation_nodes(tree):
    """Yield every node that is evaluated as an annotation."""
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign):
            yield node.annotation
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in list(node.args.args) + list(node.args.kwonlyargs) + list(node.args.posonlyargs):
                if arg.annotation is not None:
                    yield arg.annotation
            if node.returns is not None:
                yield node.returns


def guarded_imports(tree):
    """Import nodes that sit in a ``try`` whose handlers catch ImportError.

    A newer module reached through such a guard is the intended pattern (``tomllib`` is imported
    that way so a 3.8 interpreter can fall back to a regex reader), so it must not be reported.
    Anything else is unguarded and would raise at import time on the floor.
    """
    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    guarded = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        child = node
        parent = parents.get(child)
        while parent is not None:
            if isinstance(parent, ast.Try):
                catches_import_error = any(
                    (handler.type is None)
                    or (isinstance(handler.type, ast.Name)
                        and handler.type.id in ("ImportError", "ModuleNotFoundError", "Exception"))
                    or isinstance(handler.type, ast.Tuple)
                    for handler in parent.handlers
                )
                if catches_import_error and child in parent.body:
                    guarded.add(id(node))
                    break
            child, parent = parent, parents.get(parent)
    return guarded


def check(path, rel, problems):
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(rel), feature_version=FLOOR)
    except SyntaxError as error:
        problems.append("{}:{}: syntax is not Python {}.{} ({})".format(
            rel, error.lineno, FLOOR[0], FLOOR[1], error.msg))
        return

    guarded = guarded_imports(tree)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                root = (alias.name or "").split(".")[0]
                since = NEWER_MODULES.get(root)
                if since is not None and id(node) not in guarded:
                    problems.append(
                        "{}:{}: imports {} (added in Python {}) without an ImportError guard"
                        .format(rel, node.lineno, root, since))
        if isinstance(node, ast.Attribute) and node.attr in NEWER_ATTRIBUTES:
            problems.append("{}:{}: .{}() needs Python {}".format(
                rel, node.lineno, node.attr, NEWER_ATTRIBUTES[node.attr]))

    for node in annotation_nodes(tree):
        for inner in ast.walk(node):
            if isinstance(inner, ast.BinOp) and isinstance(inner.op, ast.BitOr):
                problems.append("{}:{}: 'X | Y' annotation needs Python 3.10 (PEP 604)".format(
                    rel, inner.lineno))
            if isinstance(inner, ast.Subscript) and isinstance(inner.value, ast.Name):
                if inner.value.id in BUILTIN_GENERICS:
                    problems.append("{}:{}: '{}[...]' annotation needs Python 3.9 (PEP 585)".format(
                        rel, inner.lineno, inner.value.id))


def main():
    problems = []
    checked = 0
    for path, rel in iter_sources():
        checked += 1
        check(path, rel, problems)
    if problems:
        print("{} file(s) break the Python {}.{} floor:\n".format(len(problems), *FLOOR))
        for line in problems:
            print("  " + line)
        return 1
    print("{} file(s) parse and annotate within the Python {}.{} floor".format(checked, *FLOOR))
    return 0


if __name__ == "__main__":
    sys.exit(main())
