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

The upstream (Modrinth) is not contacted: the plugin's API base URL is
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
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parent.parent


def plugin_badge() -> str:
    """``[Mod Update Checker]`` — read from the plugin's own metadata.

    Read rather than written down, because five separate assertions below look for it and a
    rename that misses one of them fails the run in a way that reads like a real defect. That
    is not hypothetical: this tool carried the old Chinese spelling after the plugin stopped
    using it, and the failure looked like "the check never ran".
    """
    metadata = json.loads((REPO / "mcdreforged.plugin.json").read_text(encoding="utf-8"))
    return "[{}]".format(metadata["name"])


PLUGIN_ID = "mod_update_checker"
ROOT_COMMANDS = ("!!modupdate", "!!muc")

# Above this MCDR version the plugin must load; below it there is nothing to test. The
# numbers are compared as tuples of ints.
MINIMUM_MCDR = (2, 13, 0)

# How long to let MCDR start the server, run the delayed check and finish. Generous: a
# cold Python start plus MCDR's own initialisation is most of it.
#
# It has to cover the boot, the delay below, the entire script and the shutdown that follows
# the final ``stop``, so it grows whenever the script does. The kill is a safety net, not the
# plan: a run that hits it loses whatever commands had not been typed yet, and the checks would
# fail for a reason that has nothing to do with the plugin.
RUN_SECONDS = 75

#: How long to wait before typing at the console: the startup check has its own delay
#: (``check.start_delay_seconds``, set to 2 in this config) plus the transfers it makes, and a
#: command typed before it finishes would be answered against a report that does not exist yet.
FEED_DELAY_SECONDS = 25

#: Spacing between console commands. Slow enough that each one's output is written before the
#: next arrives — which is what makes "the notice came from *this* check" a readable claim.
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
    # Echo anything carrying a tellraw back, so the test can inspect what the plugin sent.
    # Matching on "tellraw" *anywhere* in the line matters: MCDR wraps the command as
    # "execute at @p run tellraw <player> {{...}}" on Minecraft 1.13 and newer (it does that to
    # mute the "No player was found" error). An earlier version of this server only matched a
    # leading "tellraw", so the echo never happened and the run wrongly reported that the
    # plugin had sent nothing.
    if "tellraw" in command:
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

#: A step in ``COMMANDS`` that is not typed at the console but done to the instance.
#:
#: run_one replaces it with :func:`unlock_deletion` bound to the folder it is building, so the
#: step and the commands around it stay one ordered script.
UNLOCK_DELETION = object()


#: Commands fed to MCDR's console once the automatic check has had time to finish, in order.
#:
#: The numbering is derived from the scenario, and the three numbers used here are the ones
#: whose status is pinned by ``tests/test_e2e.py``: with the automatic check and download done,
#: ``1`` is Tampered Mod (an update whose file never verifies), ``2`` is Flaky Mod (downloaded,
#: waiting) and ``5`` is Blocked Mod (nothing published for this loader). A change to the
#: scenario that moves them fails these assertions loudly, which is the point.
COMMANDS = [
    # 裸命令 = 帮助页（v1.5.0 起，项目约定），汇总搬到了自己的词上。
    "!!modupdate",
    "!!modupdate summary",
    "!!modupdate help",
    "!!modupdate status",
    "!!modupdate list",
    "!!modupdate list update_available",
    # A trailing page number, on a status that the scenario guarantees is non-empty (the
    # download stage always leaves something waiting to be installed). Before v1.5.0 this was a
    # parse error — and "some listing appeared" would not have noticed, which is why the check
    # on it counts listings instead of looking for one.
    "!!modupdate list awaiting_install 2",
    # The listing is a numbered index now; this is the click target on one of its rows. A
    # number is deliberate here — it is the one place the number path is exercised for real.
    "!!modupdate info 1",
    "!!modupdate reload",
    "!!muc help",
    # --- the on-demand pair, staged and confirmed -------------------------------------
    #
    # Addressed by **name**, not by number, and that is the point rather than a convenience:
    # the listing's numbers depend on what the automatic pass has already fetched, which is a
    # fact about timing. These four commands exist to exercise the two-step flow for three
    # specific mods — one whose bytes never match, one not downloaded, one already fetched —
    # and a number would silently start meaning a different mod the moment anything about the
    # ordering changed. That is exactly what happened when the numbering was fixed, and the run
    # failed for it. Naming them also means a real MCDR parses a multi-word name through the
    # command tree's ``GreedyText`` argument, which nothing else covers.
    #
    # Tampered's bytes never match, so the confirm below really does open a socket and really
    # is refused — and nothing lands in the download folder either way.
    "!!muc download Tampered Mod",
    "!!muc confirm",
    # Not downloaded: must point at ``download`` rather than stage anything.
    "!!muc install Tampered Mod",
    "!!muc install Blocked Mod",
    # --- the bulk forms, staged only ----------------------------------------------------
    #
    # Both are superseded by the explicit single-mod command below, so the batches never get
    # confirmed and the default run stays a statement about *staging* — the one thing a bulk
    # plan must get right, since acting on it is just the single path in a loop.
    #
    # The counts are pinned to the scenario: one mod is still waiting to be fetched (Tampered,
    # whose transfer is refused), and three have been downloaded but not installed (Outdated,
    # Prefixed, Flaky). A bulk command that quietly covered fewer mods than the report lists
    # would still print a plausible plan, so the numbers are asserted rather than the wording.
    "!!muc download all",
    "!!muc install all",
    # Staged and deliberately NOT confirmed in the default run: confirming it would authorise
    # an install, which would move the file the download assertions above are looking at.
    "!!muc install Flaky Mod",
    "!!modupdate check",
    # --- 清理旧版备份：先被开关拒绝一次，再由管理员打开它，最后真的删掉 --------------------
    #
    # 这一段是一个故事，顺序就是故事本身，四步都有断言：
    #
    #   1. ``list old_backup`` —— 备份**看得见**，这一步不受任何开关影响；
    #   2. ``delete <文件名>`` —— ``cleanup.allow_delete`` 还是出厂值（关），所以它必须被拒绝；
    #   3. 场景步骤：把那个选项写开（管理员改配置文件），然后 ``reload``；
    #   4. ``check`` → ``cleanup`` → ``confirm`` —— 提醒出现、计划列出、文件真的没了。
    #
    # 第 2 步的「什么都没发生」由第 4 步反证：``cleanup`` 只在 `retired.jar.old` **还在**
    # 的时候才列得出计划（``cleanup_staged`` 锚在这个文件名上），所以那次被拒的删除若是真删了
    # 东西，后面的计划就列不出来。这比在控制台里数文件名出现几次可靠得多。
    "!!muc list old_backup",
    "!!muc delete retired.jar.old",
    UNLOCK_DELETION,
    "!!muc reload",
    # 解锁之后的第一次检查——提醒只有到这里才允许出现，而那正是要验证的前置关系。
    "!!muc check",
    # 先把计划列出来并要求确认，``confirm`` 才动手。顺序不能反：这两条命令之间如果插进别的
    # ``confirm``，删的就是别的计划了。
    "!!muc cleanup",
    "!!muc confirm",
    # 读一次实时状态：这一行数的是**磁盘**上的备份，所以它同时证明 cleanup 只拿走了过期的
    # 那一个（还剩 1 个），而不是把两个都删了。
    "!!muc status",
    # 同一个锁下面的另一条路径：``delete all`` 不看天数，刚剩下的那个 3 天大的备份正是它的。
    "!!muc delete all",
    "!!muc confirm",
]

