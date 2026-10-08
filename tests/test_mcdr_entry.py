"""The MCDR entry module: how it hooks the lifecycle, and what it must not do twice.

Two of the defects this file guards against are invisible at runtime — no exception, no
warning, just wrong behaviour that only shows up as an extra request or a check that stops
firing — so they are pinned here rather than left to a review.
"""

import ast
import hashlib
import json
import sys
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
        """Behave like MCDR for a ``Serializable`` target — file and all.

        What the plugin says *about* its config file is a reaction to what MCDR does to the
        file on disk: it creates it when it is missing, and it fills in options the file does
        not have and writes the whole thing back. A stub that just returned
        ``target_class.get_default()`` would leave the file untouched, so every such message
        would be describing a state a real server cannot produce. This mirrors MCDR's
        ``load_config_simple``: load, deserialize with the missing-field callback, and save
        exactly when the file was unreadable or something was missing.
        """
        from mcdreforged.plugin.si._simple_config_handler import SimpleConfigHandler

        handler = SimpleConfigHandler(file_name, None, self.get_data_folder())
        incomplete = False

        def note_missing(*_args):
            nonlocal incomplete
            incomplete = True

        try:
            raw = handler.load(encoding="utf8")
        except OSError:
            config = target_class.get_default()
            handler.save(config.serialize(), encoding="utf8")
            return config

        config = target_class.deserialize(
            raw, missing_callback=note_missing, redundancy_callback=note_missing
        )
        if incomplete:
            handler.save(config.serialize(), encoding="utf8")
        return config

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

    monkeypatch.setattr(plugin, "_config", _config_with({"check.interval_hours": 1}),
                        raising=False)
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

    monkeypatch.setattr(plugin, "_config", _config_with(), raising=False)
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

    # The three at the root, deliberately not buried in a section.
    assert config.enabled is True
    assert config.language == "auto"
    assert config.command_permission_level == 3

    assert config.server.mods_directory == ""
    assert config.server.loader == "fabric"
    assert config.server.mc_version == "auto"

    assert config.check.on_server_start is True
    assert config.check.interval_hours == 0        # no surprise periodic load
    assert config.check.include_beta is False      # release builds only, by default
    assert config.check.include_alpha is False
    assert config.check.ignored_mods == []

    assert config.report.updates_only is True
    assert config.report.in_game is False          # never broadcast to players by default
    assert config.report.write_file is True

    assert config.sources.modrinth.enabled is True
    assert config.sources.modrinth.api_base == ""   # official endpoint

    # The auto-download feature writes files, so "off unless asked for" is part of the
    # contract rather than a preference.
    assert config.download.enabled is False
    assert config.download.folder_name == "downloads"
    assert config.download.max_size_mb == 128
    # Extra attempts after the first, matching the ``network.retries`` convention.
    assert config.download.retries == 3

    assert config.network.timeout_seconds == 20
    assert config.network.cache.enabled is True      # and therefore must work for a user
    assert config.network.requests_per_minute == 240  # under Modrinth's documented 300/min

    # A day, not half an hour: an admin logging in wants the answer, and the answer from
    # yesterday is still the answer unless something has been installed since.
    assert config.report.reuse_report_minutes == 1440

    # The two permission thresholds are deliberately separate settings rather than one shared
    # value: one decides who counts as an admin, the other who may receive a broadcast. A refactor
    # that merges them would still pass every other assertion here.
    assert config.report.admin_permission == 3
    assert config.report.in_game_permission == 3


def _write_config(server, payload):
    """Put ``payload`` where the plugin will look for its config file."""
    import mod_update_checker as plugin

    path = Path(server.get_data_folder()) / plugin.CONFIG_FILE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_a_config_file_still_using_the_flat_option_names_is_reported(tmp_path):
    """MCDR drops keys it does not recognise without saying so, so this has to be said here.

    An admin who already has a config file keeps it; every option in it is then read as absent,
    defaults are substituted, and the file is rewritten in the new shape. Without this warning
    the only symptom is settings that appear to have been forgotten for no reason — which looks
    like a bug in the plugin rather than a renamed option.
    """
    import mod_update_checker as plugin

    server = _FakeServer(tmp_path)
    _write_config(server, {"ignored_mods": ["pinned"], "download_updates": True})

    config = plugin._load_config(server)

    joined = "\n".join(server.logger.messages)
    assert "WARN" in joined, joined
    # The message has to name the move, or the admin cannot act on it.
    assert "ignored_mods -> check.ignored_mods" in joined, joined
    assert "download_updates -> download.enabled" in joined, joined
    # A warning, not a failure: the plugin still comes up.
    assert isinstance(config, plugin.Config)
    # And nothing was archived as if the file had been corrupt.
    assert "config_invalid" not in joined and ".broken." not in joined, joined


def test_a_config_file_in_the_new_shape_never_gets_the_legacy_warning(tmp_path):
    """The flat-name warning must not fire on a file that is already grouped.

    An incomplete file in the new shape is a different matter — it is filled in and reported
    as such, which the tests below cover. What must not happen here is the *legacy* warning,
    which would send the admin looking for options that never moved.
    """
    import mod_update_checker as plugin

    server = _FakeServer(tmp_path)
    _write_config(server, {"check": {"ignored_mods": ["pinned"]}, "download": {"enabled": True}})

    plugin._load_config(server)

    joined = "\n".join(server.logger.messages)
    assert "WARN" not in joined, joined


def test_a_config_file_that_does_not_exist_yet_is_reported_where_it_was_created(tmp_path):
    """A first install — or one where the file was deleted — says where the file went.

    "Where is the config file" is otherwise a question about conventions: this plugin's, or
    MCDR's, or a wiki's. One line, once, answers it.
    """
    import mod_update_checker as plugin

    server = _FakeServer(tmp_path)
    config = plugin._load_config(server)

    written = Path(server.get_data_folder()) / plugin.CONFIG_FILE_NAME
    assert written.is_file(), "loading must have created the file"
    assert isinstance(config, plugin.Config)

    joined = "\n".join(server.logger.messages)
    assert "已按默认值创建" in joined, joined
    assert str(written) in joined, joined


def test_an_incomplete_config_file_is_healed_and_the_added_options_are_named(tmp_path):
    """The report a user needed and did not get.

    The missing option was never the whole story: what could not be answered from the outside
    was whether the plugin had *noticed*, and whether the file had been brought up to date.
    Both halves are asserted — the file gains the options, and the console names them with the
    path — because either one alone can regress without the other.
    """
    import mod_update_checker as plugin

    server = _FakeServer(tmp_path)
    stale = plugin.Config.get_default().serialize()
    del stale["download"]["install_on_stop"]
    del stale["sources"]["manual_map"]
    path = _write_config(server, stale)

    plugin._load_config(server)

    rewritten = path.read_text(encoding="utf-8")
    assert "install_on_stop" in rewritten and "manual_map" in rewritten

    joined = "\n".join(server.logger.messages)
    assert "已按默认值补上" in joined, joined
    assert "download.install_on_stop" in joined, joined
    assert "sources.manual_map" in joined, joined
    assert str(path) in joined, joined


def test_a_config_file_that_was_not_updated_is_warned_about_with_its_path(tmp_path):
    """The message for the state nobody has reproduced yet: the file just will not change.

    It is reached when a load leaves the file missing options it should have gained — whatever
    the cause, from another program holding the file to the plugin that was updated not being
    the plugin that is running. When it appears, the path and the option names are the two
    facts needed to find out which.
    """
    import mod_update_checker as plugin

    server = _FakeServer(tmp_path)
    stale = plugin.Config.get_default().serialize()
    del stale["download"]["install_on_stop"]
    path = _write_config(server, stale)

    plugin._report_config_file_state(
        server,
        plugin.Config.get_default(),
        str(path),
        existed_before=True,
        leaves_before=plugin._leaf_paths(stale),
        legacy_rebuilt=False,
    )

    joined = "\n".join(server.logger.messages)
    assert "WARN" in joined, joined
    assert "download.install_on_stop" in joined, joined
    assert str(path) in joined, joined


def test_a_complete_config_file_is_not_talked_about(tmp_path):
    """The report is for the three states worth knowing about — not for every single load."""
    import mod_update_checker as plugin

    server = _FakeServer(tmp_path)
    _write_config(server, plugin.Config.get_default().serialize())

    plugin._load_config(server)

    assert server.logger.messages == [], server.logger.messages


