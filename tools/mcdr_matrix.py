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

#: The player the fake server announces, so the in-game notification path has a recipient.
TEST_PLAYER = "MucTester"

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
# Announced before the plugin's delayed check fires, so the in-game notification has an
# online recipient to target. The shape is what MCDR's player-joined regex expects.
out("{player}[/127.0.0.1:41234] logged in with entity id 42 at (0.0, 64.0, 0.0)")
time.sleep(0.4)

for line in sys.stdin:
    command = line.strip()
    if command == "stop":
        break
    # Echo the command back so the test can see what the plugin sent, and check the payload.
    if command.startswith("tellraw"):
        out("(tellraw) " + command)

out("Stopping server")
'''

#: MCDR's permission file. The test player is an owner so the notification is addressed to
#: them — the plugin only messages players at or above ``notify_in_game_permission``.
PERMISSION_YML = """\
default_level: user
owner:
- {player}
admin: []
helper: []
user: []
guest: []
""".format(player=TEST_PLAYER)

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


def _ensure_dependencies_importable() -> None:
    """Put this repo and ``.testlibs`` on ``sys.path`` for the *driver* process.

    The driver is not just a launcher: it imports the scenario builder, which imports the
    plugin package, which imports MCDR. So the driver needs MCDR too — and asking whoever runs
    this to get ``PYTHONPATH`` right is a footgun, as two bashes of mine demonstrated
    (``.testlibs:tests`` is mangled by MSYS path conversion, and a relative entry does not
    survive the ``cwd`` change into a temp instance directory).

    Done here rather than documented, so ``python tools/mcdr_matrix.py`` works from a clean
    checkout in either layout: dependencies in ``.testlibs`` or in the interpreter.
    """
    for entry in (REPO, REPO / "tests", REPO / ".testlibs"):
        text = str(entry)
        if entry.is_dir() and text not in sys.path:
            sys.path.insert(0, text)


def build_plugin() -> Path:
    """Build the distributable artifact, so the tested thing is what a user installs."""
    _ensure_dependencies_importable()
    import pack

    out = Path(tempfile.mkdtemp(prefix="muc_matrix_art_")) / "ModUpdateChecker.mcdr"
    pack.build(out)
    return out


#: Memoised "can this interpreter import mcdreforged by itself?" answers, keyed by python path.
_OWN_MCDR: dict = {}


def _interpreter_has_own_mcdr(python: str) -> bool:
    """Can ``python`` import MCDR *without* help from ``.testlibs``?"""
    if python not in _OWN_MCDR:
        probe = subprocess.run(
            [python, "-c", "import mcdreforged"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            # A clean environment on purpose: this question is exactly "what does this
            # interpreter have on its own?".
            env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
        )
        _OWN_MCDR[python] = probe.returncode == 0
    return _OWN_MCDR[python]


def _child_env(python: str) -> dict:
    """The environment for the MCDR subprocesses run with ``python``.

    ``.testlibs`` may hold MCDR (the documented ``pip install --target`` layout, and what CI
    uses) or may hold only pytest (the layout a developer with MCDR virtualenvs ends up with).
    Deciding which matters a lot, and getting it wrong is silent:

    * If the interpreter has **no** MCDR of its own, ``.testlibs`` must provide it — otherwise
      the child cannot even start. This is the CI layout.
    * If the interpreter **does** have its own MCDR, ``.testlibs`` must NOT be added. A
      PYTHONPATH entry precedes site-packages, so a ``.testlibs/mcdreforged`` would shadow the
      version being tested — and the matrix would cheerfully report "5 versions pass" while
      running one version five times. That happened: every interpreter reported 2.16.0.

    So the probe decides, once per interpreter.

    ``cwd`` is also why the path must be absolute: the children run in temporary instance
    directories, where a relative ``.testlibs`` does not resolve.
    """
    env = dict(os.environ)
    testlibs = REPO / ".testlibs"

    if not testlibs.is_dir() or _interpreter_has_own_mcdr(python):
        # Drop any inherited PYTHONPATH too: the same shadowing argument applies to whatever
        # the caller exported.
        env.pop("PYTHONPATH", None)
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
        env=_child_env(python),
    )
    return completed.stdout.strip() or "unknown"


def version_tuple(text: str):
    parts = []
    for chunk in text.split("."):
        digits = "".join(character for character in chunk if character.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts[:3])


#: The plugin-config keys this tool is allowed to deviate from ``Config.get_default()`` on,
#: with the reason. Everything else must be left at its shipped default.
#:
#: This is an allow-list rather than a free-form dict for a specific reason: an earlier
#: version of this file set ``use_resolve_cache: False`` for convenience, which meant the
#: cache path was never executed against a real MCDR — and that path turned out to crash on
#: the shipped default config. Anything that switches a code path off has to be justified
#: here, and ``tests/test_mcdr_entry.py`` fails if the dict drifts from this list.
CONFIG_OVERRIDES = (
    # The fake upstream is not reachable at the real URLs.
    "modrinth_api_base",
    "curseforge_api_base",
    "curseforge_api_key",
    # Waiting the shipped 60 seconds would make every job three times as long.
    "start_check_delay_seconds",
    # A test must not sit in the self-imposed rate limiter, and the retry path is covered by
    # its own unit test.
    "requests_per_minute",
    "http_retries",
    # The shipped default is off, and off means the tellraw path never executes. It builds a
    # command out of arbitrary mod names and sends it with ``server.execute``, so it is worth
    # running rather than trusting — the fake server echoes the command back and the run
    # asserts the payload is valid JSON.
    "notify_in_game",
)


def plugin_config(upstream) -> dict:
    """The config for one MCDR instance: the shipped defaults, plus the allow-list."""
    config = {
        "modrinth_api_base": upstream.modrinth_base,
        "curseforge_api_base": upstream.curseforge_base,
        "curseforge_api_key": "matrix-key",
        "start_check_delay_seconds": 2,
        "requests_per_minute": 0,
        "http_retries": 0,
        "notify_in_game": True,
    }
    unexpected = set(config) - set(CONFIG_OVERRIDES)
    assert not unexpected, "override not declared in CONFIG_OVERRIDES: {}".format(unexpected)
    return config


def build_tree(root: Path, python: str, plugin: Path, upstream, jars) -> None:
    """Lay out one MCDR instance plus a planted mods folder."""
    (root / "plugins").mkdir(parents=True, exist_ok=True)
    (root / "server" / "mods").mkdir(parents=True, exist_ok=True)
    shutil.copy2(plugin, root / "plugins" / plugin.name)
    write(
        root / "server" / "fake_server.py",
        FAKE_SERVER.format(mc="26.3", player=TEST_PLAYER),
    )

    for jar_path in jars:
        shutil.copy2(jar_path, root / "server" / "mods" / jar_path.name)

    write(
        root / "config" / PLUGIN_ID / "config.json",
        json.dumps(plugin_config(upstream), indent=2),
    )
    write(root / "permission.yml", PERMISSION_YML)


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
            env=_child_env(python),
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
            env=_child_env(python),
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


def _inspect_tellraw(console: str) -> tuple:
    """Find the in-game notification the plugin sent and validate its payload.

    The fake server echoes ``tellraw`` commands it receives, so the console carries the exact
    command that went to the game. Parsing it back out is the only way to check the two things
    that matter and that a mere "it didn't crash" would miss: the argument is addressed to the
    right player, and the JSON is well-formed — the payload is built from mod names, which are
    attacker-controlled text as far as this plugin is concerned.
    """
    marker = "(tellraw) tellraw {} ".format(TEST_PLAYER)
    for line in console.splitlines():
        index = line.find(marker)
        if index == -1:
            continue
        raw = line[index + len(marker):].strip()
        try:
            payload = json.loads(raw)
        except ValueError:
            return False, "not valid JSON: {}".format(raw[:120])
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            return False, "no text in the payload: {}".format(raw[:120])
        return True, text
    return False, "no tellraw command was sent"


def summarise(console: str, root: Path, version: str, python: str) -> dict:
    plugin_folder = root / "config" / PLUGIN_ID
    report_path = plugin_folder / "last_report.json"
    report = {}
    if report_path.is_file():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except ValueError:
            report = {}

    # The resolve cache is enabled by default, and its code path is only reachable when it is
    # — so whether the file appeared is a real check, not decoration. An earlier version of
    # this tool disabled the cache, and that hid a crash on the shipped default config.
    cache_path = plugin_folder / "resolve-cache.json"
    cache_records = 0
    if cache_path.is_file():
        try:
            cache_records = len(json.loads(cache_path.read_text(encoding="utf-8")).get("records") or {})
        except (ValueError, AttributeError):
            cache_records = -1

    updates = report.get("counts", {}).get("update_available", 0)
    # Keyed by file name, not mod id: a jar with no mod metadata has an empty mod id, and two
    # jars of one mod share an id — neither is a usable key for "what happened to this jar".
    statuses = {
        entry["file_name"]: entry["status"] for entry in report.get("entries", [])
    }

    tellraw_valid, tellraw_text = _inspect_tellraw(console)

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
        "cache_written": cache_path.is_file(),
        "cache_records": cache_records,
        "notify_in_game_sent": tellraw_valid,
        "notify_in_game_text": tellraw_text,
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
    "cache_written",
    "notify_in_game_sent",
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

    # The scenario builder imports the plugin package and the test fixtures; the helper puts
    # the repo, tests/ and .testlibs on sys.path so this works without a hand-set PYTHONPATH.
    _ensure_dependencies_importable()
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

    # A distinct interpreter that reports a version already seen means the run is not testing
    # what it claims. This is not hypothetical: pointing ``.testlibs`` at a Python where it
    # shadows the interpreter's own MCDR made five different interpreters all report 2.16.0,
    # and the summary said "5 versions pass" while running one version five times. A warning
    # would have been easy to skim past, so this is fatal.
    seen: dict = {}
    for item in results:
        key = item["mcdr"]
        if key in seen and seen[key] != item["python"]:
            raise SystemExit(
                "FAILED: two different interpreters both report MCDR {}:\n"
                "  {}\n  {}\n"
                "Something is shadowing the per-interpreter MCDR — most likely a "
                "PYTHONPATH/.testlibs MCDR that takes precedence over site-packages. "
                "This run would not compare versions.".format(
                    key, seen[key], item["python"]
                )
            )
        seen[key] = item["python"]

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