def unlock_deletion(root: Path):
    """The admin's edit: write ``cleanup.allow_delete: true`` into the instance's config.

    This is what the run's story turns on — the same file an admin would open, written the same
    way, with a ``reload`` typed right after it. Nothing else in the config is touched, so the
    only difference between the two halves of the run is the switch under test.

    MCDR has already rewritten the file with every option in it by the time this runs, so the
    section is known to exist; ``setdefault`` is there for the case where the healing did not
    happen, which is a thing the run checks for separately.
    """
    def step() -> None:
        path = root / "config" / PLUGIN_ID / "config.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("cleanup", {})["allow_delete"] = True
        write(path, json.dumps(data, indent=2))

    return step

#: The extra step the install run takes: confirm the staged authorisation.
#:
#: Appended rather than part of ``COMMANDS`` because it changes what the stop replaces, and the
#: two modes ask different questions about the download folder — see ``DOWNLOAD_KEYS``.
MANUAL_INSTALL_COMMAND = "!!muc confirm"

#: Substrings that must appear in the console for each command to count as answered. Chinese,
#: because the MCDR instance is pinned to zh_cn and the plugin follows it.
COMMAND_EXPECTATIONS = {
    "summary": plugin_badge(),
    # 裸命令显示帮助页 —— 从帮助页的用法行里认，而不是从「有没有打印东西」认。
    "bare_help": "不带子命令就是本页",
    "help": "!!modupdate list",
    # ASCII colon: the separator is part of the translated label, not hardcoded.
    "status_mc": "服务端: 26.3",
    # The listing is a numbered index: one line per mod plus a clickable detail label.
    # 列表尾部那句提示（v1.6.0 起：原来那句带一个悬空的冒号，拆成了两句）。
    "list_all": "某个 Mod 的详情",
    "list_filtered": "[详细信息]",
    # The detail view a click lands on: a version change and the two links.
    "info_detail": "版本: ",
    "reload": "配置已重载",
    # Runs ``!!muc help`` and looks for a row naming that alias. The status screen cannot
    # serve here any more: both aliases now print the same title bar, so it would pass without
    # proving anything about the alias. Only the ``!!muc`` help says ``!!muc list``.
    "alias": "!!muc list",
    "check_started": "已在后台开始检查",
    # 清理计划的第一行，锚在文件名上：批量计划的计数会变，而这个文件名是场景种下的、
    # 整轮只出现一次的那个。
    "cleanup_staged": "retired.jar.old",
    # ``delete all`` 的标题带「全部」，与 ``cleanup`` 的标题（「即将删除 N 个旧版备份」）不是同一句。
    "delete_all_staged": "即将删除全部 1 个旧版备份",
    # 列表行末尾的状态注释，带 ASCII 括号——状态屏那一行是「旧版备份: 」，提醒里是
    # 「旧版备份（.old）」，只有列表会把状态写进括号，所以这个子串只可能来自一行列表。
    "list_backups": "(旧版备份)",
    # 提醒的开头，**连同计数**：只有 ``cleanup.notice`` 会写「有 N 个旧版备份」，列表写的是
    # ``(旧版备份)``、状态屏写的是「旧版备份: 」。带上计数还让它同时证明「提醒的是 1 个」。
    "cleanup_notice": "有 1 个旧版备份",
    # 开关关着时那条命令的回绝语。它出现的位置本身就是一条证据（见 cleanup_reminded）。
    "cleanup_locked": "删除功能未开启",
    # The staged plan, and the sentence that asks for the confirmation. The timeout itself is
    # left out of the assertion: it is a constant in the plugin, and pinning the number here
    # would make changing it fail a run for no reason.
    #
    # Pinned to the *file* rather than to the ask-for-confirmation line, and that is not
    # decoration: the bulk plan ends with the same sentence, so a substring they share would
    # still be satisfied after the single-mod path broke.
    "download_staged": "文件名：tampered-1.1.0.jar",
    # The bulk forms: a plan whose counts come from the scenario (see ``COMMANDS``).
    "download_all_staged": "即将从 Modrinth 下载 1 个",
    "install_all_staged": "即将安排安装 3 个",
    # The confirm really fetched and really gave up: the served bytes never match their hash.
    "download_refused": "失败",
    # Named, so the sentence is pinned to the mod it is about. The number it ends with is
    # deliberately not pinned here — that number is the listing's, and ``tests/test_mcdr_entry``
    # checks it is the right one without this file having to know the ordering.
    "install_wants_download": "Tampered Mod 的新版本还没下载。请先输入 !!muc download ",
    "install_refused_status": "Blocked Mod 当前状态是「无适配构建」",
    # The single-mod header, whole: the bulk header carries a count between these two words,
    # so the shorter substring would be satisfied by a batch reply with no single install in it.
    "install_staged": "即将安排安装(下次关服时执行)",
    "install_authorised": "已授权",
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


#: The config options this tool is allowed to deviate from ``Config.get_default()`` on, with
#: the reason. Everything else must be left at its shipped default.
#:
#: An allow-list rather than a free-form dict for a specific reason: an earlier version of this
#: file set ``network.cache.enabled: false`` for convenience, which meant the cache path was
#: never executed against a real MCDR — and that path turned out to crash on the shipped
#: default config. Anything that switches a code path off has to be justified here, and
#: ``tests/test_mcdr_entry.py`` fails if the dict below drifts from this list.
#:
#: Dotted paths, because the config file groups its options into sections. The same paths are
#: used to validate the dict, so a typo here cannot turn into a silent no-op.
CONFIG_OVERRIDES = (
    # The fake upstream is not reachable at the real URLs.
    "sources.modrinth.api_base",
    # Waiting the shipped 60 seconds would make every job three times as long.
    "check.start_delay_seconds",
    # A test must not sit in the self-imposed rate limiter, and the retry path is covered by
    # its own unit test.
    "network.requests_per_minute",
    "network.retries",
    # The shipped default is off, and off means the in-game notification path never executes.
    # It builds a message out of arbitrary mod names, so it is worth running rather than
    # trusting — the fake server echoes the command back and the run asserts the payload is
    # valid JSON addressed to the right player.
    "report.in_game",
    # Off by default too. Turned on here because it is the only feature that writes files, and
    # therefore the last one to leave to unit tests: this run proves against a real MCDR that
    # a verified file lands, that a tampered one is refused, and that nothing is written
    # outside the plugin's own folder.
    "download.enabled",
    # Not off, but a non-default *value*: this is how the run proves that a mod the admin
    # excluded is genuinely left alone. The scenario plants one with a newer build upstream,
    # so if the exclusion were ignored it would show up as an update.
    "check.ignored_mods",
    # Off by default, and the one setting that lets the plugin write to mods/. Switched on only
    # by ``--with-install``, which is a separate run because installing moves the fetched files
    # out of the download folder that the default run's assertions inspect.
    "download.install_on_stop",
    # Off by default. Turned on because the reminder is one of the two things this section has
    # to get right, and a message built out of a directory's worth of file names is worth reading
    # once as a real admin would.
    "cleanup.enabled",
)

#: ``cleanup.allow_delete`` is deliberately **absent** from everything above.
#:
#: It is not an oversight and not a convenience: the run starts on the shipped default (deletion
#: off) so that the refusal path is executed, and then ``UNLOCK_DELETION`` writes it on and types
#: ``!!muc reload`` — the same edit an admin would make, in the middle of the same run. Declaring
#: it here would mean writing it before boot and losing the first half of the story; leaving it
#: at the default and flipping it mid-run is what makes "the switch is what changed the answer"
#: a fact about one run rather than a comparison between two.
#:
#: The flip is a *scenario* change, not a config override, which is why it is not in the tuple
#: above — that tuple describes the file this tool writes before the instance starts, and
#: ``tests/test_mcdr_entry.py`` asserts it still does.


def plugin_config(upstream, install: bool = False) -> dict:
    """The config for one MCDR instance: the shipped defaults, plus the allow-list.

    Shaped like the file the plugin actually ships — grouped into sections — so what the run
    exercises is the structure a user edits. A section may list only the options it overrides;
    MCDR fills the rest in from the class defaults, which is itself worth exercising, since
    that is what happens to an admin who upgrades and keeps an older, smaller config file.
    """
    config = {
        "sources": {"modrinth": {"api_base": upstream.modrinth_base}},
        "check": {"start_delay_seconds": 2, "ignored_mods": ["ignored"]},
        "network": {"requests_per_minute": 0, "retries": 0},
        "report": {"in_game": True},
        "download": {"enabled": True},
        # Off by default, and the only feature that deletes anything. Switched on here with a
        # threshold of zero so the seeded backup below is guaranteed to be named whatever the
        # clock says — the run then proves end to end that a real MCDR deletes it after a
        # confirmation, and that nothing else in mods/ is touched.
        # The threshold is left at its shipped value (30 days) on purpose: the two planted
        # backups straddle it, which is what makes ``cleanup`` (expired only) and ``delete all``
        # (everything) different answers in this run rather than two spellings of one.
        "cleanup": {"enabled": True},
    }
    if install:
        # Only the install run switches this on. It has to be a separate run rather than part
        # of the default one because installing *moves the fetched files out of the download
        # folder*, which is exactly what the download assertions look at.
        config["download"]["install_on_stop"] = True
    _ensure_dependencies_importable()
    from support import flatten_options

    unexpected = set(flatten_options(config)) - set(CONFIG_OVERRIDES)
    assert not unexpected, "override not declared in CONFIG_OVERRIDES: {}".format(
        sorted(unexpected)
    )
    return config


#: The part of the listing's tail that every listing prints, whatever it holds. Counted, not
#: searched for: "how many listings came out" is the question the paged command turns on.
LISTING_MARKER = "某个 Mod 的详情"

#: The install summary line, as the plugin writes it. Checked verbatim so a run cannot pass
#: on an install that never announced itself.
#: The marker both install messages carry, so the check does not depend on which of the two
#: a given run happens to print.
INSTALL_LOG_HEADER = plugin_badge() + " 已替换"

#: What the prefixed mod's jar should be called once its note has been carried over.
PREFIXED_INSTALLED_NAME = "[测试-前缀]prefixed-fabric-1.1.0.jar"

#: A build left in the downloads folder from an earlier run, for a mod whose newer build is
#: available now. Seeded so the run has to notice it is superseded, remove it, and fetch the new
#: one — the "an updated mod gets re-downloaded" behaviour, checked against a real MCDR.
STALE_DOWNLOAD = {
    "mod_id": "outdated",
    "file": "outdated-1.0.5.jar",
    "version": "1.0.5",
    "bytes": b"PK\x03\x04 an older build that has since been superseded",
}


#: Two backups, one either side of the 30-day threshold the shipped config ships with.
#:
#: Two rather than one, because the feature under test is the *difference* between the two bulk
#: commands: ``cleanup`` may only take the expired one, ``delete all`` takes both. A single
#: backup could not tell those apart.
SEEDED_BACKUPS = (
    {
        "file": "retired.jar.old",
        "bytes": b"PK\x03\x04 a jar that was replaced and kept aside",
        "age_days": 400,
    },
    {
        "file": "fresh.jar.old",
        "bytes": b"PK\x03\x04 a jar from last week's update",
        "age_days": 3,
    },
)

#: The one ``cleanup`` is about, and the one only ``delete all`` may touch.
SEEDED_BACKUP = SEEDED_BACKUPS[0]
SEEDED_FRESH_BACKUP = SEEDED_BACKUPS[1]


def seed_expired_backup(root: Path) -> None:
    """Plant the backups in ``mods/``, one expired and one not.

    Seeded rather than produced by an install: the install runs at *stop*, which is after every
    command in the run, so a backup created by it could never be seen by ``!!muc cleanup``. The
    ages are stamped well either side of the threshold so neither is ambiguous.
    """
    folder = root / "server" / "mods"
    folder.mkdir(parents=True, exist_ok=True)
    for backup in SEEDED_BACKUPS:
        path = folder / backup["file"]
        path.write_bytes(backup["bytes"])
        stamp = time.time() - (backup["age_days"] * 86400)
        os.utime(str(path), (stamp, stamp))


def seed_stale_download(root: Path) -> None:
    """Plant an older download plus the ledger entry that claims it."""
    folder = root / "config" / PLUGIN_ID / "downloads"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / STALE_DOWNLOAD["file"]).write_bytes(STALE_DOWNLOAD["bytes"])
    write(
        root / "config" / PLUGIN_ID / "download-manifest.json",
        json.dumps(
            {
                "version": 1,
                "mods": {
                    STALE_DOWNLOAD["mod_id"]: {
                        "file": STALE_DOWNLOAD["file"],
                        "sha1": hashlib.sha1(STALE_DOWNLOAD["bytes"]).hexdigest(),
                        "version": STALE_DOWNLOAD["version"],
                        "at": "2026-01-01T00:00:00+00:00",
                    }
                },
            },
            indent=2,
        ),
    )


