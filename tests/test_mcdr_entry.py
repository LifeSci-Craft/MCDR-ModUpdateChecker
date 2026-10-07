"""The MCDR entry module: how it hooks the lifecycle, and what it must not do twice.

Two of the defects this file guards against are invisible at runtime — no exception, no
warning, just wrong behaviour that only shows up as an extra request or a check that stops
firing — so they are pinned here rather than left to a review.
"""

import ast
import hashlib
import json
import time
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "mod_update_checker"
REPO = PACKAGE.parent
ENTRY = PACKAGE / "__init__.py"

#: MCDR discovers entry-module handlers by these names (see MCDRPluginEvents).
EVENT_HANDLERS = (
    "on_load",
    "on_unload",
    "on_server_startup",
    "on_server_stop",
    "on_player_joined",
    "on_player_left",
)


class _FakeLogger:
    def __init__(self):
        self.messages = []

    def info(self, message, *_args, **_kwargs):
        self.messages.append(str(message))

    def debug(self, *_args, **_kwargs):
        pass

    def warning(self, message, *_args, **_kwargs):
        self.messages.append("WARN " + str(message))

    def error(self, message, *_args, **_kwargs):
        self.messages.append("ERROR " + str(message))

    def exception(self, *_args, **_kwargs):
        pass


class _FakeServer:
    """Just enough of PluginServerInterface to run the entry module head-less.

    Covers ``on_load`` (data folder, MCDR config, language, registration) and the player
    notification paths (``is_server_running``, ``get_permission_level``, ``tell``), which is
    what the on-join feature needs in order to be exercised without booting MCDR.
    """

    def __init__(self, tmp_path, levels=None, running=True):
        self.logger = _FakeLogger()
        self._folder = Path(tmp_path) / "config" / "mod_update_checker"
        self._folder.mkdir(parents=True, exist_ok=True)
        self._mcdr_config = {"working_directory": str(tmp_path), "language": "zh_cn"}
        self.help_messages = []
        self.commands = []
        #: name -> MCDR permission level. An absent name raises, like an unknown player.
        self._levels = dict(levels or {})
        self._running = running
        #: (player, text) for every message this server delivered.
        self.delivered = []

    def get_data_folder(self):
        return str(self._folder)

    def load_config_simple(self, file_name=None, *, target_class=None, **_kwargs):
        return target_class.get_default()

    def get_mcdr_config(self):
        return dict(self._mcdr_config)

    def get_mcdr_language(self):
        return self._mcdr_config["language"]

    def is_server_running(self):
        return self._running

    def register_help_message(self, prefix, message, permission=0):
        self.help_messages.append((prefix, message, permission))

    def register_command(self, node, **_kwargs):
        self.commands.append(node)

    def get_permission_level(self, name):
        if name not in self._levels:
            raise KeyError("no permission level for {!r}".format(name))
        return self._levels[name]

    def tell(self, player, text, **_kwargs):
        self.delivered.append((player, str(text)))

    def told(self, player=None):
        return [text for name, text in self.delivered if player is None or name == player]


def _entry_source() -> str:
    return ENTRY.read_text(encoding="utf-8")


def test_the_lifecycle_handlers_exist_with_mcdr_names():
    """MCDR finds these by name on the entry module. A rename silently unhooks the event."""
    import mod_update_checker as plugin

    for name in EVENT_HANDLERS:
        assert callable(getattr(plugin, name, None)), "{} is missing or not callable".format(name)


def test_event_handlers_are_not_registered_explicitly_as_well():
    """Registering an event handler explicitly *duplicates* it.

    MCDR's ``register_event_listener`` during plugin loading only *stages* the listener, and
    ``_register_default_listeners`` then registers the staged ones **and** every module-level
    function named after an event. So an explicit registration alongside a correctly named
    function installs the handler twice, and the event fires twice — a second post-startup
    check thread on every server start, with no error to show for it.

    Checked on the syntax tree rather than the raw text so that a mention in a comment or a
    docstring cannot trip it.
    """
    tree = ast.parse(_entry_source())
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "register_event_listener":
            offenders.append(node.lineno)
    assert offenders == [], (
        "on_load must not call register_event_listener (lines {}); name the handler after "
        "the event instead and let MCDR register it".format(offenders)
    )