def test_two_default_configs_do_not_share_their_nested_state():
    """Two ``Config`` objects must not share the contents of a section.

    MCDR builds a nested default with ``copy.copy``, which is shallow, so two instances would
    share the same list object — and appending to one would change the other, the class
    attribute, and every config built afterwards. The plugin rebuilds each section per
    instance to prevent that; this is the assertion that notices if a new section is added
    without going through the same path.
    """
    import mod_update_checker as plugin

    first = plugin.Config.get_default()
    second = plugin.Config.get_default()

    first.check.ignored_mods.append("sodium")
    assert second.check.ignored_mods == [], "the two configs share one ignored_mods list"
    assert plugin.CheckConfig.ignored_mods == [], "the class attribute itself was mutated"

    # Same question one level deeper, where the section-of-a-section lives.
    assert first.sources.modrinth is not second.sources.modrinth
    assert first.network.cache is not second.network.cache
    first.network.cache.ttl_hours = 1
    assert second.network.cache.ttl_hours == 24


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

    # Every option the tool sets, as a dotted path — the config file groups its options into
    # sections, so comparing sets of paths is the only way to ask this question of both shapes.
    #
    # Both runs are asked. One option (``download.install_on_stop``) is set by ``--with-install``
    # alone, because installing moves the fetched files out of the folder the default run's
    # download assertions inspect; without asking about both, the declared list and the set it
    # describes would never agree.
    from support import flatten_options, option_paths

    with_install = tool.plugin_config(_Upstream(), install=True)
    overridden = flatten_options(config)

    assert set(tool.CONFIG_OVERRIDES) == set(overridden) | set(flatten_options(with_install)), (
        "the matrix config drifted from its declared overrides://n"
        "  extra: {}\n  missing: {}".format(
            sorted((set(overridden) | set(flatten_options(with_install)))
                   - set(tool.CONFIG_OVERRIDES)),
            sorted(set(tool.CONFIG_OVERRIDES)
                   - set(overridden) - set(flatten_options(with_install))),
        )
    )

    # And the install run differs by exactly that one option, so the second run stays a second
    # run of the same thing rather than a differently configured one.
    assert set(flatten_options(with_install)) - set(overridden) == {"download.install_on_stop"}

    # The options whose defaults must be in force, i.e. absent from the override dict. These
    # are the ones that decide whether a code path runs at all.
    for key in (
        "network.cache.enabled",   # the crash that hid here
        "enabled",
        "report.write_file",
        "report.updates_only",
        "server.loader",
        "language",
        "server.mc_version",
        "server.mods_directory",
        "check.on_server_start",
        # Where files are written must stay at the shipped value: pointing the run at some
        # other folder would leave the download assertions looking at an empty directory and
        # quietly passing.
        "download.folder_name",
        "download.max_size_mb",
        # And so must the retry budget, so the matrix keeps exercising the number a user gets.
        "download.retries",
    ):
        assert key not in overridden, (
            "{} must stay at its shipped default in the end-to-end run, otherwise the "
            "behaviour a user gets is never exercised".format(key)
        )

    # ``report.in_game`` is the one exception, and a deliberate one: its default is off, and
    # off means the in-game notification path never executes. That path builds a message out of
    # mod names, so it is worth running. The override is declared above, and the run asserts
    # the payload is valid JSON addressed to the right player.
    assert overridden["report.in_game"] is True

    # ``download.enabled`` is off by default for the same reason, and switched on here because
    # it is the only feature that writes files — the last one to leave to unit tests alone.
    assert overridden["download.enabled"] is True

    # And every override has to name an option the plugin actually has, so a typo cannot
    # silently become a no-op. Walked off the class structure rather than a hand-kept list, so a
    # renamed section fails here instead of passing by accident.
    import mod_update_checker as plugin

    known = option_paths(plugin.Config)
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


def _report(updates=2, age_seconds=0.0, mods_directory=None):
    """A report with ``updates`` pending updates, produced ``age_seconds`` ago."""
    from datetime import datetime, timedelta, timezone

    from mod_update_checker.report import Report, UpdateEntry
    from mod_update_checker.serverinfo import ServerContext

    produced = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    report = Report(
        generated_at=produced.isoformat(timespec="seconds"),
        server=ServerContext(mc_version="26.3", mc_version_source="config", loader="fabric"),
        mods_directory=_MODS_DIRECTORY if mods_directory is None else mods_directory,
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


#: The mods directory the plugin resolves for the fake server. ``entry_env`` sets it to what
#: the fixture's server actually implies, because a stored report is only reused when it
#: describes the server in front of us — so a synthetic report naming some other directory is
#: correctly refused, and every test of the reuse window would quietly turn into a test of the
#: fresh-check path instead.
_MODS_DIRECTORY = "mods"


@pytest.fixture
def entry_env(tmp_path, monkeypatch):
    """The plugin module wired to a fake server, with a recording check function."""
    import mod_update_checker as plugin
    from mod_update_checker.scanner import resolve_mods_directory

    # Only two departures from the shipped defaults here; everything else this fixture used to
    # spell out was already the default, and saying so twice is how a stub drifts. The language
    # is pinned for the same reason ``_manual_env`` pins it: the translator is module state, so
    # a fixture that leaves it alone makes every assertion below depend on which test ran first
    # — a green suite one day and a red one the next, depending on the collection order.
    monkeypatch.setattr(
        plugin,
        "_config",
        _config_with({"report.in_game": True, "report.reuse_report_minutes": 30,
                      "language": "zh_cn"}),
        raising=False,
    )
    plugin._apply_language(None, plugin._config)
    monkeypatch.setattr(
        sys.modules[__name__],
        "_MODS_DIRECTORY",
        str(resolve_mods_directory(str(tmp_path), "")),
        raising=False,
    )
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
    monkeypatch.setattr(plugin._config.report, "reuse_report_minutes", 0)
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
    monkeypatch.setattr(plugin._config.report, "on_admin_join", False)

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
    monkeypatch.setattr(plugin._config.report, "in_game_permission", 4)

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
    config.download.folder_name = name

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
    assert config.download.enabled is False
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
    assert config.download.enabled is False
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
    config.download.enabled = True
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


def test_colour_survives_the_reply_path_and_only_there():
    """The colour paths differ, and mixing them up loses the colour silently.

    ``source.reply`` renders an RText (MCDR's ``StdoutReplier`` calls ``to_colored_text``, and
    a player receives a chat component), while ``server.logger.info`` runs the message through
    ``str()`` — and ``RTextBase.__str__`` returns plain text. So a screen is built from RText
    and the log is built from strings, and this is what keeps the two apart: the same screen
    text stringifies back to exactly itself, and is coloured only when it is rendered.

    Empirically checked against the installed MCDR: ``str(RText('hello', RColor.yellow))`` is
    ``'hello'`` with no ANSI escape in it.
    """
    import mod_update_checker as plugin
    from mod_update_checker.report import UpdateEntry

    title = plugin._title_line(None)
    assert "\x1b[" not in str(title)                 # nothing depends on ANSI surviving
    assert "\x1b[" in title.to_colored_text()        # but the reply path does colour it
    assert str(title).startswith("=") and "Mod Update Checker" in str(title)

    # A row is coloured by what it *means*, not by what it contains. The heuristic this
    # replaced read ``"->" in line``, which coloured by coincidence of text and made the same
    # mod one colour in the listing and another in the summary.
    needs_work = UpdateEntry(mod_id="a", name="A -> B", file_name="a.jar",
                             status=plugin.STATUS_UP_TO_DATE)
    done = UpdateEntry(mod_id="b", name="B", file_name="b.jar",
                       status=plugin.STATUS_UPDATE_AVAILABLE)
    assert plugin._row_colour(needs_work) != plugin._row_colour(done)
    assert plugin._row_colour(done) == plugin.RColor.yellow


def test_the_console_path_logs_plain_strings():
    """Guards the asymmetry: passing RText to the logger is a silent no-op for colour.

    Also for the buttons: a click event logged to the console is a click event nobody can use,
    and the text it decorates arrives as its bare label.
    """
    import ast

    rtext_builders = {"_title_line", "_entry_row", "_field", "RText", "RTextList"}
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
            isinstance(argument.func, ast.Name) and argument.func.id in rtext_builders
        ), "an RText line is being logged, where the colour and the buttons would be dropped"


# --------------------------------------------------------------------------------------
# The help and status screens
# --------------------------------------------------------------------------------------


def _config_with(overrides=None):
    """The shipped config with specific options replaced. Keys are dotted paths.

    Built from the real ``Config`` rather than hand-written. A stub that mirrors four fields of
    the config has to be updated whenever the config changes, and the time it is not updated is
    the time the test quietly stops exercising the real object — which is the failure mode this
    whole suite exists to catch. ``check.interval_hours`` rather than ``check_interval_hours``,
    because the config file groups its options into sections and the tests should speak the
    same language as the file.
    """
    import mod_update_checker as plugin

    config = plugin.Config.get_default()
    for path, value in (overrides or {}).items():
        parts = path.split(".")
        target = config
        for name in parts[:-1]:
            target = getattr(target, name)
        setattr(target, parts[-1], value)
    return config


class _ReplyRecorder:
    """A command source that keeps whatever the command replied with."""

    def __init__(self):
        self.replies = []

    def reply(self, text, **_kwargs):
        self.replies.append(text)


def _render_help(prefix, language="zh_cn"):
    import mod_update_checker as plugin

    previous = plugin._config
    plugin._apply_language(None, _config_with({"language": language}))
    try:
        source = _ReplyRecorder()
        plugin._show_help(source, prefix)
        return source.replies
    finally:
        plugin._config = previous


def test_help_is_one_rich_message_rather_than_a_line_per_reply():
    """One ``RTextList``, not a stack of separate replies.

    The screen is a single block: the title bar, the blank lines and the aligned columns only
    make sense if they are composed together. Replying line by line also loses the colours on
    any client that renders each reply separately.
    """
    from mcdreforged.api.rtext import RTextList

    replies = _render_help("!!muc")

    assert len(replies) == 1, replies
    assert isinstance(replies[0], RTextList)


def test_help_names_the_spelling_that_was_typed():
    """``!!muc help`` must not list ``!!modupdate ...``.

    Both aliases are registered and either may be the one an admin remembers; showing the
    other one is how someone concludes the command they typed does not exist.
    """
    for prefix, other in (("!!muc", "!!modupdate"), ("!!modupdate", "!!muc")):
        body = str(_render_help(prefix)[0])

        assert prefix + " <" in body or prefix + " check" in body
        assert "{} check".format(prefix) in body
        assert "{} check".format(other) not in body
        # The other spelling is still mentioned once, in the usage line.
        assert other in body


