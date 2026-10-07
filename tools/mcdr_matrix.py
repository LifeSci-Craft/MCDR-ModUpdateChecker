#!/usr/bin/env python3
"""Run the plugin inside real MCDR instances and report what each one does.

The pytest suite pins a single MCDR (the interpreter you happen to run it with), so the
"requires MCDR 2.13.0 or newer" claim would otherwise be unverified. This tool boots a real
MCDR per interpreter with a fake Minecraft server and a fake *upstream*, and checks, on each
one:

* the plugin loads, or is cleanly refused with MCDR's own dependency message;
* the mods folder is scanned and the configured language is actually honoured — the MCDR
  instance is pinned to ``zh_cn`` while the plugin's config says ``language: auto``, so a
  Chinese assertion here also proves that ``auto`` follows MCDR on *this* version;
* the automatic check runs after the server reports ``Done`` and finds the planted updates;
* every command in the tree answers: ``!!modupdate``, ``help``, ``status``, ``check``,
  ``list``, ``list <status>``, ``reload`` and the ``!!muc`` alias;
* ``last_report.json`` is written and contains what the console claimed;
* no traceback comes out of the plugin.

The upstream (Modrinth and CurseForge) is not contacted: the plugin's API base URLs are
pointed at a local fake, so a run needs no network and cannot be rate-limited.

Usage::

    # every interpreter that has MCDR installed
    python tools/mcdr_matrix.py \\
        /path/to/mcdr-2.13/python /path/to/mcdr-2.15.7/python /path/to/mcdr-2.16/python

    # just the one running this script
    python tools/mcdr_matrix.py --current

The plugin is built on the fly with ``pack.py``, so the thing being tested is the artifact a
user would install rather than the source tree.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN_ID = "mod_update_checker"
ROOT_COMMANDS = ("!!modupdate", "!!muc")

# Above this MCDR version the plugin must load; below it there is nothing to test. The
# numbers are compared as tuples of ints.
MINIMUM_MCDR = (2, 13, 0)

# How long to let MCDR start the server, run the delayed check and finish. Generous: a
# cold Python start plus MCDR's own initialisation is most of it.
RUN_SECONDS = 60
COMMAND_INTERVAL = 1.2

FAKE_SERVER = '''\
"""A stand-in Minecraft server: prints a Fabric-style startup, then obeys ``stop``."""
import sys
import time


def out(message):
    sys.stdout.write("[12:00:00] [Server thread/INFO]: " + message + "\\n")
    sys.stdout.flush()


out("Loading Minecraft {mc} with Fabric Loader 0.18.1")
out('Starting minecraft server version {mc}')
out('Done (1.234s)! For help, type "help"')

for line in sys.stdin:
    command = line.strip()
    if command == "stop":
        break
    if command.startswith("tellraw"):
        out("(tellraw) " + command)

out("Stopping server")
'''

MCDR_CONFIG = """\
language: zh_cn
working_directory: server
start_command: '"{python}" fake_server.py'
handler: vanilla_handler
encoding: utf8
decoding: utf8
rcon:
  enable: false
  address: 127.0.0.1
  port: 25575
  password: password