def test_load_stops_the_previous_modules_scheduler():
    """A reload hands the old module to ``on_load`` and never fires ``on_unload``.

    Verified in MCDR's own ``plugin_manager``: ``__reload_plugin`` calls ``plugin.reload()``
    directly, with no ``PLUGIN_UNLOADED`` dispatch and no ``__unload_plugin``. So the outgoing
    module's scheduler thread is still alive, and if ``on_load`` does not stop it, every
    reload leaves one more thread running checks against this server forever.
    """
    tree = ast.parse(_entry_source())
    loader = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "on_load"
    )
    body = ast.dump(loader)
    assert "_stop_scheduler" in body, "on_load does not call into the previous module"
    assert "prev_module" in body, "on_load ignores the previous module handed to it"


def test_the_stop_event_is_cleared_before_a_new_scheduler_starts(tmp_path, monkeypatch):
    """``!!modupdate reload`` stops the old thread and then starts a new one.

    If the stop event is not cleared in between, the new loop's very first ``wait`` returns
    immediately and the thread exits — silently disabling interval checking until the next
    MCDR restart. Reproduced here by setting the event, starting the scheduler, and checking
    that a live thread actually came up.
    """
    import mod_update_checker as plugin

    class _Config:
        enabled = True
        check_interval_hours = 1
        notify_on_updates_only = True

    monkeypatch.setattr(plugin, "_config", _Config(), raising=False)
    plugin._stop_scheduler()            # leaves _stop_event set, exactly as a reload does
    assert plugin._stop_event.is_set()

    server = _FakeServer(tmp_path)
    try:
        plugin._start_interval_scheduler(server)
        assert not plugin._stop_event.is_set(), "a stale stop event survived"
        thread = plugin._scheduler_thread
        assert thread is not None and thread.is_alive()
        assert thread.daemon, "a scheduler thread must not keep the process alive"
    finally:
        plugin._stop_scheduler()

    assert not plugin._scheduler_thread


def test_no_scheduler_thread_when_the_interval_is_zero(tmp_path, monkeypatch):
    import mod_update_checker as plugin

    class _Config:
        enabled = True
        check_interval_hours = 0
        notify_on_updates_only = True

    monkeypatch.setattr(plugin, "_config", _Config(), raising=False)
    plugin._scheduler_thread = None
    plugin._start_interval_scheduler(_FakeServer(tmp_path))
    assert plugin._scheduler_thread is None


def test_stopping_twice_is_harmless(monkeypatch):
    """Both ``on_unload`` and the next ``on_load`` may ask for a stop; that must be safe."""
    import mod_update_checker as plugin

    plugin._stop_scheduler()
    plugin._stop_scheduler()
    assert plugin._scheduler_thread is None


def test_on_load_counts_jars_without_hashing_them(tmp_path, monkeypatch):
    """``on_load`` must count, not read.

    It runs on MCDR's plugin-loading thread, so walking and hashing every jar there would
    block MCDR's startup and every ``!!MCDR reload plugin`` for as long as it takes to read
    the whole ``mods/`` folder — seconds on a real modpack. A fixture of five tiny jars makes
    that invisible, which is exactly why it is pinned: the first version of this plugin did
    hash the whole folder during load.
    """
    import mod_update_checker as plugin

    from support import fabric_metadata, write_jar

    mods = Path(tmp_path) / "mods"
    mods.mkdir(parents=True)
    for index in range(3):
        write_jar(
            mods / "mod{}.jar".format(index),
            fabric=fabric_metadata(id="mod{}".format(index)),
        )
    (mods / "stale.jar.disabled").write_bytes(b"not a jar")

    def explode(*_args, **_kwargs):
        raise AssertionError("on_load hashed the mods folder; that belongs in the check")

    monkeypatch.setattr(plugin, "scan_mods", explode, raising=False)
    plugin._stop_event.clear()

    server = _FakeServer(tmp_path)
    plugin.on_load(server, None)

    joined = "\n".join(server.logger.messages)
    assert "3" in joined, joined                    # the three jars were counted
    assert "mod0.jar" not in joined                 # and not one of them was opened
    assert len(server.commands) == 2                # both root aliases registered
    assert {prefix for prefix, _m, _p in server.help_messages} == {"!!modupdate", "!!muc"}

    plugin._stop_scheduler()