def test_help_lists_every_registered_subcommand():
    """A subcommand missing from the help is a subcommand nobody will use.

    Read off the command tree rather than a hand-kept list, so adding a subcommand without
    adding a help row fails here instead of shipping silently. Uses the public accessors
    (``literals`` / ``get_children``) so a reshuffle inside MCDR does not make this lie.
    """
    import mod_update_checker as plugin

    node = plugin._command_tree("!!muc")
    registered = set()
    for child in node.get_children():
        registered.update(child.literals)

    assert registered, "the command tree reported no subcommands"
    assert "help" in registered, "the help subcommand itself is missing"

    body = str(_render_help("!!muc")[0])
    for name in sorted(registered):
        assert "!!muc {}".format(name) in body, "{} is not in the help".format(name)


def test_help_columns_line_up_for_both_aliases():
    """The description column starts at the same place on every row.

    The padding is computed from the longest command, so the row that *is* the longest is the
    one this fails on when the separator forgets its leading space.
    """
    for prefix in ("!!muc", "!!modupdate"):
        body = str(_render_help(prefix)[0])
        columns = [line.index("-- ") for line in body.splitlines() if "-- " in line]

        assert columns, body
        assert len(set(columns)) == 1, (prefix, columns)


def test_every_help_row_is_clickable_and_describes_its_command():
    """Colour and click targets are the whole point of the rich help page.

    Asserted on ``to_json_object()`` rather than on private attributes: that is the form the
    client actually receives, so it proves the colour and the click survive the trip, and it
    is a documented interface instead of an internal one.
    """
    segments = list(_segments(_render_help("!!muc")[0]))

    commands = [
        item for item in segments
        if item.get("text", "").startswith("!!muc") and item.get("color") == "aqua"
    ]
    assert commands, "no clickable command segments found"

    for item in commands:
        click = item.get("clickEvent")
        assert click is not None, item
        # Clicking runs or types the command itself, never its description.
        assert click.get("value", "").startswith("!!muc"), item
        assert click.get("action") in ("run_command", "suggest_command"), item

    # The descriptions are present and set apart, so the screen reads as two columns.
    descriptions = [item for item in segments if item.get("color") == "white"]
    assert descriptions, "the help rows have no descriptions"


def test_only_the_row_that_needs_an_argument_suggests_instead_of_running():
    """Help rows run, except where the command is useless without an argument.

    ``list`` runs: it is a read-only listing and the useful thing to see. ``info`` does not —
    it needs a mod, so clicking it fills the input box rather than firing an error, which is
    what makes the number in the listing worth copying. ``download`` and ``install`` follow the
    same rule for the same reason; ``confirm`` takes nothing, so it runs.
    """
    segments = list(_segments(_render_help("!!muc")[0]))
    rows = {
        item["text"]: item
        for item in segments
        if item.get("text", "").startswith("!!muc ") and "clickEvent" in item
    }

    for command in ("!!muc list", "!!muc check", "!!muc status", "!!muc reload",
                    "!!muc confirm"):
        assert rows[command]["clickEvent"]["action"] == "run_command", command

    for command in ("!!muc info", "!!muc download", "!!muc install"):
        assert rows[command]["clickEvent"]["action"] == "suggest_command", command
        # A trailing space, so the number is typed straight after the command.
        assert rows[command]["clickEvent"]["value"].endswith(" "), command

    # Every command the tree registers is on the page, so a new one cannot be added and left
    # undiscoverable — the failure this list would otherwise not notice at all. (The bare
    # ``!!muc`` row is the summary, and is excluded by the ``"!!muc "`` filter above.)
    assert set(rows) == {
        "!!muc check", "!!muc list", "!!muc info", "!!muc download", "!!muc install",
        "!!muc confirm", "!!muc status", "!!muc reload", "!!muc help",
    }


def _segments(node):
    """Every leaf of an ``RTextList``, as the JSON objects the client is sent."""
    children = getattr(node, "children", None)
    if children is None:
        yield node.to_json_object()
        return
    for child in children:
        yield from _segments(child)


def test_the_status_screen_uses_the_same_title_bar_as_the_help(tmp_path):
    """The two screens are meant to look like one plugin's.

    Checked by shape rather than by exact text: the title bar is gold ``=`` bars around the
    plugin name, and it opens both screens.
    """
    import mod_update_checker as plugin

    class _Scan:
        directory = "server/mods"
        mods = []
        disabled = []

    config = _config_with({"language": "zh_cn"})
    previous_config, previous_scan, previous_server = plugin._config, plugin._scan_current, plugin._server
    try:
        plugin._config = config
        plugin._apply_language(None, config)
        plugin._scan_current = lambda *_a, **_k: (
            _Scan(),
            __import__(
                "mod_update_checker.serverinfo", fromlist=["ServerContext"]
            ).ServerContext(mc_version="26.3", mc_version_source="config", loader="fabric"),
        )
        plugin._server = _FakeServer(tmp_path)

        source = _ReplyRecorder()
        plugin._show_status(source)
    finally:
        plugin._config, plugin._scan_current, plugin._server = (
            previous_config, previous_scan, previous_server,
        )

    assert len(source.replies) == 1
    bars = [
        item for item in _segments(source.replies[0])
        if item.get("text", "").startswith("=") and item.get("color") == "gold"
    ]
    assert bars, "the status screen has no title bar"
    assert bars[0]["text"].strip("=") == "", bars[0]


def _one_screen_setup(tmp_path, monkeypatch, entries):
    """The plugin wired to a fake server with a report, for driving one screen at a time."""
    import mod_update_checker as plugin
    from mod_update_checker.serverinfo import ServerContext

    class _Scan:
        directory = "server/mods"
        mods = []
        disabled = []

    server = _FakeServer(tmp_path)
    monkeypatch.setattr(plugin, "_server", server, raising=False)
    monkeypatch.setattr(plugin, "_last_report", _report_with(entries), raising=False)
    monkeypatch.setattr(plugin, "_config", _config_with({"language": "zh_cn"}), raising=False)
    monkeypatch.setattr(
        plugin, "_scan_current",
        lambda *_a, **_k: (
            _Scan(),
            ServerContext(mc_version="26.3", mc_version_source="config", loader="fabric"),
        ),
        raising=False,
    )
    plugin._apply_language(server, plugin._config)
    return plugin, server


def _entry_for_screens():
    """One mod with everything a screen can show: a version change, a page and an action."""
    from mod_update_checker.report import UpdateEntry

    entry = UpdateEntry(
        mod_id="alpha", name="Alpha", file_name="alpha.jar",
        local_version="1.0.0", latest_version="1.1.0", status="update_available",
    )
    entry.project_url = "https://modrinth.com/mod/alpha"
    entry.download_url = "https://cdn.example/alpha-1.1.0.jar"
    entry.download_sha1 = "a" * 40
    return entry


def test_every_screen_opens_with_the_same_title_bar(tmp_path, monkeypatch):
    """One bar, five screens.

    They used to disagree: ``help`` and ``status`` drew a gold bar, the listing and the summary
    opened with the plugin's badge line, and a mod's detail had no header at all. A reader who
    has to recognise the layout afresh on every command is reading the layout instead of the
    answer — and this is asserted on the segments that actually go to the client, so a screen
    that quietly drops the bar fails rather than looking merely inconsistent.
    """
    entry = _entry_for_screens()
    plugin, _server = _one_screen_setup(tmp_path, monkeypatch, [entry])
    report = plugin._last_report

    screens = {
        "summary": lambda source: plugin._reply_summary(source, report),
        "list": lambda source: plugin._reply_index(source, report),
        "info": lambda source: plugin._reply_detail(source, entry),
        "status": plugin._show_status,
        "help": plugin._show_help,
    }

    bars = {}
    for name, call in screens.items():
        source = _ReplyRecorder()
        call(source)
        assert source.replies, "{} replied with nothing".format(name)
        gold = [item["text"] for item in _segments(source.replies[0])
                if item.get("color") == "gold"]
        assert gold, "{} does not open with the title bar".format(name)
        bars[name] = gold

    assert len({tuple(value) for value in bars.values()}) == 1, bars


def test_the_chat_summary_offers_a_button_where_the_log_offers_a_url(tmp_path, monkeypatch):
    """The url belongs in the log, the button in the chat — and both read the same sections.

    A raw url in a chat line wraps onto a second row on any real mod, and cannot be clicked
    from inside the game at all. The log keeps it: a log line cannot be clicked either, so
    there the url is the only way through, and it can at least be copied.
    """
    entry = _entry_for_screens()
    plugin, _server = _one_screen_setup(tmp_path, monkeypatch, [entry])
    report = plugin._last_report

    source = _ReplyRecorder()
    plugin._reply_summary(source, report)
    segments = [item for reply in source.replies for item in _segments(reply)]

    body = "\n".join(str(reply) for reply in source.replies)
    assert entry.project_url not in body
    assert "cdn.example" not in body

    clicks = [item.get("clickEvent", {}).get("value") for item in segments]
    assert "!!modupdate info 1" in clicks, clicks

    # And the log form really does still carry it, so the two are not the same screen twice.
    from mod_update_checker.i18n import make_translator
    from mod_update_checker.report import render_summary

    assert entry.project_url in "\n".join(render_summary(report, make_translator("zh_cn")))


# --------------------------------------------------------------------------------------
# !!muc download / install / confirm
#
# The three commands exist so a single mod can be fetched and installed without switching the
# automatic halves on. Both end in something awkward to undo, so both are staged and carried
# out by ``confirm`` — which means the interesting invariants are about what must NOT happen:
# nothing is fetched on the first command, nothing is installed that the admin did not name,
# and a plan cannot be carried out after the numbers it referred to have moved.
# --------------------------------------------------------------------------------------


