"""The Python-floor check, tested against source it is supposed to catch.

``tools/check_python_floor.py`` is a guard for a promise the repository makes in four places
(``ruff.toml``, this tool's ``FLOOR``, ``README-dev.md`` and the CI ``python-floor`` job), and a
guard that cannot fail is not a guard. It gave a **false pass** on the case it was written for:
``importlib.resources`` exists on 3.8, so an import of it looked fine, while
``importlib.resources.files`` is 3.9+ and raised ``AttributeError`` on the declared floor — which
``i18n.py`` swallowed, leaving a packed install with one language.

So the tests below do two things: run the real tool over the real repository (it must stay
green), and run it over a temporary tree containing the exact spellings it has to catch. The
temporary tree is what makes them regression tests rather than a restatement of the tables.
"""

import ast
import shutil
import subprocess
import sys
from pathlib import Path

TOOL = Path(__file__).resolve().parent.parent / "tools" / "check_python_floor.py"
REPO = TOOL.parent.parent


def _run_checker(python, cwd):
    return subprocess.run(
        [python, str(cwd / "tools" / "check_python_floor.py")],
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
    )


def _tree_with(tmp_path, name, source):
    """A throwaway tree the checker treats as a repository: a copy of the tool, plus ``source``.

    The tool locates its repository from ``__file__``, so the copy is what makes it scan the
    sample rather than the real tree — which also means these tests exercise the real script,
    not a re-implementation of its rules.
    """
    tree = tmp_path / "tree"
    (tree / "tools").mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(TOOL), str(tree / "tools" / "check_python_floor.py"))
    (tree / name).write_text(source, encoding="utf-8")
    return tree


def test_the_repository_itself_is_within_the_floor():
    """The check that CI runs, run the same way — and it has to say so, not just exit 0."""
    result = _run_checker(sys.executable, REPO)

    assert result.returncode == 0, result.stdout
    assert "within the Python 3.8 floor" in result.stdout, result.stdout


def test_a_newer_module_attribute_is_caught(tmp_path):
    """The finding this table was extended for, in the spelling that used to slip through.

    ``importlib.resources`` is importable on 3.8; ``files`` is not an attribute of it there.
    """
    tree = _tree_with(
        tmp_path,
        "i18n.py",
        "import importlib.resources\n"
        "\n"
        "\n"
        "def lang_files(package):\n"
        "    return importlib.resources.files(package)\n",
    )

    result = _run_checker(sys.executable, tree)

    assert result.returncode == 1, result.stdout
    assert "importlib.resources.files" in result.stdout, result.stdout
    assert "3.9" in result.stdout, result.stdout


def test_a_bare_name_bound_from_a_newer_attribute_is_caught(tmp_path):
    """``from importlib.resources import files`` is the same call spelled differently."""
    tree = _tree_with(
        tmp_path,
        "module.py",
        "from importlib.resources import files\n"
        "\n"
        "DATA = files(__package__)\n",
    )

    result = _run_checker(sys.executable, tree)

    assert result.returncode == 1, result.stdout
    assert "importlib.resources.files" in result.stdout, result.stdout


def test_a_guarded_newer_attribute_is_still_reported(tmp_path):
    """A guard around an *import* cannot make an absent attribute appear.

    The module-level table treats ``try: import tomllib / except ImportError`` as handled, which
    is right — the module is either there or it is not. The attribute is different: the module is
    always there, so ``files`` raises inside the same ``except`` and the fallback has to be
    something that actually works.
    """
    tree = _tree_with(
        tmp_path,
        "module.py",
        "try:\n"
        "    import importlib.resources as resources\n"
        "except ImportError:\n"
        "    resources = None\n"
        "\n"
        "\n"
        "def where(package):\n"
        "    return resources.files(package)\n",
    )

    result = _run_checker(sys.executable, tree)

    assert result.returncode == 1, result.stdout
    assert "resources.files" in result.stdout, result.stdout


def test_an_aliased_attribute_is_caught(tmp_path):
    """``import importlib.resources as res`` then ``res.files`` is one rename away from the same bug."""
    tree = _tree_with(
        tmp_path,
        "module.py",
        "import importlib.resources as res\n"
        "\n"
        "DATA = res.files('mod_update_checker')\n",
    )

    result = _run_checker(sys.executable, tree)

    assert result.returncode == 1, result.stdout
    # Reported under the canonical path, whichever alias the file used.
    assert "importlib.resources.files" in result.stdout, result.stdout


def _load_tool():
    """Import ``check_python_floor`` from ``tools/`` without polluting ``sys.path`` for long."""
    sys.path.insert(0, str(TOOL.parent))
    try:
        import check_python_floor as floor  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)
    return floor


def _path_of(floor, expression, names):
    """The dotted path the checker sees for an expression like ``res.files(x)``.

    Walked the same way ``check`` walks the tree — ``ast.Attribute`` first, then a bare name — so
    this observes the real resolution instead of restating it.
    """
    tree = ast.parse(expression)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            candidate = floor.dotted_name(node, names)
            if candidate:
                return candidate
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            return floor.dotted_name(node, names)
    return ""


def test_the_import_table_follows_every_spelling():
    """The name resolution behind the check, exercised directly.

    The subprocess tests prove the check fires; this one proves *why* it fires for four different
    spellings, which is the part a future edit to the import walker would break silently.
    """
    floor = _load_tool()

    source = (
        "import importlib.resources\n"
        "import importlib.resources as res\n"
        "from importlib import resources as r\n"
        "from importlib.resources import files\n"
    )
    names = floor.imported_module_names(ast.parse(source))

    for spelling in (
        "importlib.resources.files(x)",
        "res.files(x)",
        "r.files(x)",
        "files(x)",
    ):
        path = _path_of(floor, spelling, names)
        assert path == "importlib.resources.files", spelling
        assert floor.newer_attribute(path) == "3.9", spelling


def test_a_plain_module_attribute_is_not_reported():
    """No false positives on the ordinary uses, or the check becomes noise and gets ignored."""
    floor = _load_tool()

    source = (
        "import json\n"
        "import os.path\n"
        "from pathlib import Path\n"
        "from typing import Optional\n"
    )
    names = floor.imported_module_names(ast.parse(source))
    for spelling in ("json.loads(x)", "os.path.join(a, b)", "Path(x)", "Optional[int]"):
        path = _path_of(floor, spelling, names)
        assert floor.newer_attribute(path) is None, (spelling, path)