def test_on_load_warns_when_the_mods_folder_is_missing(tmp_path):
    import mod_update_checker as plugin

    plugin._stop_event.clear()
    server = _FakeServer(tmp_path)
    plugin.on_load(server, None)

    joined = "\n".join(server.logger.messages)
    assert "WARN" in joined and "mods" in joined, joined

    plugin._stop_scheduler()


def test_config_defaults_are_the_documented_ones():
    """The README's "works out of the box" claim depends on these exact values."""
    import mod_update_checker as plugin

    config = plugin.Config.get_default()
    assert config.modrinth_api_base == ""          # official endpoint
    assert config.use_modrinth is True
    assert config.mc_version == "auto"
    assert config.loader == "fabric"
    assert config.language == "auto"
    assert config.mods_directory == ""
    assert config.check_on_server_start is True
    assert config.check_interval_hours == 0        # no surprise periodic load
    assert config.notify_in_game is False          # never broadcast to players by default
    assert config.notify_on_updates_only is True
    assert config.command_permission_level == 3
    assert config.use_resolve_cache is True          # and therefore must work for a user
    assert config.requests_per_minute == 240         # under Modrinth's documented 300/min
    # The auto-download feature writes files, so "off unless asked for" is part of the
    # contract rather than a preference.
    assert config.download_updates is False
    assert config.download_folder_name == "downloads"
    assert config.download_max_size_mb == 128
    # Extra attempts after the first, matching the ``http_retries`` convention.
    assert config.download_retries == 3
    # A day, not half an hour: an admin logging in wants the answer, and the answer from
    # yesterday is still the answer unless something has been installed since.
    assert config.admin_join_max_report_age_minutes == 1440