class _PlayerSource:
    """A named player command source that records the replies."""

    def __init__(self, player="Admin"):
        self.player = player
        self.is_player = True
        self.replies = []

    def reply(self, text, **_kwargs):
        self.replies.append(str(text))

    @property
    def body(self):
        return "\n".join(self.replies)


def _manual_env(tmp_path, monkeypatch, status="update_available", player="Admin"):
    """The plugin wired to a fake server, with one entry and a matching ledger record.

    The ledger record is written because both ``download`` and ``install`` act on records, not
    on the report: a build with no record is a build the install stage will refuse, and a test
    that skipped this step would pass while the real flow refused.

    The language is set explicitly rather than inherited. ``_translator`` is module state that
    survives between tests, so a fixture that leaves it alone makes every assertion below depend
    on which test ran first — which is a green suite one day and a red one the next.
    """
    import mod_update_checker as plugin
    from mod_update_checker.downloads import DownloadLedger
    from mod_update_checker.report import UpdateEntry

    monkeypatch.setattr(plugin, "_config", _config_with({"language": "zh_cn"}), raising=False)
    plugin._apply_language(None, plugin._config)
    plugin._stop_event.clear()
    plugin._clear_pending()

    server = _FakeServer(tmp_path, levels={"Admin": 4, "Other": 4})
    entry = UpdateEntry(
        mod_id="sodium",
        name="Sodium",
        file_name="sodium.jar",
        local_version="1.0.0",
        latest_version="1.1.0",
        status=status,
        platform="modrinth",
        project_url="https://modrinth.com/mod/sodium",
        download_url="https://cdn.example/sodium-1.1.0.jar",
        download_filename="sodium-fabric-1.1.0.jar",
        download_sha1="a" * 40,
        download_size=2048,
    )
    monkeypatch.setattr(plugin, "_last_report", _report_with([entry]), raising=False)
    monkeypatch.setattr(plugin, "_server", server, raising=False)

    ledger = DownloadLedger(
        Path(server.get_data_folder()) / plugin.DOWNLOAD_LEDGER_FILE_NAME, logger=server.logger
    )
    ledger.record("sodium", "sodium-fabric-1.1.0.jar", "a" * 40, "1.1.0", "2026-01-01T00:00:00+00:00",
                  installed_file="sodium.jar", name="Sodium")
    ledger.save()

    return plugin, server, _PlayerSource(player), entry, ledger


def test_download_stages_a_plan_and_fetches_nothing(tmp_path, monkeypatch):
    """The first command must not spend bandwidth: that is what ``confirm`` is for."""
    plugin, _server, source, entry, _ledger = _manual_env(tmp_path, monkeypatch)
    fetched = []
    monkeypatch.setattr(
        plugin, "_perform_manual_download", lambda item: fetched.append(item), raising=False
    )

    plugin._manual_download(source, "1", "!!muc")

    assert fetched == [], "the download ran before it was confirmed"
    pending = plugin._pending_action
    assert pending is not None and pending["kind"] == "download"
    assert "!!muc confirm" in source.body
    assert "sodium-fabric-1.1.0.jar" in source.body, source.body
    assert "2 KB" in source.body, "the plan should say how big the file is"


def test_confirm_is_what_runs_the_download(tmp_path, monkeypatch):
    """And the outcome is reported back with the next command to type."""
    from mod_update_checker.downloads import STATUS_DOWNLOADED

    plugin_mod, server, source, entry, _ledger = _manual_env(tmp_path, monkeypatch)

    class _Outcome:
        status = STATUS_DOWNLOADED
        path = "config/mod_update_checker/downloads/sodium-fabric-1.1.0.jar"
        file_name = "sodium.jar"
        detail = ""

    plugin_mod._manual_download(source, "1", "!!muc")
    monkeypatch.setattr(plugin_mod, "_perform_manual_download",
                        lambda item: _Outcome(), raising=False)
    plugin_mod._manual_confirm(source, "!!muc")

    # The fetch runs on its own thread; wait for it rather than sleeping a fixed time.
    for _ in range(200):
        if "已下载到" in source.body:
            break
        time.sleep(0.02)

    assert "!!muc install 1" in source.body, source.body
    assert plugin_mod._pending_action is None, "the plan was carried out, so it is spent"


def test_a_failed_download_says_why_and_stays_an_update(tmp_path, monkeypatch):
    """A failure is reported in words, not as the internal code it travels as."""
    from mod_update_checker.downloads import STATUS_FAILED

    plugin_mod, _server, source, entry, _ledger = _manual_env(tmp_path, monkeypatch)

    class _Outcome:
        status = STATUS_FAILED
        path = ""
        file_name = "sodium.jar"
        detail = "no-hash-to-verify"

    plugin_mod._manual_download(source, "1", "!!muc")
    monkeypatch.setattr(plugin_mod, "_perform_manual_download",
                        lambda item: _Outcome(), raising=False)
    plugin_mod._manual_confirm(source, "!!muc")

    for _ in range(200):
        if "失败" in source.body:
            break
        time.sleep(0.02)

    assert "上游没有提供哈希" in source.body, source.body
    assert "no-hash-to-verify" not in source.body, "a raw code reached the player"


def test_an_already_downloaded_mod_points_at_install(tmp_path, monkeypatch):
    """The one case that must not say "cannot download" — there is nothing left to fetch."""
    plugin, _server, source, _entry, _ledger = _manual_env(
        tmp_path, monkeypatch, status="awaiting_install"
    )

    plugin._manual_download(source, "1", "!!muc")

    assert plugin._pending_action is None
    assert "!!muc install 1" in source.body, source.body


def test_a_mod_with_nothing_to_fetch_says_which_case_it_is(tmp_path, monkeypatch):
    """Four situations, four sentences — an admin needs to know which one they are in."""
    for status, expected in (
        ("up_to_date", "已是最新"),
        ("no_compatible_build", "没有适配本服务端的构建"),
        ("unresolved", "无法定位到 Modrinth"),
    ):
        plugin, _server, source, _entry, _ledger = _manual_env(
            tmp_path, monkeypatch, status=status
        )
        plugin._manual_download(source, "1", "!!muc")

        assert plugin._pending_action is None, status
        assert expected in source.body, (status, source.body)


def test_install_refuses_a_mod_that_has_not_been_downloaded(tmp_path, monkeypatch):
    """It names the command that fixes it, with the number already filled in."""
    plugin, _server, source, _entry, _ledger = _manual_env(tmp_path, monkeypatch)

    plugin._manual_install(source, "1", "!!muc")

    assert plugin._pending_action is None
    assert "!!muc download 1" in source.body, source.body


def test_the_number_the_install_hint_suggests_is_the_listing_number(tmp_path, monkeypatch):
    """``!!muc install <name>`` may answer "type !!muc download 3" — the ``3`` has to be right.

    Asserted with the mod **not** first in the listing, because a one-entry report would be
    satisfied by any numbering at all. The handle is a name on purpose: that is the form which
    has to go through the lookup and then still produce the listing's number.
    """
    import mod_update_checker as plugin
    from mod_update_checker.report import UpdateEntry

    monkeypatch.setattr(plugin, "_config", _config_with({"language": "zh_cn"}), raising=False)
    plugin._apply_language(None, plugin._config)
    plugin._stop_event.clear()
    plugin._clear_pending()

    server = _FakeServer(tmp_path, levels={"Admin": 4})
    fetched = UpdateEntry(mod_id="alpha", name="Alpha", file_name="alpha.jar",
                          local_version="1.0.0", latest_version="1.1.0",
                          status="awaiting_install")
    waiting = UpdateEntry(mod_id="zeta", name="Zeta", file_name="zeta.jar",
                          local_version="1.0.0", latest_version="1.1.0",
                          status="update_available")
    # Both are actionable, so they share a rank and are ordered by name: Alpha is 1, Zeta is 2.
    monkeypatch.setattr(plugin, "_last_report", _report_with([fetched, waiting]), raising=False)
    monkeypatch.setattr(plugin, "_server", server, raising=False)

    source = _PlayerSource("Admin")
    plugin._manual_install(source, "Zeta", "!!muc")

    assert "!!muc download 2" in source.body, source.body


def test_install_stages_the_swap_and_confirm_authorises_only_that_mod(tmp_path, monkeypatch):
    """The whole point of the per-record flag: five downloads, one named, one installed."""
    from mod_update_checker.downloads import DownloadLedger

    plugin_mod, server, source, _entry, ledger = _manual_env(
        tmp_path, monkeypatch, status="awaiting_install"
    )
    # A second mod, downloaded and NOT authorised.
    ledger.record("other", "other-2.0.jar", "b" * 40, "2.0", "2026-01-01T00:00:00+00:00",
                  installed_file="other.jar", name="Other")
    ledger.save()

    plugin_mod._manual_install(source, "1", "!!muc")
    assert plugin_mod._pending_action["kind"] == "install"
    assert "sodium.jar" in source.body and "sodium-fabric-1.1.0.jar" in source.body, source.body
    assert "!!muc confirm" in source.body

    plugin_mod._manual_confirm(source, "!!muc")

    assert "已授权" in source.body, source.body
    # Read back from disk: the command writes its own ledger, because by the time an admin
    # confirms, the plugin may have been reloaded and the record is what is on disk.
    written = DownloadLedger(
        Path(server.get_data_folder()) / plugin_mod.DOWNLOAD_LEDGER_FILE_NAME
    )
    assert written.approved_keys() == ["sodium"], "an unauthorised record was swept up"