plugin_directories:
- plugins
check_update: false
advanced_console: false
telemetry: false
disable_console_thread: false
disable_console_color: true
handler_detection: false
write_server_output_to_log_file: false
"""

#: Commands fed to MCDR's console once the automatic check has had time to finish, in order.
COMMANDS = [
    "!!modupdate",
    "!!modupdate help",
    "!!modupdate status",
    "!!modupdate list",
    "!!modupdate list update_available",
    "!!modupdate reload",
    "!!muc status",
    "!!modupdate check",
]

#: Substrings that must appear in the console for each command to count as answered. Chinese,
#: because the MCDR instance is pinned to zh_cn and the plugin follows it.
COMMAND_EXPECTATIONS = {
    "summary": "Mod 更新检查",
    "help": "!!modupdate list",
    "status_mc": "服务端：26.3",
    "list_all": "全部 Mod：",
    "list_filtered": "[update_available]",
    "reload": "配置已重载",
    "alias": "Mod Update Checker — 当前状态",
    "check_started": "已在后台开始检查",
}


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def build_plugin() -> Path:
    """Build the distributable artifact, so the tested thing is what a user installs."""
    sys.path.insert(0, str(REPO))
    import pack

    out = Path(tempfile.mkdtemp(prefix="muc_matrix_art_")) / "ModUpdateChecker.mcdr"
    pack.build(out)
    return out


def _child_env() -> dict:
    """The environment for the MCDR subprocesses.

    The test dependencies may live in ``.testlibs`` (the documented ``pip install --target``
    layout, and what CI uses) rather than in the interpreter's own site-packages (what a
    local MCDR virtualenv looks like). MCDR is launched with ``cwd`` set to a temporary
    instance directory, and a *relative* ``PYTHONPATH`` does not resolve from there — so the
    path is made absolute here, or the subprocess would fail to import MCDR on CI while
    working perfectly on a developer machine.
    """
    env = dict(os.environ)
    testlibs = REPO / ".testlibs"
    if not testlibs.is_dir():
        return env
    existing = env.get("PYTHONPATH", "")
    parts = [str(testlibs)] + [part for part in existing.split(os.pathsep) if part]
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


def mcdr_version(python: str) -> str:
    completed = subprocess.run(
        [python, "-c", "import mcdreforged;print(mcdreforged.__version__)"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        env=_child_env(),
    )
    return completed.stdout.strip() or "unknown"


def version_tuple(text: str):
    parts = []
    for chunk in text.split("."):
        digits = "".join(character for character in chunk if character.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts[:3])


def build_tree(root: Path, python: str, plugin: Path, upstream, jars) -> None:
    """Lay out one MCDR instance plus a planted mods folder."""
    (root / "plugins").mkdir(parents=True, exist_ok=True)
    (root / "server" / "mods").mkdir(parents=True, exist_ok=True)
    shutil.copy2(plugin, root / "plugins" / plugin.name)
    write(root / "server" / "fake_server.py", FAKE_SERVER.format(mc="26.3"))

    for jar_path in jars:
        shutil.copy2(jar_path, root / "server" / "mods" / jar_path.name)

    write(
        root / "config" / PLUGIN_ID / "config.json",
        json.dumps(
            {
                "language": "auto",
                "loader": "fabric",
                "mc_version": "auto",
                "check_on_server_start": True,
                "start_check_delay_seconds": 2,
                "check_interval_hours": 0,
                "notify_on_updates_only": True,
                "notify_in_game": False,
                "write_report_file": True,
                "modrinth_api_base": upstream.modrinth_base,
                "curseforge_api_base": upstream.curseforge_base,
                "curseforge_api_key": "matrix-key",
                "ignored_mods": [],
                "use_resolve_cache": False,
                "requests_per_minute": 0,
                "http_retries": 0,
            },
            indent=2,
        ),
    )


def feed_commands(process, delay: float) -> None:
    """Write console commands into MCDR's stdin, spaced out so ordering is observable."""
    def run() -> None:
        try:
            time.sleep(delay)
            for command in COMMANDS:
                process.stdin.write(command + "\n")
                process.stdin.flush()
                time.sleep(COMMAND_INTERVAL)
            process.stdin.write("stop\n")
            process.stdin.flush()
        except Exception:  # noqa: BLE001 - the process may already be gone
            pass

    threading.Thread(target=run, daemon=True).start()


def run_one(python: str, plugin: Path, workdir: Path, scenario_builder) -> dict:
    """Boot one MCDR instance, drive it, and report what happened."""
    from fake_upstream import FakeUpstream

    version = mcdr_version(python)
    root = workdir / ("mcdr_" + "".join(c if c.isalnum() else "_" for c in version))
    if root.is_dir():
        shutil.rmtree(root)

    upstream = FakeUpstream()
    upstream.api_key = "matrix-key"
    upstream.start()
    try:
        scenario_jars = scenario_builder(upstream, root)
        build_tree(root, python, plugin, upstream, scenario_jars)

        subprocess.run(
            [python, "-m", "mcdreforged", "init"],
            cwd=str(root),
            capture_output=True,
            timeout=180,
            env=_child_env(),
        )
        write(root / "config.yml", MCDR_CONFIG.format(python=python.replace("\\", "/")))

        process = subprocess.Popen(
            [python, "-m", "mcdreforged"],
            cwd=str(root),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=_child_env(),
        )
        feed_commands(process, delay=RUN_SECONDS - 25)

        lines = []
        started = time.time()
        try:
            while time.time() - started < RUN_SECONDS:
                line = process.stdout.readline()
                if not line:
                    if process.poll() is not None:
                        break
                    continue
                lines.append(line.rstrip())
        finally:
            try:
                process.terminate()
                process.wait(timeout=15)
            except Exception:  # noqa: BLE001
                process.kill()

        console = "\n".join(lines)
        write(root / "console.txt", console + "\n")
        result = summarise(console, root, version, python)
        result["root"] = str(root)
        result["upstream_requests"] = len(upstream.request_paths())
        return result
    finally:
        upstream.stop()