def test_the_end_to_end_run_uses_the_shipped_defaults():
    """The real-MCDR run must execute the config a user actually gets.

    This test exists because of a specific escape. The matrix tool used to write a config with
    ``use_resolve_cache: False`` for convenience; that disabled the cache code path, so it was
    never executed against a real MCDR, and it turned out to crash on the *default* config. A
    green end-to-end run therefore proved nothing about the shipped behaviour.

    Every override now has to be declared in ``CONFIG_OVERRIDES`` with a reason, and this
    test fails if the dict drifts from that list — so switching a code path off cannot happen
    silently again.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "mcdr_matrix", REPO / "tools" / "mcdr_matrix.py"
    )
    assert spec is not None and spec.loader is not None
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)

    class _Upstream:
        modrinth_base = "http://127.0.0.1:1/v2"

    config = tool.plugin_config(_Upstream())

    assert set(config) == set(tool.CONFIG_OVERRIDES), (
        "the matrix config drifted from its declared overrides:\n"
        "  extra: {}\n  missing: {}".format(
            sorted(set(config) - set(tool.CONFIG_OVERRIDES)),
            sorted(set(tool.CONFIG_OVERRIDES) - set(config)),
        )
    )

    # The keys whose defaults must be in force, i.e. absent from the override dict. These are
    # the ones that decide whether a code path runs at all.
    for key in (
        "use_resolve_cache",   # the crash that hid here
        "enabled",
        "write_report_file",
        "notify_on_updates_only",
        "loader",
        "language",
        "mc_version",
        "mods_directory",
        "check_on_server_start",
        # Where files are written must stay at the shipped value: pointing the run at some
        # other folder would leave the download assertions looking at an empty directory and
        # quietly passing.
        "download_folder_name",
        "download_max_size_mb",
        # And so must the retry budget, so the matrix keeps exercising the number a user gets.
        "download_retries",
    ):
        assert key not in config, (
            "{} must stay at its shipped default in the end-to-end run, otherwise the "
            "behaviour a user gets is never exercised".format(key)
        )

    # ``notify_in_game`` is the one exception, and a deliberate one: its default is off, and
    # off means the tellraw path never executes. That path builds a command out of mod names
    # and sends it with ``server.execute``, so it is worth running. The override is declared
    # above, and the run asserts the payload is valid JSON addressed to the right player.
    assert config["notify_in_game"] is True

    # ``download_updates`` is off by default for the same reason, and switched on here because
    # it is the only feature that writes files — the last one to leave to unit tests alone.
    assert config["download_updates"] is True

    # And every override has to be one the plugin's own config class knows about, so a typo
    # cannot silently become a no-op.
    import mod_update_checker as plugin

    known = set(plugin.Config.get_field_annotations())
    unknown = set(tool.CONFIG_OVERRIDES) - known
    assert not unknown, "overrides for options that do not exist: {}".format(sorted(unknown))


# --------------------------------------------------------------------------------------
# Admin-on-join checks and notification
# --------------------------------------------------------------------------------------


def _report_with(entries):
    """A report holding exactly ``entries``."""
    from mod_update_checker.report import Report
    from mod_update_checker.serverinfo import ServerContext

    return Report(
        generated_at="2026-01-01T00:00:00+00:00",
        server=ServerContext(mc_version="26.3", mc_version_source="config", loader="fabric"),
        mods_directory="server/mods",
        entries=list(entries),
    )


def _report(updates=2, age_seconds=0.0):
    """A report with ``updates`` pending updates, produced ``age_seconds`` ago."""
    from datetime import datetime, timedelta, timezone

    from mod_update_checker.report import Report, UpdateEntry
    from mod_update_checker.serverinfo import ServerContext

    produced = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    report = Report(
        generated_at=produced.isoformat(timespec="seconds"),
        server=ServerContext(mc_version="26.3", mc_version_source="config", loader="fabric"),
        mods_directory="server/mods",
    )
    for index in range(updates):
        report.entries.append(
            UpdateEntry(
                mod_id="mod{}".format(index),
                name="Mod {}".format(index),
                file_name="mod{}.jar".format(index),
                local_version="1.0.0",
                latest_version="1.1.0",
                status="update_available",
            )
        )
    return report


@pytest.fixture
def entry_env(tmp_path, monkeypatch):
    """The plugin module wired to a fake server, with a recording check function."""
    import mod_update_checker as plugin

    class _Config:
        enabled = True
        check_on_admin_join = True
        admin_join_permission = 3
        admin_join_max_report_age_minutes = 30
        notify_on_updates_only = True
        notify_in_game = True
        notify_in_game_permission = 3
        start_check_delay_seconds = 60

    monkeypatch.setattr(plugin, "_config", _Config(), raising=False)
    plugin._stop_event.clear()
    plugin._online_players.clear()
    monkeypatch.setattr(plugin, "_last_report", None, raising=False)

    server = _FakeServer(tmp_path, levels={"Admin": 4, "Helper": 2, "Guest": 0})
    calls = []

    def fake_run(server, source=None, announce_clean=True, broadcast=True):
        calls.append({"announce_clean": announce_clean, "broadcast": broadcast})
        return _report()

    monkeypatch.setattr(plugin, "_run_check", fake_run, raising=False)
    return plugin, server, calls


def test_an_admin_joining_with_no_report_runs_a_check_and_is_told(entry_env, tmp_path):
    plugin, server, calls = entry_env

    plugin._admin_join_worker(server, "Admin")

    assert len(calls) == 1, "a check should have run"
    # No separate broadcast: this admin is already being messaged with the same figures.
    assert calls[0]["broadcast"] is False
    told = server.told("Admin")
    assert len(told) == 1, told
    assert "服务端 26.3" in told[0]
    assert "Mod 0" in told[0] and "1.0.0 -> 1.1.0" in told[0]


def test_a_recent_report_is_reused_instead_of_rechecking(entry_env):
    """Newest first: an admin logging in should not wait for a scan, and should not cause
    one either when the answer is minutes old."""
    plugin, server, calls = entry_env
    plugin._last_report = _report(updates=1, age_seconds=120)

    plugin._admin_join_worker(server, "Admin")

    assert calls == [], "no check should have run for a 2-minute-old report"
    told = server.told("Admin")
    assert len(told) == 1
    assert "2 分钟前" in told[0], told[0]
    # 120 seconds is well inside the 24-hour default, so nothing was re-fetched.


def test_a_stale_report_triggers_a_fresh_check(entry_env):
    plugin, server, calls = entry_env
    plugin._last_report = _report(updates=1, age_seconds=25 * 3600)

    plugin._admin_join_worker(server, "Admin")

    assert len(calls) == 1, "a 25-hour-old report is past the 24-hour window"
    assert "分钟前" not in server.told("Admin")[0]


def test_the_window_can_be_switched_off(entry_env, monkeypatch):
    """``0`` means "always re-check", which is the literal reading of the feature."""
    plugin, server, calls = entry_env
    monkeypatch.setattr(plugin._config, "admin_join_max_report_age_minutes", 0)
    plugin._last_report = _report(updates=1, age_seconds=1)

    plugin._admin_join_worker(server, "Admin")

    assert len(calls) == 1


def test_a_non_admin_gets_nothing(entry_env):
    plugin, server, calls = entry_env

    plugin.on_player_joined(server, "Helper", None)
    plugin.on_player_joined(server, "Guest", None)
    assert calls == []
    assert server.delivered == []
    # Both are still tracked as online, which the broadcast notification relies on.
    assert {"Helper", "Guest"} <= plugin._online_players


def test_an_admin_joining_is_checked_against_the_permission_level(entry_env):
    plugin, server, calls = entry_env

    plugin.on_player_joined(server, "Admin", None)

    # The worker runs on its own thread; wait briefly for it rather than assume.
    for _ in range(100):
        if server.told("Admin"):
            break
        time.sleep(0.02)
    assert server.told("Admin"), "the admin was never told"
    assert len(calls) == 1


def test_an_unknown_player_does_not_raise(entry_env):
    """A permission lookup can fail for a name MCDR does not know."""
    plugin, server, calls = entry_env

    plugin.on_player_joined(server, "SomeoneMCDRDoesNotKnow", None)

    assert calls == []
    assert "SomeoneMCDRDoesNotKnow" in plugin._online_players


def test_the_feature_can_be_switched_off(entry_env, monkeypatch):
    plugin, server, calls = entry_env
    monkeypatch.setattr(plugin._config, "check_on_admin_join", False)

    plugin.on_player_joined(server, "Admin", None)

    assert calls == []


def test_a_disabled_plugin_does_nothing_on_join(entry_env, monkeypatch):
    plugin, server, calls = entry_env
    monkeypatch.setattr(plugin._config, "enabled", False)

    plugin.on_player_joined(server, "Admin", None)

    assert calls == []


def test_a_check_that_cannot_start_still_answers(entry_env, monkeypatch):
    """If another check holds the lock, the admin gets the previous report, not silence."""
    plugin, server, calls = entry_env
    # Past the reuse window, so a check is genuinely attempted — and refused the lock.
    previous = _report(updates=1, age_seconds=25 * 3600)

    monkeypatch.setattr(plugin, "_run_check", lambda *a, **k: None, raising=False)
    plugin._last_report = previous

    plugin._admin_join_worker(server, "Admin")

    told = server.told("Admin")
    assert len(told) == 1, told
    assert "Mod 0" in told[0]


def test_no_report_at_all_is_stated_plainly(entry_env, monkeypatch):
    plugin, server, calls = entry_env
    monkeypatch.setattr(plugin, "_run_check", lambda *a, **k: None, raising=False)
    plugin._last_report = None

    plugin._admin_join_worker(server, "Admin")

    told = server.told("Admin")
    assert len(told) == 1
    assert "还没有任何检查结果" in told[0]


def test_nothing_is_sent_while_the_server_is_stopped(entry_env):
    """A message that cannot be delivered must not be attempted, nor crash the thread."""
    plugin, _, _ = entry_env
    stopped = _FakeServer(Path("."), levels={"Admin": 4}, running=False)
    plugin._last_report = _report(updates=1)

    plugin._admin_join_worker(stopped, "Admin")

    assert stopped.delivered == []


def test_an_unloaded_plugin_does_not_send(entry_env):
    plugin, server, calls = entry_env
    plugin._last_report = _report(updates=1)
    plugin._stop_event.set()
    try:
        plugin._admin_join_worker(server, "Admin")
    finally:
        plugin._stop_event.clear()

    assert server.delivered == []
    assert calls == []


def test_a_clean_report_says_so_rather_than_nothing(entry_env):
    plugin, server, _calls = entry_env
    plugin._last_report = _report(updates=0)

    plugin._admin_join_worker(server, "Admin")

    told = server.told("Admin")
    assert len(told) == 1
    assert "没有发现更新" in told[0]


def test_the_join_message_respects_the_update_cap(entry_env):
    """A server with 200 outdated mods must not put 200 lines in someone's chat box."""
    from mod_update_checker import NOTIFY_MAX_UPDATES

    plugin, server, _calls = entry_env
    plugin._last_report = _report(updates=NOTIFY_MAX_UPDATES + 5)

    plugin._admin_join_worker(server, "Admin")

    told = server.told("Admin")[0]
    listed = [line for line in told.splitlines() if "1.0.0 -> 1.1.0" in line]
    assert len(listed) == NOTIFY_MAX_UPDATES, len(listed)
    assert "5 个" in told, "the remainder should be summarised, not silently dropped"