def test_confirm_with_nothing_staged_says_so(tmp_path, monkeypatch):

    plugin_mod, _server, source, _entry, _ledger = _manual_env(tmp_path, monkeypatch)
    plugin_mod._clear_pending()
    plugin_mod._manual_confirm(source, "!!muc")

    assert "没有待确认的操作" in source.body, source.body


def test_only_the_one_who_staged_it_can_confirm(tmp_path, monkeypatch):
    """A confirmation is a decision, and only its author can make it."""
    plugin, _server, source, _entry, _ledger = _manual_env(tmp_path, monkeypatch, player="Admin")
    other = _PlayerSource("Other")

    plugin._manual_download(source, "1", "!!muc")
    plugin._manual_confirm(other, "!!muc")

    assert "只有本人可以确认" in other.body, other.body
    assert plugin._pending_action is not None, "somebody else's confirm consumed the plan"


def test_a_lapsed_confirmation_is_refused(tmp_path, monkeypatch):
    """A ``!!muc confirm`` typed much later must not act on a plan nobody remembers."""
    plugin, _server, source, _entry, _ledger = _manual_env(tmp_path, monkeypatch)

    plugin._manual_download(source, "1", "!!muc")
    plugin._pending_action["deadline"] = time.monotonic() - 1

    plugin._manual_confirm(source, "!!muc")

    assert "作废" in source.body, source.body
    assert plugin._pending_action is None


def test_a_confirmation_is_dropped_when_the_report_moved_underneath_it(tmp_path, monkeypatch):
    """Numbers are a mapping, and a new report is a new mapping.

    Re-resolving ``1`` against a report that has been replaced is how the wrong mod gets
    installed — the numbers still exist, they just mean something else now.
    """
    from mod_update_checker.report import UpdateEntry

    plugin_mod, server, source, _entry, _ledger = _manual_env(
        tmp_path, monkeypatch, status="awaiting_install"
    )
    plugin_mod._manual_install(source, "1", "!!muc")

    # Same number, different mod: exactly what a re-run produces.
    replacement = UpdateEntry(
        mod_id="lithium", name="Lithium", file_name="lithium.jar",
        local_version="0.1", latest_version="0.2", status="awaiting_install",
    )
    plugin_mod._last_report = _report_with([replacement])

    plugin_mod._manual_confirm(source, "!!muc")

    assert "作废" in source.body, source.body
    assert plugin_mod._pending_action is None


def test_the_console_counts_as_its_own_requester(tmp_path, monkeypatch):
    """A player cannot confirm a plan the console staged, and vice versa."""

    plugin_mod, server, _source, _entry, _ledger = _manual_env(tmp_path, monkeypatch)
    console = _ReplyRecorder()
    console.player = ""
    console.is_player = False

    plugin_mod._manual_install(console, "1", "!!muc")
    # The entry is not downloaded, so nothing was staged — stage it directly to test ``confirm``.
    plugin_mod._pending_action = {
        "kind": "install", "number": 1, "file_name": "sodium.jar", "mod_id": "sodium",
        "requester": plugin_mod._requester(console),
        "deadline": time.monotonic() + 60,
    }

    player = _PlayerSource("Admin")
    plugin_mod._manual_confirm(player, "!!muc")

    assert "只有本人可以确认" in player.body, player.body


# --------------------------------------------------------------------------------------
# The bulk forms: ``download all`` / ``install all``
#
# A batch is a different promise from a single mod: instead of "this one", the admin approves
# a *set*. So the tests below are about the set surviving whatever happens between the two
# commands, about a batch never widening into the ledger, and about ``all`` being a word with
# one meaning rather than sometimes-a-mod.
# --------------------------------------------------------------------------------------


def _bulk_entry(mod_id, name, file_name, status="update_available", latest="1.1.0",
                downloadable=True):
    """One report entry, with all three spellings deliberately different.

    ``id``, ``file name`` and ``display name`` are unrelated on purpose: a test whose entry is
    named after its own id can pass through the id path whatever happened to the name path —
    this suite has made exactly that mistake once already (v1.2.0).
    """
    from mod_update_checker.report import UpdateEntry

    entry = UpdateEntry(
        mod_id=mod_id, name=name, file_name=file_name,
        local_version="1.0.0", latest_version=latest, status=status,
    )
    if downloadable:
        entry.download_url = "https://cdn.example/" + file_name
        entry.download_filename = "{}-{}.jar".format(mod_id, latest)
        entry.download_sha1 = "b" * 40
        entry.download_size = 2048
    return entry


def _bulk_env(tmp_path, monkeypatch, entries):
    """The plugin wired to a fake server whose report holds exactly ``entries``."""
    import mod_update_checker as plugin

    monkeypatch.setattr(plugin, "_config", _config_with({"language": "zh_cn"}), raising=False)
    plugin._apply_language(None, plugin._config)
    plugin._stop_event.clear()
    plugin._clear_pending()

    server = _FakeServer(tmp_path, levels={"Admin": 4})
    monkeypatch.setattr(plugin, "_last_report", _report_with(entries), raising=False)
    monkeypatch.setattr(plugin, "_server", server, raising=False)
    return plugin, server, _PlayerSource("Admin")


def test_download_all_stages_one_plan_for_every_fetchable_mod(tmp_path, monkeypatch):
    """One confirmation for the lot, and the plan names what it is about.

    The un-fetchable update is counted out loud rather than dropped: a plan that quietly covers
    fewer mods than the report lists is how an admin comes to believe a mod was fetched.
    """
    fetchable = _bulk_entry("sodium", "Sodium", "sodium.jar")
    second = _bulk_entry("lithium", "Lithium", "lithium.jar")
    withdrawn = _bulk_entry("withdrawn", "Withdrawn", "withdrawn.jar", downloadable=False)
    settled = _bulk_entry("iris", "Iris", "iris.jar", status="up_to_date")

    plugin, _server, source = _bulk_env(
        tmp_path, monkeypatch, [fetchable, second, withdrawn, settled]
    )
    monkeypatch.setattr(
        plugin, "_perform_manual_downloads",
        lambda items: pytest.fail("the batch ran before it was confirmed"), raising=False,
    )

    plugin._manual_download(source, "all", "!!muc")

    assert plugin._pending_action["kind"] == "download_all"
    assert plugin._pending_action["files"] == ["lithium.jar", "sodium.jar"]
    assert "Sodium" in source.body and "Lithium" in source.body
    assert "下载 2 个" in source.body, source.body
    assert "1 个更新没有可下载的文件" in source.body, source.body
    assert "!!muc confirm" in source.body


def test_download_all_with_nothing_to_fetch_says_which_case_it_is(tmp_path, monkeypatch):
    """Two empty answers, because they are two different situations.

    "Nothing is pending" is a server that is up to date. "Nothing *fetchable*" is a platform
    that published a version whose files were withdrawn — and an admin told the first while
    the second is true stops looking for a problem that does not exist.
    """
    settled = _bulk_entry("iris", "Iris", "iris.jar", status="up_to_date")
    plugin, _server, source = _bulk_env(tmp_path, monkeypatch, [settled])

    plugin._manual_download(source, "all", "!!muc")

    assert "没有待下载的更新" in source.body, source.body
    assert plugin._pending_action is None

    plugin._last_report = _report_with(
        [_bulk_entry("withdrawn", "Withdrawn", "withdrawn.jar", downloadable=False)]
    )
    source.replies.clear()

    plugin._manual_download(source, "all", "!!muc")

    assert "都没有提供可下载的文件" in source.body, source.body
    assert plugin._pending_action is None


def test_confirm_runs_the_whole_batch_and_reports_it(tmp_path, monkeypatch):
    """The outcome is a summary rather than a line per mod, and it names the next command."""
    from mod_update_checker.downloads import STATUS_DOWNLOADED, STATUS_FAILED

    first = _bulk_entry("sodium", "Sodium", "sodium.jar")
    second = _bulk_entry("lithium", "Lithium", "lithium.jar")
    plugin, _server, source = _bulk_env(tmp_path, monkeypatch, [first, second])

    class _Outcome:
        def __init__(self, file_name, name, status):
            self.file_name, self.name, self.status = file_name, name, status
            self.path = "config/mod_update_checker/downloads/" + file_name
            self.detail = ""
            self.bytes_written = 0

    def stand_in(entries):
        # Every candidate arrives at the downloader — a batch that quietly dropped one is the
        # failure this test exists to catch, and it would be invisible from the summary alone.
        assert {entry.file_name for entry in entries} == {"lithium.jar", "sodium.jar"}
        return [
            _Outcome("sodium.jar", "Sodium", STATUS_DOWNLOADED),
            _Outcome("lithium.jar", "Lithium", STATUS_FAILED),
        ]

    monkeypatch.setattr(plugin, "_perform_manual_downloads", stand_in, raising=False)

    plugin._manual_download(source, "all", "!!muc")
    plugin._manual_confirm(source, "!!muc")

    # The fetch runs on its own thread; wait for it rather than sleeping a fixed time.
    for _ in range(200):
        if "批量下载结束" in source.body:
            break
        time.sleep(0.02)

    assert "1 个已下载" in source.body, source.body
    assert "失败的是：Lithium" in source.body, source.body
    assert "!!muc install all" in source.body, source.body
    assert plugin._pending_action is None, "the plan was carried out, so it is spent"