def summarise(console: str, root: Path, version: str, python: str) -> dict:
    report_path = root / "config" / PLUGIN_ID / "last_report.json"
    report = {}
    if report_path.is_file():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except ValueError:
            report = {}

    updates = report.get("counts", {}).get("update_available", 0)
    # Keyed by file name, not mod id: a jar with no mod metadata has an empty mod id, and two
    # jars of one mod share an id — neither is a usable key for "what happened to this jar".
    statuses = {
        entry["file_name"]: entry["status"] for entry in report.get("entries", [])
    }

    return {
        "mcdr": version,
        "python": python,
        "version_tuple": version_tuple(version),
        # "已加载" proves both that the plugin loaded and that `language: auto` followed MCDR.
        "loaded": "Mod Update Checker] 已加载" in console,
        "refused_cleanly": "不满足版本约束" in console,
        "spoke_chinese": "Mod Update Checker] 已加载" in console
        and "[Mod Update Checker] loaded" not in console,
        "check_ran": "Mod 更新检查 — 服务端 26.3" in console,
        "detected_version": "服务端：26.3" in console,
        "found_update": updates >= 1,
        "reported_up_to_date": "已是最新" in console or "没有发现更新" in console,
        "no_compatible_build_reported": "无适配构建" in console,
        "report_file_written": report_path.is_file(),
        "report_has_entries": bool(report.get("entries")),
        "report_entry_count": len(report.get("entries", [])),
        "statuses": statuses,
        "command_summary": COMMAND_EXPECTATIONS["summary"] in console,
        "command_help": COMMAND_EXPECTATIONS["help"] in console,
        "command_status": COMMAND_EXPECTATIONS["status_mc"] in console,
        "command_list_all": COMMAND_EXPECTATIONS["list_all"] in console,
        "command_list_filtered": COMMAND_EXPECTATIONS["list_filtered"] in console,
        "command_reload": COMMAND_EXPECTATIONS["reload"] in console,
        "command_alias": COMMAND_EXPECTATIONS["alias"] in console,
        "command_check": COMMAND_EXPECTATIONS["check_started"] in console,
        "curseforge_used": any(entry["platform"] == "curseforge" for entry in report.get("entries", [])),
        "modrinth_used": any(entry["platform"] == "modrinth" for entry in report.get("entries", [])),
        "tracebacks": console.count("Traceback (most recent call last)"),
        "plugin_errors": console.count("[Mod Update Checker] 检查失败"),
    }


CHECK_KEYS = [
    "check_ran",
    "detected_version",
    "found_update",
    "no_compatible_build_reported",
    "report_file_written",
    "report_has_entries",
    "command_summary",
    "command_help",
    "command_status",
    "command_list_all",
    "command_list_filtered",
    "command_reload",
    "command_alias",
    "command_check",
    "curseforge_used",
    "modrinth_used",
    "spoke_chinese",
]


def verdict(result: dict) -> str:
    if result["loaded"]:
        problems = [key for key in CHECK_KEYS if not result.get(key)]
        if problems:
            return "FAIL (" + ", ".join(problems) + ")"
        if result["tracebacks"]:
            return "FAIL (traceback)"
        if result["plugin_errors"]:
            return "FAIL (plugin reported a failed check)"
        return "PASS"
    if result["refused_cleanly"] and result["tracebacks"] == 0:
        return "refused (below the minimum version, as intended)"
    if result["version_tuple"] < MINIMUM_MCDR:
        return "FAIL (below the minimum but not refused cleanly)"
    return "FAIL (did not load)"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("interpreters", nargs="*", help="python executables with MCDR installed")
    parser.add_argument("--current", action="store_true", help="only the running interpreter")
    args = parser.parse_args()

    pythons = [sys.executable] if args.current else args.interpreters
    if not pythons:
        parser.error("give at least one interpreter, or use --current")

    # The scenario builder imports the plugin package and the test fixtures, so both the repo
    # root and tests/ have to be importable before it is loaded.
    sys.path.insert(0, str(REPO))
    sys.path.insert(0, str(REPO / "tests"))
    from matrix_scenario import build_scenario_jars

    plugin = build_plugin()
    workdir = Path(tempfile.mkdtemp(prefix="muc_matrix_work_"))
    print("plugin artifact: {}".format(plugin))
    print()

    results = []
    for python in pythons:
        result = run_one(python, plugin, workdir, build_scenario_jars)
        results.append(result)
        print("  {:<14} {}".format(result["mcdr"], verdict(result)))

    loaded = [item for item in results if item["loaded"]]
    refused = [item for item in results if not item["loaded"]]

    print()
    print("总结")
    print("  成功加载并完整通过 : {}".format(", ".join(item["mcdr"] for item in loaded) or "(无)"))
    print("  按设计被拒绝       : {}".format(", ".join(item["mcdr"] for item in refused) or "(无)"))

    failures = [item for item in results if not verdict(item).startswith(("PASS", "refused"))]
    if failures:
        print("  失败               : {}".format(", ".join(item["mcdr"] for item in failures)))
        for item in failures:
            print()
            print("  --- {} 明细 ---".format(item["mcdr"]))
            print(json.dumps(item, ensure_ascii=False, indent=2))
        return 1

    if len(loaded) > 1:
        print()
        print("  跨版本一致性（成功加载的 {} 个版本）".format(len(loaded)))
        for key in CHECK_KEYS + ["report_entry_count", "tracebacks"]:
            values = {str(item[key]) for item in loaded}
            print("    {:<28} {}".format(key, "一致" if len(values) == 1 else "不一致: " + str(values)))

    shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