def test_broadcast_and_join_use_their_own_permission_settings(entry_env, monkeypatch):
    """The two thresholds mean different things and must not be folded into one."""
    plugin, server, _calls = entry_env
    plugin._online_players.update({"Admin", "Helper"})
    monkeypatch.setattr(plugin._config, "notify_in_game_permission", 4)

    plugin._notify_in_game(server, _report(updates=1))

    assert server.told("Admin"), "the level-4 admin should be told"
    assert not server.told("Helper"), "the level-2 helper should not be"


# --------------------------------------------------------------------------------------
# Auto-download wiring
# --------------------------------------------------------------------------------------


def test_downloads_land_in_a_folder_of_the_plugins_own(tmp_path):
    """Inside the plugin's data folder, and named exactly as configured."""
    import mod_update_checker as plugin

    server = _FakeServer(tmp_path)
    config = plugin.Config.get_default()
    folder, reason = plugin.resolve_download_folder(server, config)

    assert reason == ""
    assert folder is not None
    assert folder == Path(server.get_data_folder()) / "downloads"


@pytest.mark.parametrize(
    "name",
    ["../server/mods", "../..", "server/mods", "/absolute", "C:\\Windows", "a/b", "..", ""],
)
def test_the_download_folder_cannot_be_aimed_outside_the_plugin(tmp_path, name):
    """The setting is a folder *name*, and that is what makes this safe.

    If it took a path, an admin could silently point it at the live server's ``mods``
    directory — which would load jars straight into a running server, the exact thing this
    plugin exists to avoid. Rejected with a reason, so the log explains itself.
    """
    import mod_update_checker as plugin

    server = _FakeServer(tmp_path)
    config = plugin.Config.get_default()
    config.download_folder_name = name

    folder, reason = plugin.resolve_download_folder(server, config)

    assert folder is None
    assert reason, "a refusal has to say why"