def build_tree(root: Path, python: str, plugin: Path, upstream, jars, install: bool = False) -> None:
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
        json.dumps(plugin_config(upstream, install=install), indent=2),
    )
    write(root / "permission.yml", PERMISSION_YML)
    seed_stale_download(root)
    seed_expired_backup(root)


def feed_commands(process, delay: float, commands) -> None:
    """Write console commands into MCDR's stdin, spaced out so ordering is observable.

    An entry may be a callable instead of a string: that is a *scenario step* — a change made to
    the instance rather than something typed at its console — and it is invoked in the same slot
    of the same sequence. Keeping both in one ordered script is what lets this run express
    "the switch refuses, then the admin opens it, then it works" as one story; run_one fills the
    step in because it is the one that knows where the instance lives.
    """
    def run() -> None:
        try:
            time.sleep(delay)
            for command in commands:
                if callable(command):
                    command()
                else:
                    process.stdin.write(command + "\n")
                    process.stdin.flush()
                time.sleep(COMMAND_INTERVAL)
            process.stdin.write("stop\n")
            process.stdin.flush()
        except Exception:  # noqa: BLE001 - the process may already be gone
            pass

    threading.Thread(target=run, daemon=True).start()


def run_one(python: str, plugin: Path, workdir: Path, scenario_builder,
            install: bool = False) -> dict:
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
        build_tree(root, python, plugin, upstream, scenario_jars, install=install)

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
        # The script, with the scenario step bound to *this* instance's folder. Built here
        # rather than inside ``feed_commands`` so that two runs of the tool at once cannot end up
        # sharing one root — the step writes a file, and writing it in the wrong tree would look
        # exactly like the plugin ignoring the switch.
        sequence = [
            unlock_deletion(root) if entry is UNLOCK_DELETION else entry
            for entry in COMMANDS
        ]
        if install:
            sequence.append(MANUAL_INSTALL_COMMAND)
        feed_commands(process, delay=FEED_DELAY_SECONDS, commands=sequence)

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
        # What the CDN actually published decides what must and must not land on disk. Taken
        # from the fake upstream rather than inferred from the report's statuses, so the check
        # would still fail if a bug stopped an entry being reported at all.
        result = summarise(
            console, root, version, python,
            expected_downloads={
                name: hashlib.sha1(blob).hexdigest()
                for name, blob in upstream.cdn_files.items()
                if name not in upstream.fail_downloads
                and name not in upstream.unwanted_downloads
            },
            forbidden_downloads=set(upstream.fail_downloads) | set(upstream.unwanted_downloads),
            flaky_downloads=set(upstream.flaky_downloads),
            unwanted_downloads=set(upstream.unwanted_downloads),
            install=install,
            seeded_jars=len(scenario_jars),
        )
        result["root"] = str(root)
        result["upstream_requests"] = len(upstream.request_paths())
        return result
    finally:
        upstream.stop()


