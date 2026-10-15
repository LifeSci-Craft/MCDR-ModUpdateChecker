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
the syntax parses as 3.8 and that no annotation, standard-library module or standard-library
*attribute* from a newer version is used, not that the code was executed on 3.8. Running the
suite on a real 3.8 interpreter is what ``mcdr-matrix`` does for MCDR instead of Python; both are
needed, and only the cheap half can be in CI on every push.

The attribute half (``NEWER_MODULE_ATTRIBUTES``) is what catches the case the module half gave a
false pass on: ``importlib.resources`` exists on 3.8, while ``importlib.resources.files`` — which
this repository used, in a module whose own ``except Exception`` hid the ``AttributeError`` — is
3.9+. ``tests/test_python_floor.py`` proves it fires by pointing the checker at a temporary
tree.
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

#: ``(module, attribute)`` pairs where the *attribute* is newer than the floor even though the
#: module is not. This table exists because the module-level one above gave a false pass on the
#: exact failure it was written for: ``importlib.resources`` is present on 3.8, so importing it
#: looked fine, while ``importlib.resources.files`` is 3.9+ and raised ``AttributeError`` at
#: run time — which the plugin's own ``except Exception`` swallowed, degrading a packaged
#: install to English-only on the interpreter the repository declares as its floor.
#:
#: `from ... import x` is caught by both the attribute form and the bare-name form, because the
#: second is how a module re-exports one.
NEWER_MODULE_ATTRIBUTES = {
    ("importlib.resources", "files"): "3.9",
    ("importlib.resources", "as_file"): "3.9",
    ("importlib.resources", "read_text"): "3.9",
    ("importlib.resources", "read_binary"): "3.9",
    ("functools", "cache"): "3.9",
    ("itertools", "pairwise"): "3.10",
    ("contextlib", "aclosing"): "3.10",
    ("dataclasses", "slots"): "3.10",
    ("importlib.metadata", "packages_distributions"): "3.10",
    ("typing", "Self"): "3.11",
    ("typing", "assert_type"): "3.11",
    ("typing", "override"): "3.12",
    ("asyncio", "timeout"): "3.11",
    ("re", "NOFLAG"): "3.11",
    ("datetime", "UTC"): "3.11",
    ("sys", "exception"): "3.11",
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


def imported_module_names(tree):
    """``bound name -> the dotted path it stands for`` for every import in the file.

    Both spellings, because both are ways to reach the attribute this check is about::

        import importlib.resources          # ``importlib.resources.files``
        from importlib import resources     # ``resources.files``
        from importlib.resources import files   # a bare ``files``, which is the same attribute

    Aliases are followed rather than ignored: ``import importlib.resources as res`` is the same
    call, and a check that depends on spelling is one that gets avoided by accident.
    """
    names = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not alias.name:
                    continue
                if alias.asname:
                    names[alias.asname] = alias.name
                else:
                    root = alias.name.split(".")[0]
                    names[root] = root
                    names[alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                if alias.name == "*":
                    continue
                bound = alias.asname or alias.name
                # ``from importlib.resources import files`` binds ``files`` to that attribute,
                # which is the same dotted path the attribute form produces.
                names[bound] = "{}.{}".format(module, alias.name) if module else alias.name
    return names


def dotted_name(node, names):
    """The dotted path ``node`` refers to, or ``""`` when it is not a plain name chain."""
    parts = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return ""
    parts.append(current.id)
    path = ".".join(reversed(parts))
    if "." not in path:
        # A bare name: only the import table can say what it stands for.
        return names.get(path, path)
    head, _, tail = path.partition(".")
    return "{}.{}".format(names.get(head, head), tail)


def newer_attribute(module_path):
    """The version that introduced the attribute ``module_path`` names, or ``None``."""
    if "." not in module_path:
        return None
    parent, _, attribute = module_path.rpartition(".")
    return NEWER_MODULE_ATTRIBUTES.get((parent, attribute))


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
    names = imported_module_names(tree)
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
        if isinstance(node, ast.Attribute):
            # Only attribute chains, not every ``ast.Name``: ``check`` has no name resolution
            # beyond imports, so a bare local variable that happens to be called ``files`` is
            # not the same finding, and reporting it would make the check cry wolf.
            path = dotted_name(node, names)
            since = newer_attribute(path)
            if since is not None:
                problems.append(
                    "{}:{}: {} needs Python {} (the module exists on {}.{}, the attribute does not)"
                    .format(rel, node.lineno, path, since, FLOOR[0], FLOOR[1]))

    for node in ast.walk(tree):
        # Bare names are only interesting where they are *called*: ``from importlib.resources
        # import files`` then ``files(package)`` is the same finding as the attribute form, while
        # a local variable that happens to be spelled the same is not. Without the call check
        # this reports every ``Optional(...)``-looking name in the tree.
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            path = dotted_name(node.func, names)
            since = newer_attribute(path)
            if since is not None:
                problems.append(
                    "{}:{}: {}() needs Python {} (the module exists on {}.{}, the attribute does not)"
                    .format(rel, node.lineno, path, since, FLOOR[0], FLOOR[1]))

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