def _downloaded_entry(tmp_path, folder_name="downloads", blob=b"PK\x03\x04payload"):
    """An entry with an update whose build is sitting in the download folder already."""
    from mod_update_checker.report import UpdateEntry

    folder = Path(tmp_path) / "config" / "mod_update_checker" / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    name = "mod0-1.1.0.jar"
    (folder / name).write_bytes(blob)
    return UpdateEntry(
        mod_id="mod0",
        name="Mod 0",
        file_name="mod0.jar",
        local_version="1.0.0",
        latest_version="1.1.0",
        status="update_available",
        platform="modrinth",
        download_url="https://cdn.example/" + name,
        download_filename=name,
        download_sha1=hashlib.sha1(blob).hexdigest(),
        download_size=len(blob),
    )


def test_a_downloaded_build_is_no_longer_announced_as_an_update(tmp_path):
    """The behaviour the whole ledger exists for.

    Once the newer build is on disk, announcing it as an update again on every start is noise:
    the admin already did that step. It becomes "downloaded, waiting to be installed" instead,
    which is a different statement and the one they still have to act on.
    """
    import mod_update_checker as plugin

    server = _FakeServer(tmp_path)
    config = plugin.Config.get_default()
    entry = _downloaded_entry(tmp_path)

    plugin._sync_download_state(server, _report_with([entry]), config)

    assert entry.status == "awaiting_install"