def _sha1_of(path: Path) -> str:
    digest = hashlib.sha1()
    try:
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(65536), b""):
                digest.update(block)
    except OSError:
        return ""
    return digest.hexdigest()


def _walk_fragments(payload, colour=None):
    """Every ``(colour, text)`` run in a chat component, depth first.

    MCDR serialises an ``RTextList`` as an object whose ``text`` is a list on some versions and
    as a bare list on others, so both shapes are walked rather than assumed. Assuming an object
    is what made this tool die on 2.13.0 with ``'list' object has no attribute 'get'`` — the
    payload was fine, the reader was not.

    A nested fragment inherits the colour of its parent unless it sets its own, which is how
    Minecraft resolves it too.
    """
    if isinstance(payload, str):
        yield colour, payload
        return
    if isinstance(payload, list):
        for item in payload:
            for pair in _walk_fragments(item, colour):
                yield pair
        return
    if not isinstance(payload, dict):
        return
    own = payload.get("color", colour)
    text = payload.get("text")
    if isinstance(text, str):
        if text:
            yield own, text
    else:
        for pair in _walk_fragments(text, own):
            yield pair
    for pair in _walk_fragments(payload.get("extra"), own):
        yield pair


def _collect_tellraw(console: str) -> tuple:
    """Every ``tellraw`` the plugin sent to the test player, as parsed payloads.

    The fake server echoes any command carrying a tellraw, so the console holds exactly what
    went to the game. Parsing it back is the only way to check the things a "it didn't crash"
    would miss: the command is addressed to the right player, and the payload is well-formed —
    the text is built from mod names, which are attacker-controlled as far as this plugin
    knows.

    The command may be wrapped. MCDR sends ``execute at @p run tellraw <player> {...}`` on
    Minecraft 1.13+, so the tellraw is located by searching for it rather than assumed to be
    at the start; which form was used is reported too, since it is version-dependent.

    Returns ``(texts, fragments, wrapped, errors)``: one string per payload, then every
    ``(colour, text)`` run across all of them flattened. The colours are here because the
    notice is meant to say *what needs doing* by colour, and "which colour did the game
    actually receive" is only answerable from the payload. A malformed command fails the run
    instead of being silently skipped.
    """
    marker = "(tellraw) "
    needle = "tellraw {} ".format(TEST_PLAYER)
    texts = []
    fragments = []
    errors = []
    wrapped = None
    for line in console.splitlines():
        index = line.find(marker)
        if index == -1:
            continue
        command = line[index + len(marker):].strip()
        at = command.find(needle)
        if at == -1:
            errors.append("not addressed to {}: {}".format(TEST_PLAYER, command[:140]))
            continue
        wrapped = command[:at].strip() or "(none)"
        raw = command[at + len(needle):].strip()
        try:
            payload = json.loads(raw)
        except ValueError:
            errors.append("not valid JSON: {}".format(raw[:120]))
            continue
        runs = list(_walk_fragments(payload))
        if not any(text.strip() for _, text in runs):
            errors.append("no text in the payload: {}".format(raw[:120]))
            continue
        fragments.extend(runs)
        texts.append("".join(text for _, text in runs))
    return texts, fragments, wrapped, errors