def test_all_is_the_bulk_word_even_when_a_mod_answers_to_it(tmp_path, monkeypatch):
    """``all`` is reserved, and being reserved is the point.

    It has to mean the list even on a server that ships a mod whose name, id and file stem are
    all spelled that way — a bulk command that silently narrowed to one mod would be the worst
    kind of surprise. Such a mod stays reachable by the number the listing shows it under.
    """
    tricky = _bulk_entry("allmod", "All", "all.jar")
    other = _bulk_entry("sodium", "Sodium", "sodium.jar")
    plugin, _server, source = _bulk_env(tmp_path, monkeypatch, [tricky, other])

    plugin._manual_download(source, "all", "!!muc")

    assert plugin._pending_action["kind"] == "download_all"
    assert plugin._pending_action["files"] == ["all.jar", "sodium.jar"]

    # And the same word on ``install``, on a report where it is the only candidate — which is
    # the case where a name lookup would also have "worked" and meant something else.
    plugin._clear_pending()
    plugin._last_report = _report_with(
        [_bulk_entry("allmod", "All", "all.jar", status="awaiting_install")]
    )
    plugin._manual_install(source, "all", "!!muc")

    assert plugin._pending_action["kind"] == "install_all"


def test_a_batch_confirmation_is_dropped_when_the_set_of_mods_changed(tmp_path, monkeypatch):
    """Same rule as the single form, one level up — because "these mods" is what was approved.

    Running whatever overlap is left would report success over a batch that partly did not
    happen, and the admin would have no way to know which half.
    """
    first = _bulk_entry("sodium", "Sodium", "sodium.jar")
    second = _bulk_entry("lithium", "Lithium", "lithium.jar")
    plugin, _server, source = _bulk_env(tmp_path, monkeypatch, [first, second])
    monkeypatch.setattr(
        plugin, "_perform_manual_downloads",
        lambda items: pytest.fail("a stale batch was carried out"), raising=False,
    )

    plugin._manual_download(source, "all", "!!muc")
    # A re-run finished underneath: one of the two is no longer waiting to be fetched.
    plugin._last_report = _report_with([first])

    plugin._manual_confirm(source, "!!muc")

    assert "作废" in source.body, source.body
    assert "!!muc download all" in source.body, source.body
    assert plugin._pending_action is None


def test_install_all_authorises_every_downloaded_build(tmp_path, monkeypatch):
    """One command for the lot, and one ledger record at a time underneath.

    The records are the point: the ledger is also the work list the next stop reads, so the
    batch must approve exactly the builds it named — and nothing else that happens to be in it.
    """
    from mod_update_checker.downloads import DownloadLedger

    waiting = [
        _bulk_entry("sodium", "Sodium", "sodium.jar", status="awaiting_install"),
        _bulk_entry("lithium", "Lithium", "lithium.jar", status="awaiting_install"),
        _bulk_entry("iris", "Iris", "iris.jar", status="awaiting_install"),
    ]
    plugin, server, source = _bulk_env(tmp_path, monkeypatch, waiting)

    ledger = DownloadLedger(
        Path(server.get_data_folder()) / plugin.DOWNLOAD_LEDGER_FILE_NAME
    )
    for entry in waiting:
        ledger.record(entry.mod_id, entry.download_filename, "b" * 40, "1.1.0",
                      "2026-01-01T00:00:00+00:00", installed_file=entry.file_name,
                      name=entry.name)
    ledger.save()

    plugin._manual_install(source, "all", "!!muc")

    assert plugin._pending_action["kind"] == "install_all"
    assert plugin._pending_action["files"] == ["iris.jar", "lithium.jar", "sodium.jar"]
    assert "即将安排安装 3 个" in source.body, source.body

    plugin._manual_confirm(source, "!!muc")

    assert "已授权 3 个 Mod" in source.body, source.body
    # Read back from disk: the command writes its own ledger, because by the time an admin
    # confirms, the plugin may have been reloaded and the record is what is on disk.
    written = DownloadLedger(
        Path(server.get_data_folder()) / plugin.DOWNLOAD_LEDGER_FILE_NAME
    )
    assert written.approved_keys() == ["iris", "lithium", "sodium"]


def test_install_all_with_nothing_waiting_says_so(tmp_path, monkeypatch):
    """Not an error, and not silence either: there is simply nothing at the install step yet."""
    plugin, _server, source = _bulk_env(
        tmp_path, monkeypatch, [_bulk_entry("sodium", "Sodium", "sodium.jar")]
    )

    plugin._manual_install(source, "all", "!!muc")

    assert "没有已下载、等待安装的 Mod" in source.body, source.body
    assert plugin._pending_action is None


# --------------------------------------------------------------------------------------
# The completion notice
#
# Two readers, one event: the console is where an admin who stepped away during the transfer
# catches up, and the game is where an admin who is *there* finds out the files arrived. The
# in-game gate is deliberately its own and these tests pin it in both directions — see
# ``_announce_download_complete`` for why it is not ``report.in_game``.
# --------------------------------------------------------------------------------------


def _check_with_fetched_files(tmp_path, monkeypatch, count, size, broadcast=True):
    """Drive one whole check whose download pass reports ``count`` files of ``size`` bytes.

    Only the reconcile step is stubbed; the announcement, the summary and the in-game delivery
    are the real code paths. The reconcile step is replaced rather than exercised because what
    is under test here is the notice, and staging real transfers to produce it would make the
    test a worse copy of ``test_e2e``.
    """
    import mod_update_checker as plugin

    server = _FakeServer(tmp_path, levels={"Admin": 4, "Player": 0})
    plugin._apply_language(server, _config_with({"language": "zh_cn"}))
    monkeypatch.setattr(plugin, "_online_players", {"Admin", "Player"}, raising=False)
    monkeypatch.setattr(
        plugin, "_reconcile_downloads",
        lambda server, report, config: (count, size), raising=False,
    )

    _run_with_stub(
        plugin, server, tmp_path, monkeypatch,
        _report(updates=0, mods_directory=str(Path(tmp_path) / "mods")),
        broadcast=broadcast, language="zh_cn",
    )
    return server


def test_a_finished_download_is_announced_to_the_console_and_to_admins(tmp_path, monkeypatch):
    """One line in the console, and the same news in game — addressed to who can act on it.

    Not gated on ``report.in_game``, which is off here as shipped: that switch is about "an
    update exists" announcements, which stay true until acted on and are repeated at the next
    login. This is this server's own files having changed a moment ago. The permission gate
    still applies: a player who cannot run the command is not told.
    """
    server = _check_with_fetched_files(tmp_path, monkeypatch, count=2, size=5 * 1024 * 1024)

    console = "\n".join(server.logger.messages)
    assert "下载完成" in console, console
    assert "2 个" in console and "5.0 MB" in console, console
    assert "!!modupdate install all" in console, "the console line must name the next command"

    told = "\n".join(server.told("Admin"))
    assert "下载完成" in told, told
    assert server.told("Player") == [], "a player who cannot act on the notice was told anyway"


def test_a_check_that_fetched_nothing_says_nothing(tmp_path, monkeypatch):
    """A completion line for a non-event is how a notice stops being read.

    The summary already describes where things stand; "download finished" is only true when
    something actually finished.
    """
    server = _check_with_fetched_files(tmp_path, monkeypatch, count=0, size=0)

    assert "下载完成" not in "\n".join(server.logger.messages)
    assert server.told() == []


def test_a_join_check_keeps_the_completion_on_the_console_only(tmp_path, monkeypatch):
    """The join path runs with ``broadcast=False``.

    Its admin is being answered personally a moment later, and re-broadcasting at that instant
    is how one event becomes two messages for everyone else. The console still records it,
    because the console is where the history is.
    """
    server = _check_with_fetched_files(tmp_path, monkeypatch, count=1, size=2048,
                                       broadcast=False)

    assert "下载完成" in "\n".join(server.logger.messages)
    assert server.told() == []


def test_install_on_stop_installs_only_what_was_authorised(tmp_path, monkeypatch):
    """With the automatic half off, the ledger is not a work list — the approvals are.

    This is the invariant the per-record flag exists for. Installing the whole ledger here
    would turn "install this one" into "install everything that happens to be downloaded".
    """
    from mod_update_checker.downloads import DownloadLedger

    plugin_mod, server, _source, entry, ledger = _manual_env(
        tmp_path, monkeypatch, status="awaiting_install"
    )
    monkeypatch.setattr(plugin_mod, "_config", _config_with(), raising=False)

    # Two real jars: the installed one, and the fetched replacement.
    mods = Path(tmp_path) / "mods"
    downloads = Path(server.get_data_folder()) / "downloads"
    mods.mkdir(parents=True, exist_ok=True)
    downloads.mkdir(parents=True, exist_ok=True)
    (mods / "sodium.jar").write_bytes(b"old")
    blob = b"new-build"
    (downloads / "sodium-fabric-1.1.0.jar").write_bytes(blob)
    (mods / "other.jar").write_bytes(b"other-old")
    (downloads / "other-2.0.jar").write_bytes(b"other-new")

    fresh = DownloadLedger(
        Path(server.get_data_folder()) / plugin_mod.DOWNLOAD_LEDGER_FILE_NAME
    )
    fresh.record("sodium", "sodium-fabric-1.1.0.jar", hashlib.sha1(blob).hexdigest(), "1.1.0",
                 "2026-01-01T00:00:00+00:00", installed_file="sodium.jar", name="Sodium")
    fresh.record("other", "other-2.0.jar", hashlib.sha1(b"other-new").hexdigest(), "2.0",
                 "2026-01-01T00:00:00+00:00", installed_file="other.jar", name="Other")
    fresh.approve("sodium")
    fresh.save()

    plugin_mod._install_on_stop(server)

    assert (mods / "sodium-fabric-1.1.0.jar").is_file(), "the authorised mod was not installed"
    assert (mods / "sodium.jar.old").is_file(), "the old jar was not kept"
    assert (mods / "other.jar").is_file() and not (mods / "other.jar.old").exists(), (
        "an unauthorised mod was installed"
    )
    assert (mods / "other-2.0.jar").exists() is False