def test_download_state_is_read_even_with_downloading_switched_off(tmp_path):
    """Off means "do not fetch", not "forget what was fetched".

    An admin who turns the option off after using it should still be told the file is waiting,
    rather than being told once more that an update exists.
    """
    import mod_update_checker as plugin

    server = _FakeServer(tmp_path)
    config = plugin.Config.get_default()
    assert config.download_updates is False
    entry = _downloaded_entry(tmp_path)

    plugin._sync_download_state(server, _report_with([entry]), config)

    assert entry.status == "awaiting_install"


def test_a_file_that_does_not_match_its_hash_stays_an_update(tmp_path):
    """Presence is not enough: a truncated or hand-replaced jar must not be called ready.

    Left as ``update_available`` so the download stage deals with it, which is the correct
    outcome — telling someone to install a file that is not what it claims to be is worse than
    telling them to fetch it again.
    """
    import mod_update_checker as plugin

    server = _FakeServer(tmp_path)
    config = plugin.Config.get_default()
    entry = _downloaded_entry(tmp_path)
    folder = Path(server.get_data_folder()) / "downloads"
    # Present, right name, wrong bytes.
    (folder / entry.download_filename).write_bytes(b"not the jar you are looking for")

    plugin._sync_download_state(server, _report_with([entry]), config)

    assert entry.status == "update_available"


def test_a_record_for_a_file_that_is_gone_is_dropped(tmp_path):
    """The admin installed it (or deleted it); the bookkeeping should follow, not go stale."""
    import mod_update_checker as plugin
    from mod_update_checker.downloads import DownloadLedger

    server = _FakeServer(tmp_path)
    config = plugin.Config.get_default()
    entry = _downloaded_entry(tmp_path)
    folder = Path(server.get_data_folder()) / "downloads"

    ledger = DownloadLedger(Path(server.get_data_folder()) / plugin.DOWNLOAD_LEDGER_FILE_NAME)
    ledger.record("mod0", entry.download_filename, entry.download_sha1, "1.1.0", "now")
    ledger.save()
    (folder / entry.download_filename).unlink()

    plugin._sync_download_state(server, _report_with([entry]), config)

    assert entry.status == "update_available"
    reloaded = DownloadLedger(Path(server.get_data_folder()) / plugin.DOWNLOAD_LEDGER_FILE_NAME)
    assert reloaded.get("mod0") is None, "a record outlived its file"


def test_nothing_is_fetched_when_the_feature_is_off(tmp_path, monkeypatch):
    """Off is the shipped default, so this is the path almost every server takes."""
    import mod_update_checker as plugin

    server = _FakeServer(tmp_path)
    config = plugin.Config.get_default()
    assert config.download_updates is False
    entry = _entry_with_update()

    def explode(*_args, **_kwargs):
        raise AssertionError("a download was attempted while the feature is off")

    monkeypatch.setattr(plugin, "Downloader", explode, raising=False)

    plugin._reconcile_downloads(server, _report_with([entry]), config)

    assert entry.status == "update_available"
    assert not (Path(server.get_data_folder()) / "downloads").exists()


def _entry_with_update():
    from mod_update_checker.report import UpdateEntry

    return UpdateEntry(
        mod_id="mod0", name="Mod 0", file_name="mod0.jar",
        local_version="1.0.0", latest_version="1.1.0",
        status="update_available", platform="modrinth",
        download_url="https://cdn.invalid/mod0-1.1.0.jar",
        download_filename="mod0-1.1.0.jar",
        download_sha1="a" * 40, download_size=100,
    )