def summarise(
    console: str,
    root: Path,
    version: str,
    python: str,
    expected_downloads: Optional[dict] = None,
    forbidden_downloads: Optional[set] = None,
    install: bool = False,
    flaky_downloads: Optional[set] = None,
    unwanted_downloads: Optional[set] = None,
    seeded_jars: int = 0,
) -> dict:
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

    # MCDR fills in the options a config file is missing and writes it back, and this run
    # depends on it: the config written before the boot is deliberately incomplete (see
    # ``plugin_config``). The two halves are asserted separately — the file gained the
    # options, and the plugin said so on the console. Either can regress without the other,
    # and both were once invisible, which is what made a stale config file look to a user
    # like a feature that did not exist.
    config_file = plugin_folder / "config.json"
    config_healed = False
    if config_file.is_file():
        try:
            written_config = json.loads(config_file.read_text(encoding="utf-8"))
        except ValueError:
            written_config = {}
        if isinstance(written_config, dict):
            download_section = written_config.get("download")
            sources_section = written_config.get("sources")
            config_healed = (
                isinstance(download_section, dict)
                and "install_on_stop" in download_section
                and isinstance(sources_section, dict)
                and "manual_map" in sources_section
            )

    updates = report.get("counts", {}).get("update_available", 0)
    # Keyed by file name, not mod id: a jar with no mod metadata has an empty mod id, and two
    # jars of one mod share an id — neither is a usable key for "what happened to this jar".
    statuses = {
        entry["file_name"]: entry["status"] for entry in report.get("entries", [])
    }

    # The seeded backup, and whether it is still there. Read off the directory rather than off
    # the log: a message claiming a deletion is worth nothing if the file did not go, and a
    # run that deleted something else would still print the same sentence.
    mods_folder_early = root / "server" / "mods"
    backup_gone = not (mods_folder_early / SEEDED_BACKUP["file"]).exists()
    fresh_gone = not (mods_folder_early / SEEDED_FRESH_BACKUP["file"]).exists()
    jar_count = len(list(mods_folder_early.glob("*.jar")))

    # Where the two markers about the switch sit in the console. Their *order* is the argument:
    # ``!!muc delete`` runs while the switch is still at its shipped default and the reload that
    # opens it comes later, so a reminder printed anywhere before the refusal would mean the
    # reminder is not gated on the switch — and one printed after it can only have come from the
    # check that ran after the reload. A missing notice fails the reminder check, so both
    # directions are covered by the pair below.
    locked_at = console.find(COMMAND_EXPECTATIONS["cleanup_locked"])
    notice_at = console.find(COMMAND_EXPECTATIONS["cleanup_notice"])

    tellraws, fragments, tellraw_wrapper, tellraw_errors = _collect_tellraw(console)
    joined = "\n".join(tellraws)

    # 通知里按**状态**上色：标题白、有更新的行蓝（与列表里同一个状态同一个颜色）、已完成的行灰。
    # 颜色只在 payload 里存在（构造方给的是角色，游戏收到的是成品），所以从 payload 读。
    # v1.5.0 之前整段都是黄的——那一条断言在看着这个。
    headings = {colour for colour, text in fragments if "尚未下载" in text}
    rows = {colour for colour, text in fragments if "->" in text}
    # The listing is counted rather than merely found. Three commands in ``COMMANDS`` end in a
    # listing, and the third one is the point: a trailing page number has to be *accepted by the
    # command tree*, so a run where the argument is refused prints two listings instead of three.
    listings = console.count(LISTING_MARKER)

    # What the auto-download stage actually put on disk, compared against what the CDN
    # published. Every file that is downloadable must be there byte for byte, and the ones that
    # are deliberately served wrong must not be there at all.
    downloads = plugin_folder / "downloads"
    downloaded = sorted(p.name for p in downloads.iterdir()) if downloads.is_dir() else []
    expected_downloads = dict(expected_downloads or {})
    forbidden_downloads = set(forbidden_downloads or set())

    # With install on, the fetched builds have been moved into mods/ — so "was this file
    # verified" is asked of both places. Same assertion, either destination: the point is that
    # the bytes the CDN published are on disk untouched, not where the plugin put them.
    mods_folder = root / "server" / "mods"

    def _find_published(name):
        for folder in (downloads, mods_folder):
            candidate = folder / name
            if candidate.is_file():
                return candidate
        return None

    verified = {}
    for name, digest in expected_downloads.items():
        found = _find_published(name)
        verified[name] = found is not None and _sha1_of(found) == digest
    forbidden_present = sorted(name for name in forbidden_downloads if name in downloaded)
    # Published but not wanted: a mod the config excluded. Named separately from the
    # verification failures, because "we refused a bad file" and "we correctly ignored a mod"
    # are different behaviours and a regression in one should not read as the other.
    ignored_not_fetched = sorted(
        name for name in unwanted_downloads if name not in downloaded
    )
    flaky_downloads = set(flaky_downloads or set())
    unwanted_downloads = set(unwanted_downloads or set())
    # A build that has been fetched is no longer an update to fetch. Asserted on the file name
    # level so that a regression which puts it back into the "not downloaded" list is caught
    # even if the download itself still works.
    awaiting = {
        entry.get("file_name") for entry in report.get("entries", [])
        if entry.get("status") == "awaiting_install"
    }
    announced_updates = {
        entry.get("file_name") for entry in report.get("entries", [])
        if entry.get("status") == "update_available"
        and entry.get("download_filename") in downloaded
    }
    # A partial ``.part`` file counts as left behind: it looks complete to everything
    # downstream, which makes it worse than nothing.
    leftovers = sorted(p.name for p in downloads.iterdir() if p.suffix != ".jar")

    return {
        "mcdr": version,
        "python": python,
        "version_tuple": version_tuple(version),
        # "已加载" proves both that the plugin loaded and that `language: auto` followed MCDR.
        "loaded": "Mod Update Checker] 已加载" in console,
        "refused_cleanly": "不满足版本约束" in console,
        "spoke_chinese": plugin_badge() + " 已加载" in console
        and "[Mod Update Checker] loaded" not in console,
        "check_ran": plugin_badge() + " 服务端 26.3" in console,
        "detected_version": "服务端: 26.3" in console,
        "found_update": updates >= 1,
        "reported_up_to_date": "已是最新" in console or "没有发现更新" in console,
        "no_compatible_build_reported": "无适配构建" in console,
        "report_file_written": report_path.is_file(),
        "report_has_entries": bool(report.get("entries")),
        "report_entry_count": len(report.get("entries", [])),
        "cache_written": cache_path.is_file(),
        "cache_records": cache_records,
        "config_healed": config_healed,
        "config_reported": "配置文件缺少" in console and "已按默认值补上" in console,
        # Two distinct notifications are expected, and their markers differ, so a regression
        # that stops sending one of them cannot be masked by the other still arriving.
        "notify_in_game_sent": plugin_badge() + " 有" in joined,
        "admin_join_notified": plugin_badge() + " 服务端" in joined,
        "notify_payloads_valid": not tellraw_errors,
        "notify_payloads": tellraws,
        "notify_wrapper": tellraw_wrapper,
        "notify_errors": tellraw_errors,
        "notify_colours": sorted({colour for colour, _ in fragments if colour}),
        # 两种颜色，两件事：标题说这一块是什么，行说这一条是什么状态。整段一个颜色会让两半
        # 都失败——这正是这条断言存在的理由（装完的行不该和待处理的行一样扎眼）。
        "notify_coloured_by_role": headings == {"white"} and rows == {"blue"},
        "downloads_written": downloaded,
        "downloads_verified": verified,
        "downloads_leftovers": leftovers,
        "downloads_awaiting": sorted(awaiting),
        "downloads_still_announced": sorted(announced_updates),
        "downloads_forbidden_present": forbidden_present,
        # The checks the run is judged on, reduced from the details above.
        "download_written": bool(downloaded),
        "download_hash_verified": bool(verified) and all(verified.values()),
        "download_tampered_refused": bool(forbidden_downloads) and not forbidden_present,
        # A file the CDN serves wrong twice and then serves properly. It is only on disk
        # because the retry happened, so this is the end-to-end proof that the budget works —
        # with retrying off, or with the per-attempt state not reset, it would never land.
        "download_flaky_recovered": bool(flaky_downloads)
        and all(verified.get(name) for name in flaky_downloads),
        "download_no_leftovers": not leftovers,
        # -- install stage (only meaningful with --with-install) -----------------------
        # Read off the mods folder rather than off the log: a message claiming an install is
        # worth nothing if the files did not actually move.
        "install_logged": INSTALL_LOG_HEADER in console,
        "install_replaced_old_body": (
            (mods_folder / "outdated-1.1.0.jar").is_file()
            and _sha1_of(mods_folder / "outdated-1.1.0.jar")
            == expected_downloads.get("outdated-1.1.0.jar")
        ),
        "install_backup_kept": (mods_folder / "outdated.jar.old").is_file(),
        # The admin's bracket note carried onto the new file name. This is the prefix feature,
        # and the only place it is observable is a real install.
        "install_prefix_kept": (
            (mods_folder / PREFIXED_INSTALLED_NAME).is_file()
            and (mods_folder / "[测试-前缀]prefixed.jar.old").is_file()
        ),
        # The replaced jar is gone from its old name: one jar per mod, not two.
        "install_left_a_single_jar": sorted(
            item.name for item in mods_folder.glob("*outdated*")
        ) == ["outdated-1.1.0.jar", "outdated.jar.old"],
        "install_untouched_mods_intact": (mods_folder / "current.jar").is_file(),
        # A mod the admin excluded is not fetched even though its build is published: that is
        # the whole point of the setting, and with downloading on it is the observable part.
        "download_ignored_not_fetched": bool(unwanted_downloads)
        and len(ignored_not_fetched) == len(unwanted_downloads),
        # The downloaded build must have left the "update to fetch" list, and the notification
        # must say both things: what still needs fetching, and what is fetched but not installed.
        "download_reclassified": bool(awaiting) and not announced_updates,
        # The superseded build planted before the run must be gone, and the new one present.
        # Both halves matter: replacing without removing would leave the folder accumulating
        # every version a mod has been through.
        "stale_download_removed": STALE_DOWNLOAD["file"] not in downloaded,
        "stale_download_replaced": any(
            name != STALE_DOWNLOAD["file"] for name in downloaded
        ),
        "notify_lists_both_groups": (
            ("尚未下载" in joined or "not downloaded" in joined)
            and ("已下载待安装" in joined or "to install" in joined
                 or "已下载待安装" in joined)
        ),
        "statuses": statuses,
        "command_summary": COMMAND_EXPECTATIONS["summary"] in console,
        "command_bare_help": COMMAND_EXPECTATIONS["bare_help"] in console,
        "command_help": COMMAND_EXPECTATIONS["help"] in console,
        "command_status": COMMAND_EXPECTATIONS["status_mc"] in console,
        "command_list_all": COMMAND_EXPECTATIONS["list_all"] in console,
        "command_list_filtered": COMMAND_EXPECTATIONS["list_filtered"] in console,
        "command_list_page_number": listings >= 3,
        # 控制台的行印**版本数字**，不是 ``[版本]`` 标签——上一轮把行形态写死成聊天形态
        # （``chat_form``），控制台因此丢掉了每一个版本号（终端里没有悬停可开）。两半都要：
        # 箭头在（版本行还在），标签不在。这是唯一在**真实控制台 source** 上量这件事的地方。
        "console_rows_keep_versions": "[版本]" not in console and "->" in console,
        # 每屏底部的收尾线，控制台拿到的是一条**素线**（全 ``=``）。标题栏的 ``=`` 只出现在
        # 插件名两侧（十几格），所以四十个连着的 ``=`` 只可能来自收尾线。
        "closing_rule_printed": "=" * 40 in console,
        # 默认配置下删除是关着的：那条命令必须被拒绝，且回绝要说清去改哪个选项。
        "cleanup_refused_while_locked": locked_at != -1,
        # 提醒**且**它只能出现在解锁之后——前置关系的证据。见上面 notice_at 的注释。
        "cleanup_reminded": notice_at != -1 and notice_at > locked_at,
        # 计划里那个文件名同时反证了「被拒的那次没有删掉任何东西」：文件不在了的话，这一行
        # 根本列不出来（``command.cleanup.none`` 会顶掉它）。
        "cleanup_staged": COMMAND_EXPECTATIONS["cleanup_staged"] in console,
        # 确认之后文件真的没了，而且 mods/ 里别的 jar 一个没少。两条缺一不可——只断言
        # 「文件不见了」的话，一个把整个目录删空的实现也能过。
        "cleanup_deleted": backup_gone,
        # ``cleanup`` must not have taken the young one: the status line it printed in between
        # counts the folder, so on a real run "1 of the 2 survived" is a fact about the disk.
        "cleanup_spared_the_fresh_backup": "旧版备份: 1 个" in console,
        "cleanup_left_the_jars_alone": jar_count == seeded_jars,
        # ``delete all`` is the same two steps, and the only thing that sets it apart from
        # ``cleanup`` is that it does not look at the age — so the proof is that a backup three
        # days old was still offered, and is now gone.
        "delete_all_staged": COMMAND_EXPECTATIONS["delete_all_staged"] in console,
        "delete_all_deleted": fresh_gone,
        "command_list_backups": COMMAND_EXPECTATIONS["list_backups"] in console,
        "command_info": COMMAND_EXPECTATIONS["info_detail"] in console,
        "command_reload": COMMAND_EXPECTATIONS["reload"] in console,
        "command_alias": COMMAND_EXPECTATIONS["alias"] in console,
        "command_check": COMMAND_EXPECTATIONS["check_started"] in console,
        "command_download_staged": COMMAND_EXPECTATIONS["download_staged"] in console,
        "command_download_all_staged": COMMAND_EXPECTATIONS["download_all_staged"] in console,
        "command_download_refused": COMMAND_EXPECTATIONS["download_refused"] in console,
        "command_install_wants_download":
            COMMAND_EXPECTATIONS["install_wants_download"] in console,
        "command_install_refused":
            COMMAND_EXPECTATIONS["install_refused_status"] in console,
        "command_install_staged": COMMAND_EXPECTATIONS["install_staged"] in console,
        "command_install_all_staged": COMMAND_EXPECTATIONS["install_all_staged"] in console,
        "command_install_authorised":
            COMMAND_EXPECTATIONS["install_authorised"] in console,
        "mode": "install" if install else "download",
        "modrinth_used": any(entry["platform"] == "modrinth" for entry in report.get("entries", [])),
        "tracebacks": console.count("Traceback (most recent call last)"),
        "plugin_errors": console.count("[Mod Update Checker] 检查失败"),
    }