def test_install_on_stop_does_nothing_at_all_without_an_approval(tmp_path, monkeypatch):
    """A server that never used the command must not grow an install report."""

    plugin_mod, server, _source, _entry, _ledger = _manual_env(tmp_path, monkeypatch)
    monkeypatch.setattr(plugin_mod, "_config", _config_with(), raising=False)

    mods = Path(tmp_path) / "mods"
    mods.mkdir(parents=True, exist_ok=True)
    (mods / "sodium.jar").write_bytes(b"old")

    plugin_mod._install_on_stop(server)

    assert not (Path(server.get_data_folder()) / plugin_mod.INSTALL_REPORT_FILE_NAME).exists()
    assert (mods / "sodium.jar.old").exists() is False


def test_the_automatic_setting_installs_the_whole_ledger(tmp_path, monkeypatch):
    """Switching install-on-stop on is itself the instruction, so the flag is not consulted."""
    from mod_update_checker.installer import pending_records

    plugin_mod, _server, _source, _entry, ledger = _manual_env(tmp_path, monkeypatch)
    ledger.record("other", "other-2.0.jar", "b" * 40, "2.0", "2026-01-01T00:00:00+00:00",
                  installed_file="other.jar", name="Other")
    ledger.save()

    assert pending_records(ledger, approved_only=False) == ["other", "sodium"]
    assert pending_records(ledger, approved_only=True) == []


# --------------------------------------------------------------------------------------
# Whether a report may be reused at all
#
# The age window used to be the only question, which was enough while the report only ever
# lived in memory. Now that ``last_report.json`` is read back after a restart, "produced
# recently" and "still describes this server" became different questions — and answering only
# the first one means an admin who just swapped their mods folder, changed loader, or upgraded
# Minecraft is shown the previous server's answer as though it were current.
# --------------------------------------------------------------------------------------


def _scan_of(tmp_path, *jar_names):
    from mod_update_checker.scanner import scan_mods

    mods = Path(tmp_path) / "mods"
    mods.mkdir(parents=True, exist_ok=True)
    for name in jar_names:
        (mods / name).write_bytes(b"not read; only listed")
    return scan_mods(mods)


def _run_with_stub(plugin, server, tmp_path, monkeypatch, report, broadcast=True, **config):
    """Drive ``_run_check`` with ``_scan_current`` and ``Checker`` stubbed out."""
    from mod_update_checker.serverinfo import ServerContext

    recorded = {}

    class _RecordingChecker:
        def __init__(self, options, logger=None):
            recorded["options"] = options

        def run(self, scan, context, cache_path=None, map_path=None):
            recorded["map_path"] = map_path
            report.server = context
            report.mods_directory = scan.directory
            return report

        def close(self):
            pass

    monkeypatch.setattr(plugin, "Checker", _RecordingChecker, raising=False)
    monkeypatch.setattr(
        plugin,
        "_scan_current",
        lambda *_a, **_k: (
            _scan_of(tmp_path),
            ServerContext(mc_version="26.3", mc_version_source="config", loader="fabric"),
        ),
        raising=False,
    )
    settings = {"report.write_file": False}
    settings.update(config)
    monkeypatch.setattr(plugin, "_config", _config_with(settings), raising=False)
    plugin._stop_event.clear()
    plugin._last_report = None

    plugin._run_check(server, broadcast=broadcast)
    plugin._last_report = None
    return recorded


def test_a_report_for_a_different_mods_folder_is_not_reused(entry_env, tmp_path):
    """Pointing ``server.mods_directory`` somewhere else makes the stored answer wrong."""
    plugin, server, calls = entry_env
    plugin._last_report = _report(updates=1, age_seconds=60, mods_directory="D:/somewhere/else")

    plugin._admin_join_worker(server, "Admin")

    assert calls, "the stored report described another folder and should not have been reused"


def test_a_report_for_a_different_pinned_game_version_is_not_reused(entry_env, tmp_path,
                                                                  monkeypatch):
    """Upgrading Minecraft is exactly when a stale answer is most dangerous.

    With ``mc_version: auto`` there is nothing cheap to compare against, so this asserts the
    case the admin can control — and the README already tells them to pin it after a major
    upgrade.
    """
    plugin, server, calls = entry_env
    monkeypatch.setattr(
        plugin, "_config",
        _config_with({"report.reuse_report_minutes": 30, "server.mc_version": "1.22"}),
        raising=False,
    )
    plugin._last_report = _report(updates=1, age_seconds=60)

    plugin._admin_join_worker(server, "Admin")

    assert calls, "the stored report was for another game version"


def test_a_report_from_another_loader_is_not_reused(entry_env, monkeypatch):
    from mod_update_checker.serverinfo import ServerContext

    plugin, server, calls = entry_env
    monkeypatch.setattr(
        plugin, "_config",
        _config_with({"report.reuse_report_minutes": 30, "server.loader": "neoforge"}),
        raising=False,
    )
    stale = _report(updates=1, age_seconds=60)
    stale.server = ServerContext(mc_version="26.3", loader="fabric")
    plugin._last_report = stale

    plugin._admin_join_worker(server, "Admin")

    assert calls, "the stored report was produced for a different loader"


def test_a_report_that_still_applies_is_reused(entry_env):
    """The other direction, so the gate cannot pass by refusing everything."""
    plugin, server, calls = entry_env
    plugin._last_report = _report(updates=1, age_seconds=60)

    plugin._admin_join_worker(server, "Admin")

    assert calls == []


# --------------------------------------------------------------------------------------
# last_report.json being read back
# --------------------------------------------------------------------------------------


def _store_report(server, **overrides):
    from mod_update_checker.report import Report

    values = {
        "generated_at": "2026-01-01T00:00:00+00:00",
        "mods_directory": _MODS_DIRECTORY,
    }
    values.update(overrides)
    report = overrides.get("report")
    if report is None:
        from mod_update_checker.serverinfo import ServerContext

        report = Report(
            generated_at=values["generated_at"],
            server=ServerContext(mc_version="26.3", mc_version_source="config",
                                 loader="fabric"),
            mods_directory=values["mods_directory"],
        )
    Path(server.get_data_folder(), "last_report.json").write_text(
        report.to_json(), encoding="utf-8"
    )
    return report


def test_a_stored_report_is_read_back_at_load(tmp_path, monkeypatch):
    """Otherwise every restart throws the reuse window away and re-asks Modrinth."""
    import mod_update_checker as plugin
    from mod_update_checker.report import Report

    server = _FakeServer(tmp_path)
    _store_report(server)
    monkeypatch.setattr(plugin, "_last_report", None, raising=False)
    plugin._stop_event.clear()

    plugin.on_load(server, None)

    assert isinstance(plugin._last_report, Report)
    assert plugin._last_report.mods_directory == _MODS_DIRECTORY
    plugin._last_report = None
    plugin._stop_scheduler()


def test_a_stored_report_is_not_read_when_report_files_are_off(tmp_path, monkeypatch):
    """``write_file: false`` means "do not persist results", so nothing is read back either."""
    import mod_update_checker as plugin

    server = _FakeServer(tmp_path)
    _store_report(server)
    plugin._stop_event.clear()
    monkeypatch.setattr(plugin, "_last_report", None, raising=False)
    monkeypatch.setattr(
        plugin, "_config",
        _config_with({"report.write_file": False}),
        raising=False,
    )

    assert plugin._load_previous_report(server, plugin._config) is None

    plugin._stop_scheduler()


def test_an_unreadable_stored_report_is_ignored_rather_than_raising(tmp_path):
    import mod_update_checker as plugin

    server = _FakeServer(tmp_path)
    Path(server.get_data_folder(), "last_report.json").write_text("{not json", encoding="utf-8")

    assert plugin._load_previous_report(server, _config_with()) is None


def test_a_live_report_from_a_reload_is_kept_over_the_one_on_disk(tmp_path, monkeypatch):
    """A reload carries the in-memory report, which is by definition at least as fresh."""
    import mod_update_checker as plugin

    server = _FakeServer(tmp_path)
    _store_report(server, generated_at="2020-01-01T00:00:00+00:00")
    live = _report(updates=1)
    plugin._stop_event.clear()
    monkeypatch.setattr(plugin, "_last_report", live, raising=False)

    plugin.on_load(server, None)

    assert plugin._last_report is live
    plugin._last_report = None
    plugin._stop_scheduler()


def test_the_configured_map_file_reaches_the_checker(tmp_path, monkeypatch):
    """The map is only useful if the path assembled in ``_run_check`` arrives intact."""
    import mod_update_checker as plugin

    server = _FakeServer(tmp_path)
    recorded = _run_with_stub(
        plugin, server, tmp_path, monkeypatch,
        _report(updates=0, mods_directory=str(Path(tmp_path) / "mods")),
    )

    assert recorded["map_path"] == Path(server.get_data_folder()) / "project-map.json"


def test_a_map_setting_that_is_a_path_is_rejected_and_warned_about(tmp_path, monkeypatch):
    """It has to be a file name, and a silent refusal would look like a broken feature."""
    import mod_update_checker as plugin

    server = _FakeServer(tmp_path)
    recorded = _run_with_stub(
        plugin, server, tmp_path, monkeypatch,
        _report(updates=0, mods_directory=str(Path(tmp_path) / "mods")),
        **{"sources.manual_map": "../outside.json"},
    )

    assert recorded["map_path"] is None
    assert any("manual_map" in message for message in server.logger.messages), (
        server.logger.messages
    )