def test_a_broken_download_stage_does_not_fail_the_check(tmp_path):
    """A successful check must not be reported as failed because a fetch went wrong.

    The report has already been written and announced at this point, so an exception escaping
    here would leave the admin with a "check failed" line for a check that actually worked.
    """
    import mod_update_checker as plugin

    server = _FakeServer(tmp_path)
    config = plugin.Config.get_default()
    config.download_updates = True
    # No data folder available is the cheapest way to make the stage throw from the inside.
    server.get_data_folder = lambda: (_ for _ in ()).throw(RuntimeError("boom"))

    plugin._reconcile_downloads(server, _report_with([_entry_with_update()]), config)

    joined = "\n".join(server.logger.messages)
    assert "WARN" in joined, joined
    assert "boom" in joined or "no-data-folder" in joined, joined


def test_the_report_records_what_was_downloaded(tmp_path):
    """The note is what makes the report file the place to look afterwards."""
    import mod_update_checker as plugin
    from mod_update_checker.downloads import DownloadOutcome

    server = _FakeServer(tmp_path)
    report = _report(updates=2)
    outcomes = [
        DownloadOutcome("mod0.jar", "mod0", "Mod 0", "downloaded",
                        path="/x/downloads/mod0-1.1.0.jar", bytes_written=2048),
        DownloadOutcome("mod1.jar", "mod1", "Mod 1", "already_present",
                        path="/x/downloads/mod1-1.1.0.jar"),
    ]

    plugin._log_download_outcomes(server, report, outcomes, Path("/x/downloads"))

    first = dict(report.entries[0].notes)
    assert "note.downloaded" in first
    assert first["note.downloaded"]["path"].endswith("mod0-1.1.0.jar")
    second = dict(report.entries[1].notes)
    assert "note.download_already_present" in second

    # And the summary line is logged, so the console shows the outcome without opening a file.
    assert any("下载结果" in message for message in server.logger.messages)


def test_config_round_trips_through_json():
    import mod_update_checker as plugin

    config = plugin.Config.get_default()
    payload = config.serialize()
    assert json.loads(json.dumps(payload)) == payload
    restored = plugin.Config.deserialize(payload)
    assert restored.command_permission_level == config.command_permission_level


def test_coloured_lines_are_only_used_where_colour_survives():
    """The colour paths differ, and mixing them up loses the colour silently.

    ``source.reply`` renders an RText (MCDR's ``StdoutReplier`` calls ``to_colored_text``, a
    player gets a chat component), while ``server.logger.info`` runs the message through
    ``str()`` — and ``RTextBase.__str__`` returns plain text. So a line built for a reply must
    still stringify back to exactly the original text, or logging it would be lossy.

    Empirically checked against the installed MCDR: ``str(RText('hello', RColor.yellow))`` is
    ``'hello'`` with no ANSI escape in it.
    """
    import mod_update_checker as plugin

    update_line = "Alpha  1.0.0 -> 1.1.0"
    coloured = plugin._coloured_line(update_line)

    assert str(coloured) == update_line
    assert "\x1b[" not in str(coloured)              # nothing depends on ANSI surviving
    assert "\x1b[" in coloured.to_colored_text()     # but the reply path does colour it

    header = plugin._coloured_line("Mod update check — server 26.3")
    assert str(header) == "Mod update check — server 26.3"
    # A header and an update line must not end up the same colour.
    assert coloured.to_colored_text() != header.to_colored_text()


def test_the_console_path_logs_plain_strings():
    """Guards the asymmetry: passing RText to the logger is a silent no-op for colour."""
    import ast

    tree = ast.parse(_entry_source())
    notify = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_notify"
    )
    logger_calls = [
        node for node in ast.walk(notify)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "info"
    ]
    assert logger_calls, "_notify no longer logs anything"
    for call in logger_calls:
        assert call.args, "logger.info called with no message"
        argument = call.args[0]
        assert not isinstance(argument, ast.Call) or not (
            isinstance(argument.func, ast.Name) and argument.func.id == "_coloured_line"
        ), "an RText line is being logged, where the colour would be dropped"