#: Checks that hold whatever mode the run is in.
CHECK_KEYS = [
    "check_ran",
    "detected_version",
    "found_update",
    "no_compatible_build_reported",
    "report_file_written",
    "report_has_entries",
    "cache_written",
    "config_healed",
    "config_reported",
    "notify_in_game_sent",
    "admin_join_notified",
    "notify_payloads_valid",
    "notify_coloured_by_role",
    "notify_lists_both_groups",
    "command_summary",
    "command_bare_help",
    "command_help",
    "command_status",
    "command_list_all",
    "command_list_filtered",
    "command_list_page_number",
    "console_rows_keep_versions",
    "closing_rule_printed",
    "cleanup_refused_while_locked",
    "cleanup_reminded",
    "cleanup_staged",
    "cleanup_deleted",
    "cleanup_spared_the_fresh_backup",
    "cleanup_left_the_jars_alone",
    "delete_all_staged",
    "delete_all_deleted",
    "command_list_backups",
    "command_info",
    "command_reload",
    "command_alias",
    "command_check",
    "command_download_staged",
    "command_download_all_staged",
    "command_download_refused",
    "command_install_wants_download",
    "command_install_refused",
    "command_install_staged",
    "command_install_all_staged",
    "modrinth_used",
    "spoke_chinese",
]