def test_an_empty_map_setting_switches_the_feature_off_without_a_warning(tmp_path, monkeypatch):
    import mod_update_checker as plugin

    server = _FakeServer(tmp_path)
    recorded = _run_with_stub(
        plugin, server, tmp_path, monkeypatch,
        _report(updates=0, mods_directory=str(Path(tmp_path) / "mods")),
        **{"sources.manual_map": ""},
    )

    assert recorded["map_path"] is None
    assert not any("manual_map" in message for message in server.logger.messages)


# --------------------------------------------------------------------------------------
# The status page's one line about the map
#
# The file is silent by design, so this line is the only way to answer "I wrote that file and
# nothing happened" without reading the config and guessing.
# --------------------------------------------------------------------------------------


def _status_with_map(tmp_path, monkeypatch, map_payload=None, setting=None):
    import mod_update_checker as plugin

    server = _FakeServer(tmp_path)
    if map_payload is not None:
        Path(server.get_data_folder(), "project-map.json").write_text(
            map_payload, encoding="utf-8"
        )

    overrides = {"language": "zh_cn"}
    if setting is not None:
        overrides["sources.manual_map"] = setting
    config = _config_with(overrides)

    class _Scan:
        directory = "server/mods"
        mods = []
        disabled = []

    previous = (plugin._config, plugin._scan_current, plugin._server)
    try:
        plugin._config = config
        plugin._apply_language(server, config)
        plugin._scan_current = lambda *_a, **_k: (
            _Scan(),
            __import__(
                "mod_update_checker.serverinfo", fromlist=["ServerContext"]
            ).ServerContext(mc_version="26.3", mc_version_source="config", loader="fabric"),
        )
        plugin._server = server
        source = _ReplyRecorder()
        plugin._show_status(source)
    finally:
        plugin._config, plugin._scan_current, plugin._server = previous
    return "\n".join(str(item) for item in source.replies)


def test_the_status_page_says_the_map_is_off(tmp_path, monkeypatch):
    rendered = _status_with_map(tmp_path, monkeypatch, setting="")

    assert "本地映射表" in rendered and "关" in rendered


def test_the_status_page_counts_what_is_in_the_map(tmp_path, monkeypatch):
    import json

    payload = json.dumps(
        {"version": 1, "by_sha1": {"a" * 40: "sodium"}, "by_mod_id": {"mycustommod": "lithium"}}
    )
    rendered = _status_with_map(tmp_path, monkeypatch, map_payload=payload)

    assert "project-map.json" in rendered
    assert "1 条按哈希" in rendered and "1 条按 mod id" in rendered


def test_the_status_page_says_when_the_map_file_is_missing_pieces(tmp_path, monkeypatch):
    """The reason is carried through, because "cannot read it" alone is not actionable."""
    rendered = _status_with_map(tmp_path, monkeypatch, map_payload='{"by_sha1": []}')

    assert "无法读取" in rendered
    assert "by_sha1" in rendered


def test_the_status_page_says_when_the_map_name_is_not_a_file_name(tmp_path, monkeypatch):
    rendered = _status_with_map(tmp_path, monkeypatch, setting="sub/dir.json")

    assert "已忽略" in rendered and "sub/dir.json" in rendered


def test_the_status_page_says_which_config_file_it_read(tmp_path, monkeypatch):
    """The page is where "which file is this server actually using" gets answered.

    The question reads as trivial until a file has been edited, upgraded and reinstalled
    without ever changing — at which point the first useful fact is the path the plugin
    itself resolved, on the server that is running.
    """
    rendered = _status_with_map(tmp_path, monkeypatch, setting="")

    expected = str(Path(tmp_path) / "config" / "mod_update_checker" / "config.json")
    assert "配置文件" in rendered, rendered
    assert expected in rendered, rendered


def test_the_status_screen_does_not_hash_the_mods_folder(tmp_path, monkeypatch):
    """``!!muc status`` shows a count, a directory and a version — none of which is a SHA-1.

    And it runs on MCDR's command thread, so hashing every jar means the server waits. On a
    modpack of a few hundred megabytes that is about a second of stall for a screen that only
    needed the file names.

    Asserted by counting calls to the digest function rather than by timing anything, and
    rather than by making it raise: ``scan_mods`` deliberately swallows a per-jar exception and
    records that jar as unreadable, so a poisoning function would leave the jar count intact
    and this test passing for the wrong reason.
    """
    import mod_update_checker as plugin
    import mod_update_checker.scanner as scanner

    from support import fabric_metadata, write_jar

    mods = Path(tmp_path) / "mods"
    mods.mkdir(parents=True, exist_ok=True)
    write_jar(mods / "example.jar", fabric=fabric_metadata(id="example", version="1.0.0"))

    read_calls = []

    def spy(handle):
        read_calls.append(handle)
        return "", 0

    monkeypatch.setattr(scanner, "digests_of_file", spy, raising=False)

    server = _FakeServer(tmp_path)
    previous = (plugin._config, plugin._server)
    try:
        plugin._config = _config_with({"language": "zh_cn"})
        plugin._apply_language(server, plugin._config)
        plugin._server = server
        source = _ReplyRecorder()
        plugin._show_status(source)
    finally:
        plugin._config, plugin._server = previous

    rendered = "\n".join(str(item) for item in source.replies)
    assert read_calls == [], "!!muc status read jar bytes"
    assert "1 个 jar" in rendered, rendered


def test_the_status_screen_still_reads_the_metadata(tmp_path, monkeypatch):
    """The other half: skipping the hashing must not turn into skipping the folder.

    Without this, "does not hash" could be satisfied by a status page that reports zero jars.
    """
    import mod_update_checker as plugin

    from support import fabric_metadata, write_jar

    mods = Path(tmp_path) / "mods"
    mods.mkdir(parents=True, exist_ok=True)
    for index in range(3):
        write_jar(mods / "mod{}.jar".format(index),
                  fabric=fabric_metadata(id="mod{}".format(index)))

    server = _FakeServer(tmp_path)
    previous = (plugin._config, plugin._server)
    try:
        plugin._config = _config_with({"language": "zh_cn"})
        plugin._apply_language(server, plugin._config)
        plugin._server = server
        source = _ReplyRecorder()
        plugin._show_status(source)
    finally:
        plugin._config, plugin._server = previous

    assert "3 个 jar" in "\n".join(str(item) for item in source.replies)


# --------------------------------------------------------------------------------------
# Is the config file complete?
#
# The question came from a user report that ``download.install_on_stop`` was missing from
# their file. It turned out not to be reproducible — MCDR fills a missing field in from the
# class default and rewrites the file, on every version this plugin supports, which is
# asserted below — but asking it turned up no test that the shipped defaults *can* produce a
# complete file at all. A field MCDR could not write would be invisible: the plugin would use
# its class default and the admin would simply never see the option.
# --------------------------------------------------------------------------------------


def _serialised_leaf_paths(node, prefix=""):
    """Every scalar in a serialised config, as a dotted path."""
    leaves = []
    for key, value in node.items():
        path = "{}.{}".format(prefix, key) if prefix else key
        if isinstance(value, dict):
            leaves.extend(_serialised_leaf_paths(value, path))
        else:
            leaves.append(path)
    return leaves


def test_every_option_reaches_the_config_file():
    """``Config``'s fields and the file MCDR writes must be the same set, both ways.

    One direction catches an option that exists but never gets written — an admin would only
    ever see the documented default and could not change it. The other catches a written key
    with no field behind it, which ``deserialize`` reports as redundant and discards.
    """
    import mod_update_checker as plugin

    from support import option_paths

    fields = set(option_paths(plugin.Config))
    written = set(_serialised_leaf_paths(plugin.Config.get_default().serialize()))

    assert written - fields == set(), "written but not an option"
    assert fields - written == set(), "an option that never reaches the file"


def test_a_config_file_missing_an_option_is_filled_in_and_rewritten(tmp_path):
    """Why "my file does not have that option" self-heals, and the reason it is worth knowing.

    MCDR hands ``deserialize`` a callback for missing fields; any miss marks the result
    imperfect and the file is saved again straight away. So an option added in a later release
    appears in an upgraded server's file on the next plugin load, keeping whatever the admin
    had already set.

    Simulated with MCDR's own ``SimpleConfigHandler`` and ``deserialize`` rather than by booting
    a server, because the behaviour under test is MCDR's and this is exactly the code path
    ``load_config_simple`` takes.
    """
    from mcdreforged.plugin.si._simple_config_handler import SimpleConfigHandler

    import mod_update_checker as plugin

    handler = SimpleConfigHandler("config.json", None, str(tmp_path))
    path = tmp_path / "config.json"

    stale = plugin.Config.get_default().serialize()
    del stale["download"]["install_on_stop"]      # as if written before that option existed
    del stale["sources"]["manual_map"]
    stale["download"]["folder_name"] = "jars"     # something the admin changed by hand
    handler.save(stale, encoding="utf8")

    state = {"imperfect": False}

    def note_missing(*_args):
        state["imperfect"] = True

    config = plugin.Config.deserialize(
        handler.load(encoding="utf8"),
        missing_callback=note_missing,
        redundancy_callback=note_missing,
    )
    assert state["imperfect"], "MCDR would not have noticed the file was incomplete"
    handler.save(config.serialize(), encoding="utf8")

    rewritten = json.loads(path.read_text(encoding="utf-8"))
    assert rewritten["download"]["install_on_stop"] is False
    assert rewritten["sources"]["manual_map"] == "project-map.json"
    assert rewritten["download"]["folder_name"] == "jars", "the admin's own value was lost"
