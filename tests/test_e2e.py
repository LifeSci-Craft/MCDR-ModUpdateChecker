"""End-to-end test: boot a real MCDR with the real plugin artifact.

Everything else in this suite stops at the module boundary — the checker is exercised against
a fake upstream, but nothing loads the plugin the way MCDR does, registers its command tree,
or observes its event handling. This test closes that gap: it builds the actual ``.mcdr`` with
``pack.py``, drops it into a real MCDR instance next to a fake Minecraft server, and checks
what the console says.

It is the only test that would catch a plugin that imports cleanly, passes every unit test and
still fails to load, so it is worth the fifty seconds it takes. It is marked ``e2e`` so it can
be deselected while iterating: ``pytest -m "not e2e"``.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _load_matrix_tool():
    """Import ``tools/mcdr_matrix.py`` by path.

    ``tools/`` is a directory of scripts rather than an importable package, so loading it by
    path is the honest way to reuse the one implementation of "boot MCDR and drive it" rather
    than keeping a second copy in sync.
    """
    path = REPO / "tools" / "mcdr_matrix.py"
    spec = importlib.util.spec_from_file_location("mcdr_matrix_tool", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _interpreter() -> str:
    """The interpreter MCDR will be booted with, or skip if there is not one.

    ``MCDR_TEST_PYTHON`` lets the suite be pointed at a specific MCDR build (used to check the
    declared minimum version); otherwise the interpreter running the tests is tried.
    """
    candidate = os.environ.get("MCDR_TEST_PYTHON", sys.executable)
    probe = os.access(candidate, os.X_OK) or Path(candidate).is_file()
    if not probe:
        pytest.skip("no usable interpreter at {}".format(candidate))
    return candidate


def _has_mcdr(python: str, tool) -> bool:
    """Would MCDR be importable by the child process this test is about to launch?

    Asks the tool rather than guessing, because the answer depends on the environment: under
    the CI layout MCDR lives in ``.testlibs`` and has to be added to ``PYTHONPATH``; against a
    local MCDR virtualenv it must *not* be, or the version under test would be shadowed.
    """
    import subprocess

    completed = subprocess.run(
        [python, "-c", "import mcdreforged"],
        capture_output=True,
        timeout=180,
        env=tool._child_env(python),
    )
    return completed.returncode == 0


@pytest.mark.e2e
def test_the_packed_plugin_works_inside_a_real_mcdr(tmp_path):
    from matrix_scenario import build_scenario_jars

    tool = _load_matrix_tool()
    python = _interpreter()
    if not _has_mcdr(python, tool):
        pytest.skip("{} cannot import mcdreforged".format(python))

    plugin = tool.build_plugin()
    result = tool.run_one(python, plugin, tmp_path, build_scenario_jars)

    verdict = tool.verdict(result)
    detail = json.dumps(result, ensure_ascii=False, indent=2)

    assert result["loaded"], "the plugin did not load:\n" + detail
    assert result["refused_cleanly"] is False
    assert result["tracebacks"] == 0, "a traceback reached the console:\n" + detail
    assert result["plugin_errors"] == 0

    # The check ran and produced the outcomes that were planted.
    assert result["check_ran"], detail
    assert result["detected_version"], detail
    assert result["found_update"], detail
    assert result["no_compatible_build_reported"], detail
    assert result["report_file_written"] and result["report_has_entries"], detail
    assert result["modrinth_used"] and result["curseforge_used"], detail

    # Both channels resolved the mods they were supposed to.
    #
    # ``outdated.jar`` is the interesting one: the run downloads it, so by the time the report
    # is written its build is on disk and it must no longer be described as an update waiting
    # to be fetched — that is the whole point of the awaiting_install status.
    assert result["statuses"].get("outdated.jar") == "awaiting_install", detail
    # ``flaky.jar`` had its transfer corrupted twice before succeeding, so it is on disk only
    # because the retry budget was honoured — and its per-attempt hash state was reset, or the
    # third attempt would have failed verification forever.
    assert result["statuses"].get("flaky.jar") == "awaiting_install", detail
    # ``cfonly.jar`` is on CurseForge only, so its download is skipped and it stays an update.
    assert result["statuses"].get("cfonly.jar") == "update_available", detail
    # ``tampered.jar`` is served with bytes that never match, so it stays an update to fetch
    # however many times it is retried.
    assert result["statuses"].get("tampered.jar") == "update_available", detail
    assert result["statuses"].get("current.jar") == "up_to_date", detail
    assert result["statuses"].get("blocked.jar") == "no_compatible_build", detail
    assert result["statuses"].get("library.jar") == "not_a_mod", detail

    # Every command is reachable, including the alias.
    for key in (
        "command_summary",
        "command_help",
        "command_status",
        "command_list_all",
        "command_list_filtered",
        "command_reload",
        "command_alias",
        "command_check",
    ):
        assert result[key], "{} failed:\n{}".format(key, detail)

    assert verdict.startswith("PASS"), verdict


@pytest.mark.e2e
def test_language_auto_follows_mcdr(tmp_path):
    """The MCDR instance is pinned to ``zh_cn`` while the plugin's config says
    ``language: auto``.

    This is the only place that proves ``auto`` really consults MCDR rather than defaulting to
    English, and it fails loudly if ``get_mcdr_language()`` ever goes missing.
    """
    from matrix_scenario import build_scenario_jars

    tool = _load_matrix_tool()
    python = _interpreter()
    if not _has_mcdr(python, tool):
        pytest.skip("{} cannot import mcdreforged".format(python))

    plugin = tool.build_plugin()
    result = tool.run_one(python, plugin, tmp_path, build_scenario_jars)

    assert result["spoke_chinese"], json.dumps(result, ensure_ascii=False, indent=2)