#: Checks about where a *download* lands, which only apply while nothing installs it.
#:
#: The two modes cannot share one list: install-on-stop moves the fetched files out of the
#: download folder and may rename them, so every assertion about that folder — was it written,
#: was it superseded, is nothing left over — becomes the wrong question rather than a failing
#: one. Each mode asks its own.
DOWNLOAD_KEYS = [
    "download_written",
    "download_hash_verified",
    "download_tampered_refused",
    "download_flaky_recovered",
    "download_no_leftovers",
    "download_ignored_not_fetched",
    "download_reclassified",
    "stale_download_removed",
    "stale_download_replaced",
]

#: Checks about what the *stop* replaced, which only apply with install-on-stop on.
INSTALL_KEYS = [
    "install_logged",
    "install_replaced_old_body",
    "install_backup_kept",
    "install_prefix_kept",
    "install_left_a_single_jar",
    "install_untouched_mods_intact",
    # Only this mode runs ``!!muc install <编号>`` followed by ``!!muc confirm``: the default
    # run stages the same plan and stops short, because carrying it out would authorise an
    # install and move the file its download assertions are inspecting.
    "command_install_authorised",
]


def required_keys(install: bool) -> list:
    """The checks a run is judged on, for the mode it ran in."""
    return CHECK_KEYS + (INSTALL_KEYS if install else DOWNLOAD_KEYS)


def verdict(result: dict) -> str:
    if result["loaded"]:
        keys = required_keys(result.get("mode") == "install")
        problems = [key for key in keys if not result.get(key)]
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
    parser.add_argument(
        "--with-install",
        action="store_true",
        help="also switch on install-on-stop, which moves the fetched builds into mods/",
    )
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
        result = run_one(
            python, plugin, workdir, build_scenario_jars, install=args.with_install
        )
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
        for key in required_keys(results[0].get("mode") == "install") + [
            "report_entry_count", "tracebacks"
        ]:
            values = {str(item[key]) for item in loaded}
            print("    {:<28} {}".format(key, "一致" if len(values) == 1 else "不一致: " + str(values)))

    shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
